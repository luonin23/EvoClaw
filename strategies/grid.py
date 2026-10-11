"""方案B — 低杠杆网格。

模型（订单式，避免"瞬发成交"错误）：
  · 在 [lower, upper] 内布 N 个价格档位。
  · 现价**下方**的最近若干档挂买单；现价**上方**的最近若干档挂卖单。
  · 卖单由「底仓」背书 —— 初始化时按卖单数量市价买入底仓。
  · 买单成交 → 在上一档挂卖单；卖单成交 → 在下一档挂买单。如此循环吃档差。
  · 不设止损：价格跌出区间就一直持有底仓等回来；靠低杠杆 + 敞口闸门防强平。
"""

import logging
import math
import time

from exchange_client import _throttle
from selector import select_grid_symbols, qualify_symbol

log = logging.getLogger(__name__)


def grid_points(lower: float, upper: float, grids: int) -> list[float]:
    """在 [lower, upper] 之间生成 grids 个几何分布价格点。"""
    if lower <= 0 or upper <= lower or grids < 2:
        return []
    ratio = (upper / lower) ** (1.0 / (grids - 1))
    return [lower * (ratio ** i) for i in range(grids)]


class GridStrategy:
    mode = "grid"

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config
        self._selection: list[dict] = []
        self._sel_ts = 0.0

    @property
    def cfg(self) -> dict:
        return self.config.get("grid", {}) or {}

    # ===== 选币 =====
    def _derive_grids(self, pct: float) -> int:
        """按波动率推导档位数：档距 = 振幅 × step_ratio，换算成档数。"""
        ratio = float(self.cfg.get("step_ratio", 0.3) or 0.3)
        step = max(1e-6, abs(pct) * ratio)
        span = (1 + pct) / (1 - pct)
        g = int(round(math.log(span) / math.log(1 + step))) + 1
        return max(2, min(int(self.cfg.get("max_grids", 15) or 15), g))

    def _symbol_range_pct(self, pick: dict) -> float:
        fixed = float(self.cfg.get("auto_range_pct", 0) or 0)
        if fixed > 0:
            return fixed
        return max(0.04, min(0.15, float((pick or {}).get("range") or 0.06)))

    def _band_pct(self, pct: float) -> float:
        """相邻档位的价格间距（比例）≈ 振幅 × step_ratio，与档位推导同源。"""
        ratio = float(self.cfg.get("step_ratio", 0.3) or 0.3)
        return max(1e-4, abs(pct) * ratio)

    def _auto_symbol_cap(self, equity: float, picks: list) -> int:
        """max_symbols=0（自动）时，按资金推导能同时跑几个标的。

        单标的占用名义 ≈ 档数 × 单格金额（最坏情况：价格跌到区间底、买档全成交）。
        敞口上限 = max_exposure_ratio × 权益，可容纳标的数 = 上限 / 单标的占用。
        账户越大能跑的越多，不需要人手填数字。
        """
        size = float(self.cfg.get("size_usdt", 0) or 0)
        ratio = float(self.cfg.get("max_exposure_ratio", 3.0) or 3.0)
        if size <= 0 or ratio <= 0 or equity <= 0 or not picks:
            return 1
        per = max(self._derive_grids(self._symbol_range_pct(p)) for p in picks) * size
        if per <= 0:
            return 1
        return max(1, min(10, int((equity * ratio) / per)))

    async def _refresh_selection(self, equity: float = 0.0):
        """按配置周期重新选币（默认每 10 分钟一次）。

        引擎每 tick 会新建策略实例，因此选币结果与时间戳必须落库，
        否则会在每个 tick 重扫全市场（拉取全部 ticker，代价很高）。
        max_symbols=0 表示自动：按资金推导上限，而非无限（否则会为
        买不起的标的反复建空壳网格）。

        **粘性**：刷新很快时，排名每轮都会有几个名次的抖动；若每次都以新排名
        为准，正在跑的网格会被反复踢成「只卖不买」，刚要积累的仓位被抖散。
        所以已选中的标的只要**仍然合格**就原样保留，新选出的只用来补足名额。
        """
        cfg = self.cfg
        interval = float(cfg.get("refresh_min", 0) or 0) * 60
        if interval <= 0:
            interval = float(cfg.get("refresh_hours", 6) or 6) * 3600
        now = time.time()
        n = int(cfg.get("max_symbols", 0) or 0)
        ts = float(self.db.get_runtime_stat("grid_selection_ts", 0) or 0)
        if ts and (now - ts) < interval:
            # 沿用已落库的选币结果，但按当前上限截断
            # （改了 max_symbols / 资金变化不必等一个周期才生效）
            saved = list(self.config.get("grid_selection") or [])
            limit = n if n > 0 else self._auto_symbol_cap(equity, saved)
            saved = saved[:limit] if limit > 0 else []
            if saved:
                self._selection = saved
                self._sel_ts = ts
                return

        size = float(cfg.get("size_usdt", 0) or 0)
        picks = await select_grid_symbols(
            self.client, cfg, size, n if n > 0 else 999)
        if not picks:
            return
        limit = max(1, n if n > 0 else self._auto_symbol_cap(equity, picks))

        # 选币器返回的是**全部合格标的**（按分数排序），所以「仍在榜上」=
        # 「仍然合格」。已在跑且仍合格的，优先保留。
        prev_syms = [s.get("symbol") for s in (self.config.get("grid_selection") or [])
                     if s.get("symbol")]
        by_sym = {p["symbol"]: p for p in picks}
        keep = [by_sym[s] for s in prev_syms if s in by_sym][:limit]
        keepset = {k["symbol"] for k in keep}
        merged = keep + [p for p in picks if p["symbol"] not in keepset]
        if len(merged) > limit:
            log.info(f"选币：{len(merged)} 个可用（其中 {len(keep)} 个为在跑标的保留），"
                     f"按名额取前 {limit} 个")
        picks = merged[:limit]
        if picks:
            self._selection = picks
            self._sel_ts = now
            self.db.set_runtime_stat("grid_selection_ts", now)
            fresh = self.db.load_config()
            fresh["grid_selection"] = picks
            self.db.save_config(fresh)

    def _specs(self) -> list[dict]:
        """当前要跑网格的标的清单。auto_select 时来自选币结果，否则来自手写配置。"""
        cfg = self.cfg
        if cfg.get("auto_select"):
            grids = int(cfg.get("grids", 0) or 0)
            size = float(cfg.get("size_usdt", 0) or 0)
            fixed = float(cfg.get("auto_range_pct", 0) or 0)
            out = []
            for s in self._selection:
                # 区间宽度随标的自身波动率自适应：取 1 倍日振幅，夹在 4%~15%。
                # 固定区间对高波动币会频繁穿出，对低波动币又几乎不成交。
                rng = fixed if fixed > 0 else max(0.04, min(0.15, float(s.get("range") or 0.06)))
                out.append({"symbol": s["symbol"], "lower": 0, "upper": 0,
                            "grids": grids, "size_usdt": size, "auto_range_pct": rng})
            return out
        return cfg.get("symbols", [])

    async def _reconcile_status(self, active: set):
        """选清单外的持仓 → draining（只卖不买）；回到清单 → 恢复 active。"""
        for st in self.db.get_grid_state(None) or []:
            sym, status = st["symbol"], st.get("status", "active")
            if sym not in active:
                if status != "draining":
                    self.db.upsert_grid_state(sym, status="draining")
                    log.info(f"GRID {sym}: 掉出选币清单 → 转为「只卖不买」，卖光后自动退役")
            else:
                if status == "draining":
                    self.db.upsert_grid_state(sym, status="active")
                    log.info(f"GRID {sym}: 重回选币清单 → 恢复双向网格")

    # ===== 主循环 =====
    async def step(self):
        cfg = self.cfg
        equity = await self._equity()          # 选币自动上限需要权益
        if cfg.get("auto_select"):
            try:
                await self._refresh_selection(equity)
            except Exception as e:
                log.warning(f"GRID 选币失败，沿用上次结果: {e}")

        specs = self._specs()
        active = {s["symbol"] for s in specs if s.get("symbol")}

        # 处理「掉出名单」：先标记，再继续维护它们的卖单
        await self._reconcile_status(active)

        # 需要维护的标的 = 选中的 + 正在 draining 的（draining 只卖不买）
        manage: list[dict] = list(specs)
        for st in self.db.get_grid_state(None) or []:
            if st["symbol"] not in active:
                manage.append({"symbol": st["symbol"], "lower": float(st["lower"]),
                               "upper": float(st["upper"]), "grids": int(st["grids"]),
                               "size_usdt": float(st["size_usdt"]),
                               "auto_range_pct": float(cfg.get("auto_range_pct", 0.06) or 0.06)})

        symbols = [s["symbol"] for s in manage if s.get("symbol")]
        if not symbols:
            return
        try:
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"GRID 行情刷新失败，跳过本轮: {e}")
            return

        for spec in manage:
            sym = spec.get("symbol")
            if not sym:
                continue
            try:
                await self._step_symbol(spec, equity)
            except Exception as e:
                log.error(f"GRID {sym} 执行异常: {e}")

    async def _equity(self) -> float:
        try:
            b = await self.client.get_futures_balance()
            return float(b.get("balance", 0) or 0)
        except Exception:
            return 0.0

    async def _step_symbol(self, spec: dict, equity: float):
        sym = spec["symbol"]
        price = await self.client.ensure_price(sym)
        if price <= 0:
            return

        lower = float(spec.get("lower") or 0)
        upper = float(spec.get("upper") or 0)
        st = self.db.get_grid_state(sym)

        # 自动区间：只在「首次初始化」时按现价生成，之后沿用已存区间。
        # （否则价格每动一下区间就变，会每 tick 重建网格并重复下单。）
        if lower <= 0 or upper <= lower:
            if st and float(st.get("lower", 0) or 0) > 0 \
                    and float(st.get("upper", 0) or 0) > float(st.get("lower", 0) or 0):
                lower, upper = float(st["lower"]), float(st["upper"])
            else:
                pct = float(spec.get("auto_range_pct") or self.cfg.get("auto_range_pct", 0.25) or 0.25)
                lower, upper = price * (1 - pct), price * (1 + pct)

        grids = int(spec.get("grids") or 0)
        size_usdt = float(spec.get("size_usdt") or 0)
        if size_usdt <= 0:
            return
        # 档位自动推导：档距不该由人随手定，应由**币的波动率**决定。
        # 目标档距 = 日振幅 × step_ratio（默认 0.3），即价格一天内大约穿越
        # 一个档位数次 —— 既不会因档距过大长期等不到平仓，也不会因过小被手续费吃掉。
        # 配置里把 grids 设为 0（或留空）即启用自动推导；填正数则用固定档位。
        if grids < 2:
            pct0 = float(spec.get("auto_range_pct") or self.cfg.get("auto_range_pct", 0) or 0)
            if pct0 <= 0:
                pct0 = self._symbol_range_pct(spec)
            grids = self._derive_grids(pct0)
        if grids < 2:
            return
        need_init = (
            st is None
            or abs(float(st["lower"]) - lower) > 1e-9
            or abs(float(st["upper"]) - upper) > 1e-9
            or int(st["grids"]) != grids
            or abs(float(st["size_usdt"]) - size_usdt) > 1e-9
        )
        # 网格「死态」自愈：初始化时底仓买入失败会留下「无挂单、无持仓」的空壳，
        # 若不重试就永远闲置（资金到位后也不会复活）。检测到空壳则重新初始化，
        # 带 60 秒冷却防止资金不足时每 tick 空转重试。
        if not need_init and st:
            lv_count = len(self.db.get_grid_levels(sym))
            if (lv_count == 0 and float(st.get("base_qty", 0) or 0) <= 1e-12
                    and (st.get("status") or "active") == "active"
                    and await self._reentry_ok(sym, price)):
                last_fail = float(self.db.get_runtime_stat(f"grid_init_fail:{sym}", 0) or 0)
                if time.time() - last_fail >= 60:
                    log.info(f"GRID {sym}: 检测到空壳网格（无挂单无持仓），重新初始化")
                    need_init = True
                else:
                    return

        if need_init:
            # 空仓时的初始化 = 全新建仓（含「掉出名单后被重新选中」的币）。
            # 先过「不追高」闸门：高于上次清仓价就不建仓，等回落。
            flat = (st is None) or float(st.get("base_qty", 0) or 0) <= 1e-12
            if flat and not await self._reentry_ok(sym, price):
                if st is None:
                    return            # 无任何状态可维护，直接等下一轮
                need_init = False     # 保留旧网格（下方买单仍在），继续正常维护
            else:
                await self._init_grid(sym, lower, upper, grids, size_usdt, price)
                st = self.db.get_grid_state(sym)
                if not st:
                    return

        # 与交易所对账持仓数量与成本：交易所为准。
        # 若数量明显不符，说明漏记了成交（如外部干预/离线成交），按交易所均价重置，
        # 否则 base_qty 与 total_cost 不一致会算出错误的均价与盈亏。
        if not self.client.dry_run:
            ex_qty, ex_entry = await self._current_entry(sym)
            db_qty = float(st.get("base_qty", 0) or 0)
            db_cost = float(st.get("total_cost", 0) or 0)
            db_avg = (db_cost / db_qty) if db_qty > 0 else 0
            # 触发条件：数量不符，或均价与交易所偏差超过 0.5%
            qty_bad = ex_qty > 0 and abs(ex_qty - db_qty) >= 1
            avg_bad = (ex_qty > 0 and ex_entry > 0 and db_qty > 0
                       and abs(db_avg - ex_entry) / ex_entry > 0.005)
            if qty_bad or avg_bad:
                self.db.upsert_grid_state(sym, base_qty=ex_qty, total_cost=ex_qty * ex_entry)
                # 账本以交易所为准重建（对账差异说明账本已不可信）
                self.db.reset_lots(sym, ex_qty, ex_entry, "对账重建")
                st = self.db.get_grid_state(sym)
                log.warning(f"GRID {sym}: 持仓对账 数量 {db_qty:.0f}→{ex_qty:.0f} / "
                            f"均价 {db_avg:.6f}→{ex_entry:.6f}，成本已按交易所重算")

        if not self.client.dry_run:
            await self._ensure_sell_coverage(sym, st)

        # 账本兜底：有持仓但账本为空（首次引入该机制时的历史仓位）→ 按均价补记一笔，
        # 否则卖出时无从配对、单笔盈亏会退化成均价口径。
        lq, _ = self.db.lots_qty_cost(sym)
        if lq <= 1e-12 and float(st.get("base_qty", 0) or 0) > 0:
            bq = float(st["base_qty"])
            self.db.reset_lots(sym, bq, float(st.get("total_cost", 0) or 0) / bq, "账本初始补记")
            log.info(f"GRID {sym}: 账本为空，按持仓均价 {float(st.get('total_cost',0) or 0)/bq:.6g} "
                     f"补记 {bq:.0f} 张")

        draining = (st.get("status") or "active") == "draining"
        if draining:
            # 只卖不买：撤掉全部买单（交易所 + DB），只保留覆盖持仓的卖单。
            # 幂等 —— 没有买单时零开销。若不撤，交易所上的买单在价格下跌时
            # 仍会真实成交加仓，与「只卖不买」的语义相悖。
            await self._cancel_buy_orders(sym)
        await self._check_fills(sym, price, draining)
        if draining:
            st2 = self.db.get_grid_state(sym) or {}
            if float(st2.get("base_qty", 0) or 0) <= 1e-12:
                self._retire(sym)
                return
        # 移动止盈：冲高后回撤即卖出剩余，锁住利润（draining 只卖不买同样生效）
        st = self.db.get_grid_state(sym) or st
        if await self._maybe_trail_tp(sym, st, price, spec):
            return
        if not self.client.dry_run:
            if await self._maybe_recenter(sym, st, price, size_usdt, grids, spec, draining):
                st = self.db.get_grid_state(sym)
                if not st:
                    return
                draining = (st.get("status") or "active") == "draining"
            await self._reconcile_orders(sym, price)
            await self._place_live(sym, equity, draining)
            # 高位试空（单币单向：有多单时 _maybe_short 内部自动跳过）
            if not draining:
                try:
                    await self._maybe_short(sym, price)
                except Exception as e:
                    log.error(f"GRID {sym} 空单逻辑异常: {e}")

    async def _cancel_buy_orders(self, sym: str):
        """撤销该标的所有待成交买单（交易所挂单 + DB 档位），卖单不动。

        用于「只卖不买」(draining)：标的掉出选币清单后，不再允许任何新开敞口。
        之前只改状态位不撤单，交易所上的旧买单下跌时仍会真实成交 —— 表面在
        收缩，实际在加仓。卖单覆盖着原仓位，保留不动。
        """
        levels = [l for l in self.db.get_grid_levels(sym)
                  if l["status"] == "pending" and l["side"] == "buy"]
        if not levels:
            return
        for lv in levels:
            if lv.get("order_id") and not self.client.dry_run:
                await self.client.cancel_order(lv["order_id"], sym, "futures")
            self.db.conn.execute("DELETE FROM grid_levels WHERE id=?", (lv["id"],))
        self.db.conn.commit()
        log.info(f"GRID {sym}: 只卖不买 → 已撤销 {len(levels)} 条买单（卖单保留）")

    async def _ensure_sell_coverage(self, sym: str, st: dict):
        """确保卖单总量覆盖当前持仓。

        多头的每一张货都必须有卖单，否则永远卖不掉（会一直沉淀）。
        加仓后卖档不够、或历史遗留的欠覆盖，都在这里自动补齐。
        """
        qty = float(st.get("base_qty", 0) or 0)
        if qty <= 0:
            return
        levels = self.db.get_grid_levels(sym)
        sells = [l for l in levels if l["status"] == "pending" and l["side"] == "sell"]
        covered = sum(float(l["amount"]) for l in sells)
        if covered >= qty - 1e-9:
            return
        short = qty - covered
        s = _throttle.emit(f"grid_cover:{sym}")
        if s is not None:
            log.warning(f"GRID {sym}: 卖单仅覆盖 {covered:.0f}/{qty:.0f} 张 → 补挂 {short:.0f} 张{s}")
        if sells:
            top = max(sells, key=lambda l: float(l["price"]))
            merged = float(top["amount"]) + short
            if not self.client.dry_run and top.get("order_id"):
                await self.client.cancel_order(top["order_id"], sym, "futures")
                res = await self.client.create_limit_order(
                    sym, "sell", merged, float(top["price"]), "futures", position_side="LONG")
                oid = res["order_id"] if res else ""
                self.db.conn.execute("UPDATE grid_levels SET amount=?, order_id=? WHERE id=?",
                                     (merged, oid, top["id"]))
            else:
                self.db.conn.execute("UPDATE grid_levels SET amount=? WHERE id=?", (merged, top["id"]))
            self.db.conn.commit()
        else:
            # 一条卖单都没有：在区间上沿挂一条覆盖全部持仓
            upper = float(st.get("upper", 0) or 0)
            if upper <= 0:
                return
            rp = self.client.round_price(sym, upper, "futures")
            res = await self.client.create_limit_order(sym, "sell", qty, rp, "futures",
                                                       position_side="LONG")
            oid = res["order_id"] if (res and not res.get("dry_run")) else ""
            self.db.insert_grid_level(sym, len(levels), rp, "sell", qty)
            if oid:
                self.db.conn.execute(
                    "UPDATE grid_levels SET order_id=? WHERE symbol=? AND side='sell' AND status='pending'",
                    (oid, sym))
                self.db.conn.commit()

    async def _maybe_recenter(self, sym: str, st: dict, price: float, size_usdt: float,
                              grids: int, spec: dict, draining: bool) -> bool:
        """价格突破区间后让网格跟随（动态居中）。

        规则（安全性优先）：
          · 触发：价格超出区间一定幅度（默认 1%），且有冷却（默认 30 分钟）
          · 允许条件：无持仓，或现价 ≥ 持仓均价（即不在亏损时重排 —— 避免变相止损）
          · draining（只卖不买）也允许：否则手里的存货永远没有卖单可挂
        返回 True 表示已重建。
        """
        cfg = self.cfg
        if not cfg.get("auto_recenter", True):
            return False
        lower, upper = float(st["lower"]), float(st["upper"])
        if lower <= 0 or upper <= lower:
            return False
        margin = float(cfg.get("recenter_margin", 0.01) or 0.01)
        if lower * (1 - margin) <= price <= upper * (1 + margin):
            return False

        key = f"grid_recenter:{sym}"
        cooldown = float(cfg.get("recenter_cooldown_min", 30) or 30) * 60
        last = float(self.db.get_runtime_stat(key, 0) or 0)
        if time.time() - last < cooldown:
            return False

        ex_qty, ex_entry = await self._current_entry(sym)
        if ex_qty > 0 and ex_entry > 0 and price < ex_entry:
            s = _throttle.emit(f"grid_norec:{sym}")
            if s is not None:
                log.warning(f"GRID {sym}: 价格 {price:.6f} 跌破区间且低于成本 {ex_entry:.6f}，"
                            f"按「不设止损」继续持有等待（不重排网格）{s}")
            return False
        # 空仓时向上重排 = 在更高价买回底仓（追高）→ 交给「不追高」闸门裁决，
        # 价格没回落到上次清仓价附近就保持空仓等待。
        if ex_qty <= 0 and price > upper and not await self._reentry_ok(sym, price):
            return False

        pct = float(spec.get("auto_range_pct") or cfg.get("auto_range_pct", 0.06) or 0.06)
        log.warning(f"GRID {sym}: 价格 {price:.6f} 突破区间 {lower:.6f}~{upper:.6f} → "
                    f"网格动态居中到 ±{pct*100:.1f}%")
        self.db.set_runtime_stat(key, time.time())
        await self._init_grid(sym, price * (1 - pct), price * (1 + pct),
                              grids, size_usdt, price, draining)
        return True

    async def _place_entry_orders(self, sym: str, pts: list, price: float,
                                  size_usdt: float, lower: float, upper: float):
        """空仓首次建仓：只用**限价买单**等回落，绝不市价追高。

        三条规则：
          1. 位置闸门 —— 现价在 24h 区间的位置 pos 越低越好；pos > entry_pos_max
             就完全不建仓，等它跌回来。（选币时振幅已很大，但程序不知道现价是在
             这波波动的顶部还是底部，pos 就是这个代理量。）
          2. 挂单深度随 pos 变化 —— pos 越低越贴近现价（越容易成交）；越靠近门槛
             要求越大的折价。总之下单位置越低越好。
          3. 首单只用「最小可下金额」—— 把第一笔的风险压到最小；即使买错，浮亏也小，
             后面的加仓才摊得动成本。
        返回 True 表示已挂出建仓单。
        """
        cfg = self.cfg
        if not cfg.get("entry_limit_only", True):
            return False
        pos = self.client.range_pos(sym)
        pos_max = float(cfg.get("entry_pos_max", 0.6) or 0.6)
        if pos is None:
            self._warn_once(f"grid_nopos:{sym}",
                            f"GRID {sym}: 无 24h 区间数据，暂不建仓（等下一轮）")
            return False
        if pos > pos_max:
            self._warn_once(f"grid_wait:{sym}",
                            f"GRID {sym}: 现价在 24h 区间 {pos*100:.0f}% 位置"
                            f"（门槛 {pos_max*100:.0f}%，越低越好）→ 等待回落再建仓")
            return False

        # 档距直接从已算出的档位反推（相邻档位的价格间距）
        band = d_min = 0.0
        if len(pts) >= 2 and pts[0] > 0:
            band = (pts[-1] / pts[0]) ** (1.0 / (len(pts) - 1)) - 1.0
        d_min = float(cfg.get("entry_min_discount", 0.004) or 0.004)
        if band <= d_min:
            band = d_min
        d = d_min + (band - d_min) * min(1.0, pos / pos_max)
        entry_px = max(pts[0], price * (1 - d))
        entry_px = self.client.round_price(sym, entry_px, "futures")
        if entry_px <= 0 or entry_px >= price:
            return False

        # 首单：最小可下金额（把第一笔风险压到最小）
        a0 = self.client.min_amount(sym, entry_px, "futures")
        if a0 <= 0:
            self._warn_once(f"grid_noentry:{sym}",
                            f"GRID {sym}: 单格资金买不起「一手」，无法挂建仓单")
            return False
        idx0 = min(range(len(pts)), key=lambda i: abs(pts[i] - entry_px))
        self.db.insert_grid_level(sym, idx0, entry_px, "buy", a0)

        # 其余买档：低于建仓价的标准档位，按常规单格金额挂
        for i, p in enumerate(pts):
            if i == idx0 or p >= entry_px * 0.999:
                continue
            rp = self.client.round_price(sym, p, "futures")
            a = self.client.amount_for_notional(sym, size_usdt, rp, "futures")
            if a > 0:
                self.db.insert_grid_level(sym, i, rp, "buy", a)

        prev = self.db.get_grid_state(sym) or {}
        self.db.upsert_grid_state(sym, lower=lower, upper=upper, grids=len(pts),
                                  size_usdt=size_usdt, base_qty=0.0, total_cost=0.0,
                                  realized_pnl=float(prev.get("realized_pnl", 0) or 0),
                                  buy_count=int(prev.get("buy_count", 0) or 0),
                                  sell_count=int(prev.get("sell_count", 0) or 0))
        log.info(f"GRID {sym}: 挂限价建仓单 {a0:.0f} 张 @ {entry_px:.6g}"
                 f"（现价 {price:.6g}，24h 区间 {pos*100:.0f}% 位置，折价 {d*100:.2f}%）")
        return True

    # ===== 高位试空（单次 / 最小仓 / 必带止损 / 不加仓 / 平掉后不再开）=====
    async def _current_short(self, sym: str):
        """返回 (空头张数, 开仓均价)。"""
        try:
            for p in await self.client.get_positions():
                if self.client.user_symbol(p.get("symbol", "")) == sym and p.get("side") == "short":
                    return float(p.get("contracts", 0) or 0), float(p.get("entryPrice", 0) or 0)
        except Exception as e:
            log.warning(f"GRID {sym}: 读取空头持仓失败: {e}")
        return 0.0, 0.0

    async def _maybe_short(self, sym: str, price: float):
        """价格到当日区间顶部时的单次试空。

        用户定的规则：挂单开空、数量最小、只做一单、必带止损、不加仓，
        平掉之后该币不再开空。与多单互斥（同一个币同时只能一个方向）。
        默认关闭（short_enabled=false），需要先用最小仓在实盘验证一次。
        """
        cfg = self.cfg
        if not cfg.get("short_enabled", False):
            return
        if self.db.get_runtime_stat(f"short_done:{sym}", 0) > 0:
            return                                   # 该币的空单额度已用完
        st = self.db.get_grid_state(sym) or {}
        if (st.get("status") or "active") == "draining":
            return                                   # 正在退出，不再新开任何仓位

        # 允许多空同时存在（对冲模式）：做多网格与高位试空互不干扰，各管各的。
        sq, entry = await self._current_short(sym)
        state = int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0)

        if state >= 2 and sq <= 1e-12:
            # 空头已平（止损/止盈/交易所触发）→ 一次性用完，永不再开
            await self._cancel_short_stop(sym)       # 清掉可能残留的止损单
            self.db.set_runtime_stat(f"short_stop_oid:{sym}", 0)
            self.db.set_runtime_stat(f"short_state:{sym}", 0)
            self.db.set_runtime_stat(f"short_done:{sym}", 1)
            log.info(f"GRID {sym}: 空单已平仓 → 该币不再开空")
            return
        if sq > 1e-12:
            await self._manage_short(sym, sq, entry or price, price)
            return
        if state == 1:
            return                                   # 开空单还挂着，等它成交

        # ---- state 0：判断要不要开空 ----
        pos = self.client.range_pos(sym)
        pos_min = float(cfg.get("short_pos_min", 0.85) or 0.85)
        if pos is None or pos < pos_min:
            return
        # 要有"回落确认"：已经离开当日最高点一定幅度，而不是还在往上冲
        hl = self.client._hl.get(sym)
        confirm = float(cfg.get("short_confirm", 0.01) or 0.01)
        if not hl or price > hl[0] * (1 - confirm):
            self._warn_once(f"short_wait:{sym}",
                            f"GRID {sym}: 现价在 24h 区间 {pos*100:.0f}%（高位）但尚未回落确认，等确认再试空")
            return

        offset = float(cfg.get("short_entry_offset", 0.002) or 0.002)
        entry_px = self.client.round_price(sym, price * (1 + offset), "futures")
        amt = self.client.min_amount(sym, entry_px, "futures")     # 数量最小
        if amt <= 0:
            return
        if not await self._exposure_ok(sym, await self._equity()):
            return
        res = await self.client.create_limit_order(sym, "sell", amt, entry_px, "futures",
                                                   position_side="SHORT")
        if not res:
            self._warn_once(f"short_fail:{sym}", f"GRID {sym}: 挂空单失败，下轮重试")
            return
        self.db.set_runtime_stat(f"short_state:{sym}", 1)
        log.warning(f"GRID {sym}: 高位试空 —— 挂空单 {amt:.0f} 张 @ {entry_px:.6g}"
                    f"（24h 区间 {pos*100:.0f}%，单次·最小仓·带止损·不加仓）")

    async def _cancel_short_stop(self, sym: str):
        """撤掉该标的上的条件单（空单的止损单），按类型识别，不依赖记录单号。"""
        try:
            for o in await self.client.fetch_open_orders(sym, "futures"):
                t = str(o.get("type") or "").upper()
                if o.get("side") == "buy" and "STOP" in t:
                    await self.client.cancel_order(str(o.get("id")), sym, "futures")
        except Exception as e:
            log.warning(f"GRID {sym}: 撤止损单失败: {e}")

    async def _manage_short(self, sym: str, qty: float, entry: float, price: float):
        """空头持仓管理：止盈用软件平；止损优先挂交易所条件单，挂不上则软件兜底。"""
        cfg = self.cfg
        sl = float(cfg.get("short_stop_pct", 0.03) or 0.03)
        tp = float(cfg.get("short_tp_pct", 0.03) or 0.03)
        stop_px = entry * (1 + sl)
        tp_px = entry * (1 - tp)
        placed = float(self.db.get_runtime_stat(f"short_stop_oid:{sym}", 0) or 0) > 0

        # 止盈：软件平。先撤掉交易所止损单 —— 否则平完仓后它一旦触发会反手开多。
        if price <= tp_px:
            log.warning(f"GRID {sym}: 空单止盈（现价 {price:.6g} ≤ {tp_px:.6g}）→ 市价平空")
            await self._cancel_short_stop(sym)
            await self.client.create_market_order(sym, "buy", qty, "futures", position_side="SHORT")
            self.db.set_runtime_stat(f"short_stop_oid:{sym}", 0)
            self.db.set_runtime_stat(f"short_state:{sym}", 0)
            self.db.set_runtime_stat(f"short_done:{sym}", 1)
            return

        # 止损：交易所条件单挂上了就靠它；没挂上则软件兜底
        if not placed:
            if price >= stop_px:
                log.warning(f"GRID {sym}: 空单止损（现价 {price:.6g} ≥ {stop_px:.6g}）→ 市价平空")
                await self.client.create_market_order(sym, "buy", qty, "futures", position_side="SHORT")
                self.db.set_runtime_stat(f"short_state:{sym}", 0)
                self.db.set_runtime_stat(f"short_done:{sym}", 1)
                return
            res = await self.client.create_stop_order(sym, "buy", qty, stop_px, position_side="SHORT")
            if res:
                self.db.set_runtime_stat(f"short_stop_oid:{sym}", 1)
                log.info(f"GRID {sym}: 空单交易所止损单已挂（触发价 {stop_px:.6g}）")
            else:
                self._warn_once(f"short_nostop:{sym}",
                                f"GRID {sym}: 交易所止损单挂不上 → 改用软件止损（{stop_px:.6g}）")

    async def _maybe_trail_tp(self, sym: str, st: dict, price: float, spec: dict) -> bool:
        """移动止盈：浮盈冲高后回撤 → 卖出剩余仓位，把利润落袋。

        解决「第一次平仓后继续冲高、还没到第二个平仓位就掉头」——剩余仓位
        本该等下一个卖档，结果一路下跌把利润吐光的问题。规则：
          · 武装：浮盈 ≥ trail_arm_bands 个档距时开始记录最高价
          · 触发：价格自最高价回撤 ≥ trail_bands 个档距
          · 下限：卖出价 ≥ 成本×(1+min_tp_ratio)；跌破成本则【一动不动】
            （所以它只会止盈、绝不割肉，与「不设止损」原则一致）
        触发后清掉卖档、保留买档 —— 之后价格回落时仍能靠买档重新低吸。
        draining（只卖不买）同样生效。返回 True 表示本次已清仓。
        """
        cfg = self.cfg
        if not cfg.get("trail_tp", True):
            return False
        qty = float(st.get("base_qty", 0) or 0)
        cost = float(st.get("total_cost", 0) or 0)
        if qty <= 0 or cost <= 0:
            self.db.set_runtime_stat(f"tp_high:{sym}", 0)
            return False
        avg = cost / qty
        min_tp = float(cfg.get("min_tp_ratio", 0.002) or 0)
        floor = avg * (1 + min_tp)
        pct = float(spec.get("auto_range_pct") or cfg.get("auto_range_pct", 0) or 0)
        if pct <= 0:
            pct = self._symbol_range_pct(spec)
        band = self._band_pct(pct)
        arm = max(0.0, float(cfg.get("trail_arm_bands", 0.5) or 0)) * band
        trail = max(0.0, float(cfg.get("trail_bands", 1.0) or 0)) * band
        if trail <= 0:
            return False
        high = float(self.db.get_runtime_stat(f"tp_high:{sym}", 0) or 0)
        # 只有已获利才记录高点（下跌途中绝不武装，避免变成变相止损）
        if price >= avg * (1 + max(arm, min_tp)):
            high = max(high, price)
            self.db.set_runtime_stat(f"tp_high:{sym}", high)
        if high <= 0 or price > high * (1 - trail) or price < floor:
            return False

        s = _throttle.emit(f"grid_trail:{sym}")
        if s is not None:
            log.warning(f"GRID {sym}: 移动止盈触发 —— 高点 {high:.6g} 回撤 "
                        f"{trail*100:.2f}% 至 {price:.6g}（成本 {avg:.6g}）→ "
                        f"卖出剩余 {qty:.0f} 张{s}")
        # 先撤掉全部卖单（否则会重复卖出），保留买档供回落时重新低吸
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending" or lv["side"] != "sell":
                continue
            if lv.get("order_id"):
                await self.client.cancel_order(lv["order_id"], sym, "futures")
            self.db.conn.execute("DELETE FROM grid_levels WHERE id=?", (lv["id"],))
        self.db.conn.commit()
        res = await self.client.create_market_order(
            sym, "sell", qty, "futures", position_side="LONG")
        if not res:
            log.error(f"GRID {sym}: 移动止盈卖出失败，状态保留，下一轮重试")
            return False
        fill = float(res.get("price") or price)
        # 按账本核销全部批次，逐笔加总真实盈亏（与 _on_fill 同一口径）
        consumed = self.db.consume_lots(sym, fill, qty)
        taken = sum(t for _, t in consumed)
        trade_pnl = sum((fill - lp) * t for lp, t in consumed)
        if taken < qty - 1e-9:            # 账本不足 → 余量按均价兜底
            trade_pnl += (fill - avg) * (qty - taken)
        self.db.clear_lots(sym)
        self.db.upsert_grid_state(
            sym, base_qty=0.0, total_cost=0.0,
            realized_pnl=float(st.get("realized_pnl", 0) or 0) + trade_pnl,
            sell_count=int(st.get("sell_count", 0) or 0) + 1)
        self.db.insert_order({
            "mode": "grid", "symbol": sym, "side": "sell", "type": "grid_trail",
            "price": fill, "amount": qty, "cost": round(fill * qty, 6),
            "fee": round(fill * qty * 0.0005, 6),
            "pnl": round(trade_pnl, 6),
            "pnl_rate": round(trade_pnl / (avg * qty), 8) if avg * qty > 0 else 0,
            "dry_run": self.client.dry_run, "note": "移动止盈"})
        # 记锚点：之后「不追高」，须等价格回落到该价位附近才允许重新建仓
        self.db.set_runtime_stat(f"grid_exit:{sym}", fill)
        self.db.set_runtime_stat(f"tp_high:{sym}", 0)
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.info(f"{tag} GRID {sym}: 移动止盈清仓 {qty:.0f} 张 @ {fill:.6g}，"
                 f"本笔盈亏 {trade_pnl:+.4f}U")
        return True

    async def _current_entry(self, sym: str):
        """返回 (持仓张数, 开仓均价)；无持仓返回 (0, 0)。"""
        try:
            for p in await self.client.get_positions():
                if self.client.user_symbol(p.get("symbol", "")) == sym and p.get("side") == "long":
                    return float(p.get("contracts", 0) or 0), float(p.get("entryPrice", 0) or 0)
        except Exception as e:
            log.warning(f"GRID {sym}: 读取持仓失败: {e}")
        return 0.0, 0.0

    async def _current_position(self, sym: str) -> float:
        """该标的当前的多头持仓张数（重建网格时用来算差额，避免重复买入）。"""
        try:
            for p in await self.client.get_positions():
                if self.client.user_symbol(p.get("symbol", "")) == sym and p.get("side") == "long":
                    return float(p.get("contracts", 0) or 0)
        except Exception as e:
            log.warning(f"GRID {sym}: 读取持仓失败: {e}")
        return 0.0

    # ===== 初始化 =====
    async def _init_grid(self, sym: str, lower: float, upper: float,
                         grids: int, size_usdt: float, price: float,
                         draining: bool = False):
        pts = grid_points(lower, upper, grids)
        if not pts:
            return
        # 全新建仓前先过敞口闸门：资金不够就直接不建（连 grid_state 记录都不留），
        # 否则会留下「无挂单无持仓」的空壳，被自愈逻辑每 60 秒重试一次、刷日志。
        # 已有持仓的重建不受此限（下面还有按 delta 的增量闸门）。
        if not self.client.dry_run and self.db.get_grid_state(sym) is None:
            if not await self._exposure_ok(sym, await self._equity()):
                return
        # 实盘：先把该标的设为逐仓并设好杠杆，再挂单
        # （逐仓 = 单仓爆仓只损失该仓保证金，不牵连整个账户）
        if not self.client.dry_run:
            await self.client.set_margin_mode(sym, "isolated")
            await self.client.set_leverage(sym, int(self.cfg.get("leverage", 3) or 3))
        # 实盘：重建前先撤掉交易所上的旧挂单，避免留下孤儿单
        if not self.client.dry_run:
            for lv in self.db.get_grid_levels(sym):
                if lv["status"] == "pending" and lv.get("order_id"):
                    await self.client.cancel_order(lv["order_id"], sym, "futures")

        half = max(1, grids // 2)
        below = [p for p in pts if p < price][-half:]      # 现价下方最近的 half 档
        above = [p for p in pts if p > price][:half]       # 现价上方最近的 half 档

        # 不设止损：若已持仓，卖价永不低于持仓均价（决不亏损卖出）
        ex_qty0, ex_entry0 = (await self._current_entry(sym)) if not self.client.dry_run else (0.0, 0.0)
        min_tp = float(self.cfg.get("min_tp_ratio", 0.002) or 0)
        min_sell = (ex_entry0 * (1 + min_tp)) if (ex_qty0 > 0 and ex_entry0 > 0) else 0.0

        # ---- 空仓首次建仓：不市价买底仓，改为挂限价买单等回落 ----
        # （市价买 = 在"现价"这个未知高低的点位上砸进最大一笔，是亏损之源）
        lq0, _ = self.db.lots_qty_cost(sym)
        if ex_qty0 <= 1e-12 and lq0 <= 1e-12 and not draining:
            if self.cfg.get("entry_limit_only", True):
                await self._place_entry_orders(sym, pts, price, size_usdt, lower, upper)
                return
            # 关掉择时开关时，退回旧的「市价买底仓」行为（见下方 sells/base 逻辑）

        # ---- 已持仓 / draining：重建档位 ----
        # 先清空旧档位；但**保留已实现盈亏与买卖计数**——重建网格（如动态跟随）
        # 不能把历史收益抹掉。
        prev = self.db.get_grid_state(sym) or {}
        self.db.replace_grid_levels(sym, [])
        # 持仓以账本为准（不要硬写 0.0 —— 万一后面提前返回，持仓记录会
        # 与交易所脱节，卖单覆盖也随之失效）。
        _lq, _lc = self.db.lots_qty_cost(sym)
        self.db.upsert_grid_state(sym, lower=lower, upper=upper, grids=grids,
                                  size_usdt=size_usdt, base_qty=_lq, total_cost=_lc,
                                  realized_pnl=float(prev.get("realized_pnl", 0) or 0),
                                  buy_count=int(prev.get("buy_count", 0) or 0),
                                  sell_count=int(prev.get("sell_count", 0) or 0))

        sells = []   # [[level_idx, price, amount], ...]
        for i, p in enumerate(pts):
            if p > price and p in above:
                if min_sell > 0 and p < min_sell * 0.999:
                    continue          # 低于成本价，不挂卖（不亏损卖出）
                a = self.client.amount_for_notional(sym, size_usdt, p, "futures")
                if a > 0:
                    sells.append([i, p, a])

        created = 0
        base_amt = 0.0
        if sells:
            base_amt = sum(s[2] for s in sells)

            # 底仓必须严格等于卖单总量，否则会沉淀出卖不掉的余量。
            # 若按现价买入达不到最小名义值，就把缺口加在「最靠近现价的卖单」上，
            # 而不是多买底仓（多买的部分没有卖单覆盖，会永远沉淀）。
            min_now = self.client.min_amount(sym, price, "futures")
            if min_now > base_amt:
                sells[-1][2] += (min_now - base_amt)
                base_amt = min_now
            if draining and ex_qty0 > 0:
                # 只卖不买：目标持仓 = 现有持仓，不额外买入；
                # 并确保卖单覆盖全部持仓（否则有张数永远卖不掉）
                base_amt = ex_qty0
                covered = sum(x[2] for x in sells)
                if sells and covered < ex_qty0:
                    sells[-1][2] += (ex_qty0 - covered)
            if base_amt <= 0:
                need = self.client.min_notional_of(sym, "futures")
                s = _throttle.emit(f"grid_nocap:{sym}")
                if s is not None:
                    log.warning(f"GRID {sym}: 单格 {size_usdt:.2f}U 买不起「一手」"
                                f"（最小约 {need:.1f}U/手）→ 跳过。请加大单格金额或换低价标的{s}")
                return

            # 底仓：实盘按「实际持仓 → 目标量」的差额调整
            # （重建网格时不能无脑再买一次，否则仓位会翻倍）
            fill = price
            traded_side, traded_amt = "buy", base_amt      # 模拟盘视为买入底仓
            if not self.client.dry_run:
                cur = await self._current_position(sym)
                delta = base_amt - cur
                # 建仓也要过敞口闸门：多标的并行时，防止一次性把敞口打满
                if delta > 0 and not await self._exposure_ok(sym, await self._equity()):
                    return
                if abs(delta) >= 1:
                    traded_side = "buy" if delta > 0 else "sell"
                    traded_amt = abs(delta)
                    res = await self.client.create_market_order(
                        sym, traded_side, traded_amt, "futures", position_side="LONG")
                    if not res:
                        log.error(f"GRID {sym}: 底仓调整失败({traded_side} {traded_amt:.0f})，放弃本次初始化")
                        return
                    fill = float(res.get("price") or price)
                    log.info(f"GRID {sym}: 底仓调整 {cur:.0f} → {base_amt:.0f} ({traded_side} {traded_amt:.0f})")
                else:
                    traded_amt = 0.0
                    if cur > 0:
                        fill = await self.client.ensure_price(sym) or price
            # 记录「实际成交的方向与数量」，不要硬编码成 buy / base_amt
            if traded_amt > 0:
                self.db.insert_order({
                    "mode": "grid", "symbol": sym, "side": traded_side, "type": "grid_base",
                    "price": fill, "amount": traded_amt, "cost": round(traded_amt * fill, 6),
                    "fee": round(traded_amt * fill * 0.0005, 6),
                    "dry_run": self.client.dry_run,
                    "note": "网格底仓" if traded_side == "buy" else "网格底仓调整",
                })
            # 账本对齐到目标持仓：多则核销、少则补记（对重建/历史遗留都自愈）
            lq, _ = self.db.lots_qty_cost(sym)
            diff = base_amt - lq
            if diff > 1e-9:
                self.db.add_lot(sym, fill, diff, "网格底仓")
            elif diff < -1e-9:
                self.db.consume_lots(sym, fill, -diff)
            qty0, cost0 = self.db.lots_qty_cost(sym)
            self.db.upsert_grid_state(sym, base_qty=qty0, total_cost=cost0)
            # 建仓完成 → 消费掉「不追高」锚点：本轮重新入场已落地，
            # 下一次清仓时再记录新锚点。
            self.db.set_runtime_stat(f"grid_exit:{sym}", 0)
            self.db.set_runtime_stat(f"tp_high:{sym}", 0)
            created += 1

        # 挂单：买单（现价下方）+ 卖单（用上面算好的量，保证底仓=卖单总量）
        sell_idx = {s[0]: s for s in sells}
        for i, p in enumerate(pts):
            rp = self.client.round_price(sym, p, "futures")
            if p < price and p in below:
                if draining:
                    continue          # 只卖不买：不挂买档
                a = self.client.amount_for_notional(sym, size_usdt, p, "futures")
                if a > 0:
                    self.db.insert_grid_level(sym, i, rp, "buy", a)
                    created += 1
            elif i in sell_idx:
                self.db.insert_grid_level(sym, i, rp, "sell", sell_idx[i][2])
                created += 1
        log.info(f"GRID {sym}: 区间 {lower:.6g}~{upper:.6g} / {grids} 档 / 单格 {size_usdt}U "
                 f"/ 底仓 {base_amt:.0f} / 卖单合计 {sum(s[2] for s in sells):.0f} "
                 f"/ 挂单 {created - (1 if sells else 0)} 条 (现价 {price:.6g})")

    # ===== 成交检测 =====
    async def _check_fills(self, sym: str, price: float, draining: bool = False):
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending":
                continue
            # 实盘：只认「交易所上确实挂着」的档位（有 order_id）。
            # 没有 order_id 说明这档从没挂成功（保证金不足等），若仅凭价格触达
            # 就记成交，会产生**幽灵成交**——账面凭空多出一笔买卖，交易所却没有。
            if not self.client.dry_run and not lv.get("order_id"):
                continue
            side, op = lv["side"], float(lv["price"])
            if side == "buy":
                if draining:
                    continue          # 只卖不买：买单即使被触及也不成交
                if price <= op:
                    await self._on_fill(sym, lv, "buy", op, price, draining)
            elif side == "sell" and price >= op:
                await self._on_fill(sym, lv, "sell", op, price, draining)

    async def _on_fill(self, sym: str, lv: dict, side: str, fill_price: float,
                       market: float, draining: bool = False):
        amt = float(lv["amount"])
        st = self.db.get_grid_state(sym) or {}
        realized = float(st.get("realized_pnl", 0) or 0)

        self.db.fill_grid_level(lv["id"])
        trade_pnl = 0.0
        trade_rate = 0.0
        if side == "buy":
            # 每一笔买入单独记一条账本（开仓/加仓都是），供卖出时按价配对核销
            self.db.add_lot(sym, fill_price, amt, "网格买入")
            self.db.upsert_grid_state(sym, buy_count=int(st.get("buy_count", 0) or 0) + 1)
            # 加仓后立即重置移动止盈的最高价：这是新的一轮持仓，
            # 旧的高点会让机制在刚加完仓就误触发。
            self.db.set_runtime_stat(f"tp_high:{sym}", 0)
        else:
            # 按「低于卖价者中价格最高优先」核销买入批次 —— 对应网格里下一档
            # 买入，故单笔必然盈利（卖价 > 该笔买入价）。
            consumed = self.db.consume_lots(sym, fill_price, amt)
            taken = sum(t for _, t in consumed)
            trade_pnl = sum((fill_price - lp) * t for lp, t in consumed)
            trade_base = sum(lp * t for lp, t in consumed)
            if taken < amt - 1e-9:
                # 账本不足（历史遗留/对账差）：余量按原持仓均价兜底，不丢数
                db_qty = float(st.get("base_qty", 0) or 0)
                db_cost = float(st.get("total_cost", 0) or 0)
                avg_fb = (db_cost / db_qty) if db_qty > 0 else fill_price
                rest = amt - taken
                trade_pnl += (fill_price - avg_fb) * rest
                trade_base += avg_fb * rest
            realized += trade_pnl
            trade_rate = (trade_pnl / trade_base) if trade_base > 0 else 0.0
            self.db.upsert_grid_state(sym, realized_pnl=realized,
                                      sell_count=int(st.get("sell_count", 0) or 0) + 1)

        # 持仓数量与成本统一从账本重算（账本是唯一真源）
        qty, cost = self.db.lots_qty_cost(sym)
        if qty <= 1e-12:
            qty, cost = 0.0, 0.0
            if side == "sell":
                # 清仓 = 网格在本轮最高卖点退出。记锚点：之后只有价格回落到
                # 它附近或更低，才允许重新建仓（不追高）。
                self.db.set_runtime_stat(f"grid_exit:{sym}", fill_price)
        self.db.upsert_grid_state(sym, base_qty=qty, total_cost=cost)

        self.db.insert_order({
            "mode": "grid", "symbol": sym, "side": side, "type": f"grid_{side}",
            "price": fill_price, "amount": amt, "cost": round(fill_price * amt, 6),
            "fee": round(fill_price * amt * 0.0005, 6),
            "pnl": round(trade_pnl, 6), "pnl_rate": round(trade_rate, 8),
            "dry_run": self.client.dry_run,
            "note": "网格卖出" if side == "sell" else "网格买入",
        })
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.info(f"{tag} GRID {sym} {side} {amt} @ {fill_price:.6g}")

        # 生成反向单（买→上一档卖；卖→下一档买）
        await self._arm_next(sym, side, fill_price, draining)

    def _levels_of(self, sym: str) -> list[float]:
        st = self.db.get_grid_state(sym)
        if not st:
            return []
        return grid_points(float(st["lower"]), float(st["upper"]), int(st["grids"]))

    def _find_pending(self, sym: str, price: float, side: str):
        """找同价同向的待成交挂单（用于去重/累加）。"""
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending" or lv["side"] != side:
                continue
            if abs(float(lv["price"]) - price) <= max(1e-9, price * 1e-6):
                return lv
        return None

    async def _arm_next(self, sym: str, filled_side: str, fill_price: float,
                        draining: bool = False):
        st = self.db.get_grid_state(sym) or {}
        size = float(st.get("size_usdt", 0) or 0)
        if size <= 0:
            return
        pts = self._levels_of(sym)
        if not pts:
            return
        if filled_side == "buy":
            cand = [p for p in pts if p > fill_price * 1.0001]
            want = cand[0] if cand else fill_price * ((pts[-1] / pts[0]) ** (1.0 / max(1, len(pts) - 1)))
            side = "sell"
        else:
            cand = [p for p in pts if p < fill_price * 0.9999]
            want = cand[-1] if cand else fill_price / ((pts[-1] / pts[0]) ** (1.0 / max(1, len(pts) - 1)))
            side = "buy"
        if draining and side == "buy":
            return  # 已掉出清单：不再挂买单，只把存货卖掉
        rp = self.client.round_price(sym, want, "futures")
        amt = self.client.amount_for_notional(sym, size, rp, "futures")
        if amt <= 0:
            return
        # 关键：该档位可能已有一条同向挂单（如已有卖单）。
        # 不能直接跳过 —— 否则新买入的那批货永远没有卖单覆盖。
        # 正确做法是把数量累加到已有挂单上（实盘需先撤旧单再按新量重挂）。
        dup = self._find_pending(sym, rp, side)
        if dup:
            merged = float(dup["amount"]) + amt
            if not self.client.dry_run and dup.get("order_id"):
                await self.client.cancel_order(dup["order_id"], sym, "futures")
                res = await self.client.create_limit_order(sym, side, merged, rp, "futures",
                                                           position_side="LONG")
                oid = res["order_id"] if res else ""
                self.db.conn.execute(
                    "UPDATE grid_levels SET amount=?, order_id=? WHERE id=?",
                    (merged, oid, dup["id"]))
                self.db.conn.commit()
            else:
                self.db.conn.execute("UPDATE grid_levels SET amount=? WHERE id=?",
                                     (merged, dup["id"]))
                self.db.conn.commit()
            s = _throttle.emit(f"grid_merge:{sym}")
            if s is not None:
                log.info(f"GRID {sym}: {side} 档位 {rp:.6f} 累加数量 "
                         f"{dup['amount']:.0f} → {merged:.0f}（覆盖新买入的持仓）{s}")
            return
        idx = min(range(len(pts)), key=lambda i: abs(pts[i] - want))
        self.db.insert_grid_level(sym, idx, rp, side, amt)

    # ===== 挂单对账 =====
    async def _reconcile_orders(self, sym: str, price: float):
        """核对 DB 里的挂单号是否仍存在于交易所。

        外部撤单 / 手工干预 / 订单过期时，DB 仍存着 order_id，而 _place_live 见
        「有单号」就跳过 → 那一侧永远不会补挂，网格会悄悄缺一条腿。这里每轮核对。
        """
        levels = [lv for lv in self.db.get_grid_levels(sym)
                  if lv["status"] == "pending" and lv.get("order_id")]
        if not levels:
            return
        try:
            open_orders = await self.client.fetch_open_orders(sym, "futures")
        except Exception:
            return
        live = {str(o.get("id")) for o in open_orders}
        for lv in levels:
            if str(lv["order_id"]) in live:
                continue
            st = await self.client.fetch_order_status(lv["order_id"], sym, "futures")
            if st == "closed":
                # 已成交（可能是离线期间成交的）→ 补记成交
                await self._on_fill(sym, lv, lv["side"], float(lv["price"]), price, False)
            else:
                s = _throttle.emit(f"grid_stale:{sym}")
                if s is not None:
                    log.warning(f"GRID {sym}: 挂单 {lv['order_id']} 已不在交易所"
                                f"(状态={st}) → 清空并重新挂单{s}")
                self.db.clear_grid_order_id(lv["id"])

    # ===== 实盘挂单 =====
    async def _place_live(self, sym: str, equity: float, draining: bool = False):
        # 先取交易所实际挂单：DB 若丢了单号（如从备份恢复），
        # 直接按「同向+同价」认领，避免重复挂单（重复挂会因超出持仓被拒 -2022）。
        try:
            live = {}
            for o in await self.client.fetch_open_orders(sym, "futures"):
                live[(str(o.get("side")), round(float(o.get("price") or 0), 8))] = o
        except Exception:
            live = {}

        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending" or lv["order_id"]:
                continue
            key = (lv["side"], round(float(lv["price"]), 8))
            if key in live:
                o = live[key]
                self.db.conn.execute(
                    "UPDATE grid_levels SET order_id=?, amount=? WHERE id=?",
                    (str(o.get("id")), float(o.get("amount") or lv["amount"]), lv["id"]))
                self.db.conn.commit()
                log.info(f"GRID {sym}: 认领交易所已有挂单 {lv['side']} {o.get('amount')}@{o.get('price')}")
                continue
            if lv["side"] == "buy":
                if draining:
                    continue          # 收缩中：不挂买单
                if not await self._still_qualified(sym):
                    continue          # 补仓前复核入选条件（流动性/波动率/状态）
                if not await self._exposure_ok(sym, equity):
                    continue
                if not await self._has_free_margin(sym, lv):
                    continue
            res = await self.client.create_limit_order(
                sym, lv["side"], float(lv["amount"]), float(lv["price"]), "futures",
                position_side="LONG")
            if res:
                self.db.conn.execute("UPDATE grid_levels SET order_id=? WHERE id=?",
                                     (res["order_id"], lv["id"]))
                self.db.conn.commit()

    async def _still_qualified(self, sym: str) -> bool:
        """补仓/再买入前复核该标的是否仍满足入选条件（30 分钟缓存）。

        选币 6 小时才刷新一次；若在这期间标的流动性枯竭或状态异常，
        不应继续买入（避免在已失去交易价值的币上越买越多）。
        """
        cfg = self.cfg
        if not cfg.get("auto_select"):
            return True                      # 手动模式：用户自己负责
        key = f"grid_qual:{sym}"
        last = float(self.db.get_runtime_stat(key, 0) or 0)
        if time.time() - last < 1800:        # 30 分钟缓存
            return float(self.db.get_runtime_stat(f"grid_qual_ok:{sym}", 1) or 1) > 0
        ok, why = await qualify_symbol(self.client, cfg, sym)
        self.db.set_runtime_stat(key, time.time())
        self.db.set_runtime_stat(f"grid_qual_ok:{sym}", 1 if ok else 0)
        if not ok:
            s = _throttle.emit(f"grid_unqual:{sym}")
            if s is not None:
                log.warning(f"GRID {sym}: 已不再满足入选条件（{why}）→ 暂停买入{s}")
        return ok

    async def _reentry_ok(self, sym: str, price: float) -> bool:
        """「不追高」闸门：空仓后重新建仓前的价格复核。

        场景：价格向上突破区间 → 卖单全部成交 → 空仓。此时若按「动态跟随」
        在现价重新布网，就会在**比刚卖出更高的价位**买回底仓；一旦回落立刻
        由盈转亏。网格的利润来自低买高卖，所以清仓后不该追高。

        规则：以**上次清仓价**为锚，只有价格回落到 锚 × (1 + reentry_tol) 以内
        才允许重新建仓；否则保持空仓等待（不追高、也不摊平）。首次交易
        （无锚点）不受限制。成功建仓后锚点清零，等待下一次清仓重新记录。
        """
        anchor = float(self.db.get_runtime_stat(f"grid_exit:{sym}", 0) or 0)
        if anchor <= 0:
            return True
        tol = float(self.cfg.get("reentry_tol", 0.003) or 0)
        if price <= anchor * (1 + tol):
            return True
        self._warn_once(
            f"grid_chase:{sym}",
            f"GRID {sym}: 上次清仓价 {anchor:.6g}，现价 {price:.6g} 更高 → "
            f"不追高，空仓等待回落")
        return False

    async def _has_free_margin(self, sym: str, lv: dict) -> bool:
        """挂买单前检查可用保证金，不够就跳过（否则每次都会 -2019 刷屏）。"""
        try:
            b = await self.client.get_futures_balance()
            free = float(b.get("available", 0) or 0)
        except Exception:
            return True
        lev = max(1, int(self.cfg.get("leverage", 3) or 3))
        need = float(lv["amount"]) * float(lv["price"]) / lev
        if free < need:
            s = _throttle.emit(f"grid_margin:{sym}")
            if s is not None:
                log.warning(f"GRID {sym}: 可用保证金 {free:.2f}U < 买单需 {need:.2f}U，暂不挂买单{s}")
            return False
        return True

    async def _exposure_ok(self, sym: str, equity: float) -> bool:
        ratio = float(self.cfg.get("max_exposure_ratio", 3.0) or 3.0)
        if ratio <= 0:
            return True
        if equity <= 0:
            self._warn_once(f"grid_noeq:{sym}", "GRID 权益为 0，暂停买入")
            return False
        try:
            positions = await self.client.get_positions()
        except Exception:
            return True
        total = 0.0
        for p in positions:
            us = self.client.user_symbol(p.get("symbol", ""))
            mk = float(p.get("markPrice", 0) or p.get("entryPrice", 0) or 0)
            c = float(p.get("contracts", 0) or 0)
            cs = float(self.client.market_info(us, "futures").get("contractSize", 1) or 1)
            total += mk * c * cs
        if total / equity >= ratio:
            self._warn_once(f"grid_cap:{sym}",
                            f"GRID 敞口闸门：总名义 {total:.1f}U / 权益 {equity:.1f}U ≥ {ratio:.0f}倍，暂停买入")
            return False
        return True

    @staticmethod
    def _warn_once(key: str, msg: str):
        s = _throttle.emit(key)
        if s is not None:
            log.warning(msg + s)

    def _retire(self, sym: str):
        """draining 且已卖光 → 从管理清单移除。"""
        self.db.replace_grid_levels(sym, [])
        self.db.conn.execute("DELETE FROM grid_state WHERE symbol=?", (sym,))
        self.db.conn.commit()
        self.db.clear_lots(sym)
        self.db.set_runtime_stat(f"tp_high:{sym}", 0)
        log.info(f"GRID {sym}: 存货已清空，网格退役（不再纳入管理）")

    # ===== 前端状态 =====
    async def status(self) -> dict:
        # 读取交易所持仓元数据（强平价 / 杠杆 / 保证金模式）—— 安全信息，必须可见
        meta = {}
        try:
            for p in await self.client.get_positions():
                us = self.client.user_symbol(p.get("symbol", ""))
                meta[us] = {
                    "liq_price": float(p.get("liquidationPrice", 0) or 0),
                    "leverage": p.get("leverage") or self.cfg.get("leverage"),
                    "margin_mode": p.get("marginMode") or "",
                    "unrealized": float(p.get("unrealizedPnl", 0) or 0),
                    "mark": float(p.get("markPrice", 0) or 0),
                }
        except Exception as e:
            log.warning(f"读取持仓元数据失败: {e}")

        out = []
        for st in self.db.get_grid_state(None) or []:
            sym = st["symbol"]
            price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            qty = float(st.get("base_qty", 0) or 0)
            cost = float(st.get("total_cost", 0) or 0)
            avg = cost / qty if qty > 0 else 0
            lv = self.db.get_grid_levels(sym)
            out.append({
                "symbol": sym,
                "lower": st["lower"], "upper": st["upper"], "grids": st["grids"],
                "size_usdt": st["size_usdt"], "price": price,
                "base_qty": qty, "avg_price": round(avg, 6),
                "position_value": round(qty * price, 4),
                "cost_value": round(cost, 4),
                # 浮盈优先用交易所权威值（与你在币安看到的完全一致），拿不到才自算
                "unrealized_pnl": round(
                    (meta.get(sym) or {}).get("unrealized")
                    if (meta.get(sym) or {}).get("unrealized") is not None and qty > 0
                    else ((price - avg) * qty if qty > 0 else 0), 4),
                "unrealized_pnl_rate": round((price - avg) / avg, 6) if avg > 0 else 0,
                "realized_pnl": round(float(st.get("realized_pnl", 0) or 0), 6),
                "realized_pnl_rate": round(float(st.get("realized_pnl", 0) or 0) / cost, 6) if cost > 0 else 0,
                "status": st.get("status", "active"),
                "leverage": (meta.get(sym) or {}).get("leverage"),
                "margin_mode": (meta.get(sym) or {}).get("margin_mode", ""),
                "liq_price": (meta.get(sym) or {}).get("liq_price", 0),
                "liq_distance": round(
                    ((meta.get(sym) or {}).get("liq_price", 0) - price) / price, 4)
                    if (meta.get(sym) or {}).get("liq_price", 0) > 0 and price > 0 else None,
                "buy_count": st.get("buy_count", 0), "sell_count": st.get("sell_count", 0),
                "levels": [
                    {"idx": l["level_idx"], "price": l["price"], "side": l["side"],
                     "amount": l["amount"], "status": l["status"]}
                    for l in lv if l["status"] == "pending"
                ],
                # 逐笔持仓账本：每一笔未卖出的买入，各自成本与浮盈（前端可展开）
                "lots": [
                    {"price": round(l["price"], 8), "qty": l["qty"],
                     "remaining": l["remaining"], "ts": l["ts"], "note": l["note"],
                     "unrealized": round((price - l["price"]) * l["remaining"], 6),
                     "unrealized_rate": round((price - l["price"]) / l["price"], 6)
                     if l["price"] > 0 else 0}
                    for l in self.db.get_lots(sym)
                ],
            })
        cfg = self.cfg
        n = int(cfg.get("max_symbols", 0) or 0)          # 0 = 按资金自动
        sel_all = list(self._selection or (self.config.get("grid_selection") or []))
        sel = sel_all[:n] if n > 0 else sel_all          # 自动时不截断展示
        return {
            "symbols": out,
            "auto_select": bool(cfg.get("auto_select")),
            "selection": [{"symbol": s["symbol"], "range": s.get("range"),
                           "quote_volume": s.get("quote_volume")} for s in sel],
            "max_symbols": n,                             # 0 表示自动
            "max_symbols_effective": len(sel),
        }
