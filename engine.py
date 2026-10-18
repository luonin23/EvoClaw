"""EvoClaw v2.0 — 策略引擎。

主循环每 loop_interval 秒执行一次：
  1. 从 DB 重载配置（改配置即时生效）
  2. 同步 dry_run 到客户端
  3. 按 mode 调度对应策略（off / grid / dca）
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from strategies.grid import GridStrategy
from strategies.dca import DCAStrategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, client, db):
        self.client = client
        self.db = db
        self.config = db.load_config()
        self.running = False
        self.paused = bool(db.get_runtime_stat("paused", 0))
        self._last_mode = None
        self._last_ok = 0.0
        self.last_error = ""
        self.tick = 0

    def _reload(self):
        try:
            self.config = self.db.load_config()
        except Exception as e:
            log.error(f"Config reload failed: {e}")

    def _strategies(self):
        """本 tick 要跑的策略列表。

        以前是「mode 三选一」，导致网格和现货不能同时跑。现在：
          · mode=off   → 什么都不跑（只监控）
          · mode=grid  → 合约网格
          · mode=dca   → 现货交易
          · `dca.enabled` 独立开关：即使在跑网格，也会同时执行现货交易
          · `profit_dca.enabled` 独立开关：用合约利润买现货，可与其他两个同时开
        三者**互不冲突**：网格管合约、现货交易管现货买卖、利润定投只买。
        """
        mode = self.config.get("mode", "off")
        if mode == "off":
            return []
        out = []
        if mode == "grid":
            out.append(GridStrategy(self.client, self.db, self.config))
        dca_cfg = self.config.get("dca", {}) or {}
        if mode == "dca" or dca_cfg.get("enabled"):
            out.append(DCAStrategy(self.client, self.db, self.config))
        if (self.config.get("profit_dca", {}) or {}).get("enabled"):
            from strategies.profit_dca import ProfitDCA
            out.append(ProfitDCA(self.client, self.db, self.config))
        return out

    async def _sync_liquidations(self):
        """同步交易所强平记录（只读，5 分钟一次；暂停时也照常监控）。

        数据源：币安 forceOrders —— 逐仓/全仓被强平的每一笔平仓单。
        - 首次同步回补 90 天；之后每次从「上次同步-1小时」增量拉，重叠部分
          靠 order_id 唯一约束幂等去重
        - 同时写入成交记录（type=liquidation）；同一标的时间窗内只保留一行，
          对账路径的完整盈亏会覆盖监控先行插入的简版数据
        """
        self.db.normalize_liquidation_rates()
        now = time.time()
        last = float(self.db.get_runtime_stat("liq_sync_ts", 0) or 0)
        # 一次性的 90 天回补：新增盈亏字段后，历史记录也要补上真实亏损
        need_backfill = (last <= 0
                         or not self.db.get_runtime_stat("liq_pnl_backfill_v3", 0))
        # 回补优先级高于节流；日常增量才受 5 分钟限制
        if not need_backfill and now - last < 300:
            return
        rows = []
        try:
            if not need_backfill:
                # 增量：重叠 1 小时，幂等去重
                rows = await self.client.fetch_force_orders(
                    since_ms=int((last - 3600) * 1000), end_ms=int(now * 1000), limit=100)
            else:
                # 首次回补 90 天；接口单次最多 100 条，按 7 天窗口分段拉全
                start = now - 90 * 86400
                while start < now:
                    end = min(start + 7 * 86400, now)
                    part = await self.client.fetch_force_orders(
                        since_ms=int(start * 1000), end_ms=int(end * 1000), limit=100)
                    rows.extend(part)
                    start = end
        except Exception as e:
            log.warning(f"强平监控同步失败（不影响交易）: {e}")
            return
        # 分批窗口/增量重叠可能返回重复强平单 → 先按 order_id 去重，
        # 否则分摊盈亏时总名义会被重复计算。
        rows = list({r["order_id"]: r for r in rows if r.get("order_id")}.values())
        # 按「标的 + 秒」聚合：同一次强平会在一秒内批量产生多张强平单。
        # 再用同秒的收入流水（REALIZED_PNL + COMMISSION）还原真实净盈亏。
        groups: dict = {}
        for r in rows:
            groups.setdefault((r["symbol"], r["ts"][:19]), []).append(r)
        # 收入流水按**秒**拉一次，再按标的分配 —— 同一次强平可能涉及几十个标的，
        # 按「标的+秒」逐个拉会重复请求同一秒并撞上限。
        second_pnl: dict = {}
        for sec in sorted({k[1] for k in groups})[:50]:   # 限频：最多 50 个时间点
            try:
                ts_ms = int(datetime.fromisoformat(sec).replace(
                    tzinfo=timezone.utc).timestamp() * 1000)
            except Exception:
                continue
            incs = await self.client.fetch_income_window(ts_ms - 2000, ts_ms + 2000)
            for x in incs:
                if x.get("incomeType") not in ("REALIZED_PNL", "COMMISSION"):
                    continue
                s = self.client.user_symbol(str(x.get("symbol") or ""))
                second_pnl[(s, sec)] = second_pnl.get((s, sec), 0.0) + float(
                    x.get("income") or 0)
        group_pnl = {key: second_pnl.get(key, 0.0) for key in groups}
        for key, items in groups.items():
            total_n = sum(i["notional"] for i in items) or 1.0
            for i in items:
                i["pnl"] = group_pnl.get(key, 0.0) * (i["notional"] / total_n)

        # 「历史回补 / 运行中」的分界 = v2 的第一笔订单时间。
        # 分界之前的强平是 v1 时代的爆仓记录：只展示，不进策略统计。
        cutoff_ms = float(self.db.get_runtime_stat("v2_first_order_ms", 0) or 0)
        if cutoff_ms <= 0:
            fts = self.db.first_order_ts()
            if fts:
                try:
                    cutoff_ms = int(datetime.fromisoformat(fts).replace(
                        tzinfo=timezone.utc).timestamp() * 1000)
                    self.db.set_runtime_stat("v2_first_order_ms", cutoff_ms)
                except Exception:
                    cutoff_ms = 0

        new = 0
        for r in rows:
            inserted = self.db.insert_liquidation(r)
            if inserted:
                new += 1
            else:
                self.db.update_liquidation_pnl(r["order_id"], r["pnl"])
            # 成交记录是「标的+时间」级别的一行 → 用该批次总盈亏，不按单张分摊
            opnl = group_pnl.get((r["symbol"], r["ts"][:19]), 0.0)
            self.db.update_liquidation_order_pnl(r["symbol"], r["ts"], opnl)
            if inserted:
                try:
                    before_v2 = (cutoff_ms > 0 and int(datetime.fromisoformat(
                        r["ts"]).replace(tzinfo=timezone.utc).timestamp() * 1000) < cutoff_ms)
                except Exception:
                    before_v2 = False
                self.db.record_liquidation_order({
                    "ts": r["ts"], "mode": "grid", "symbol": r["symbol"],
                    "side": r["side"] or "sell", "price": r["price"],
                    "amount": r["amount"], "cost": r["notional"],
                    "fee": 0, "pnl": opnl, "pnl_rate": 0, "dry_run": False,
                    "note": f"强制平仓（{r['position_side'] or '仓位'}）",
                    "order_id": r["order_id"],
                }, overwrite=False, source="backfill" if before_v2 else "monitor")
        self.db.set_runtime_stat("liq_sync_ts", now)
        if need_backfill:
            # 只有真正完成了 90 天回补才置位；增量路径置位会导致历史盈亏永远补不上
            self.db.set_runtime_stat("liq_pnl_backfill_v3", 1)
        if new:
            log.warning(f"强平监控：新增 {new} 条强平记录")

    def set_paused(self, paused: bool):
        self.paused = paused
        self.db.set_runtime_stat("paused", 1 if paused else 0)
        log.warning(f"引擎 {'已暂停' if paused else '已恢复'}")

    async def run(self):
        self.running = True
        log.info(f"引擎启动 (mode={self.config.get('mode')}, dry_run={self.config.get('dry_run')}, paused={self.paused})")
        while self.running:
            interval = max(3, int(self.config.get("loop_interval", 10) or 10))
            try:
                # 强平监控是只读操作，暂停交易时也要继续跑
                await self._sync_liquidations()

                if self.paused:
                    if self.tick % 30 == 0:
                        log.info("已暂停 — 跳过本轮")
                    self.tick += 1
                    await asyncio.sleep(interval)
                    continue

                self._reload()
                # 配置里的 dry_run 实时同步到客户端
                self.client.dry_run = bool(self.config.get("dry_run", True))
                self.client.min_order_notional = float(self.config.get("min_order_notional", 5) or 5)
                self.client.notional_safety = float(self.config.get("order_notional_safety", 1.02) or 1.02)

                mode = self.config.get("mode", "off")
                if mode != self._last_mode:
                    log.info(f"模式切换 → {mode}")
                    self._last_mode = mode

                for strat in self._strategies():
                    try:
                        await asyncio.wait_for(strat.step(), timeout=60)
                    except asyncio.TimeoutError:
                        self.last_error = f"{type(strat).__name__} 执行超时(60s)"
                        log.error(f"策略执行超时，跳过本轮 ({type(strat).__name__})")
                    except Exception as e:
                        self.last_error = f"{type(strat).__name__}: {e}"
                        log.error(f"策略异常 ({type(strat).__name__}): {e}")

                self._last_ok = time.monotonic()
                self.last_error = ""
                self.tick += 1

                if self.tick % 60 == 0:
                    self.db.checkpoint()
                if self.tick % 30 == 0:
                    # 数据库自检：WAL 被外部删掉时，进程会「看着一切正常、
                    # 其实已与磁盘脱钩」。宁可停下不交易，也不要在一个
                    # 不一致的库上继续下单。
                    try:
                        ok, why = self.db.health_check()
                    except Exception as e:
                        ok, why = False, str(e)
                    if not ok:
                        log.error(f"数据库自检失败：{why} → 已暂停交易，请重启程序")
                        self.paused = True

            except asyncio.TimeoutError:
                self.last_error = "策略执行超时(60s)"
                log.error(f"策略执行超时，跳过本轮 (mode={self.config.get('mode')})")
            except Exception as e:
                self.last_error = str(e)
                log.error(f"引擎异常: {e}")

            await asyncio.sleep(interval)
        log.info("引擎停止")

    def stop(self):
        self.running = False

    async def health(self) -> dict:
        age = time.monotonic() - self._last_ok if self._last_ok else None
        return {
            "running": self.running,
            "paused": self.paused,
            "mode": self.config.get("mode", "off"),
            "dry_run": bool(self.config.get("dry_run", True)),
            "tick": self.tick,
            "last_ok_seconds_ago": round(age, 1) if age is not None else None,
            "last_error": self.last_error,
        }
