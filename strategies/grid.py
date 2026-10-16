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
from datetime import datetime, timezone

import params
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
        return max(params.RANGE["min"],
                   min(params.RANGE["max"], float((pick or {}).get("range") or params.RANGE["default"])))

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
            # 沿用已落库的选币结果，但按当前上限截断。
            # 名额出现空位（上调了 max_symbols / 有标的被顶掉）时**不要**提前
            # 返回 —— 立即走完整选币把空位补上，否则调大名额要干等下一个周期。
            saved = list(self.config.get("grid_selection") or [])
            limit = n if n > 0 else self._auto_symbol_cap(equity, saved)
            saved = saved[:limit] if limit > 0 else []
            if saved and len(saved) >= limit:
                self._selection = saved
                self._sel_ts = ts
                return

        size = float(cfg.get("size_usdt", 0) or 0)
        # 单标的能承受的名义价值上限 = 权益 × 敞口上限 / 名额。
        # 用来把「最小一手就买不起」的币挡在名单外（ZECUSDT 一手 1089U 而总资金 30U）。
        slots = n if n > 0 else max(1, len(self.config.get("grid_selection") or []) or 5)
        ratio = float(cfg.get("max_exposure_ratio", 3) or 3)
        budget = (equity * ratio / slots) if equity > 0 else 0.0
        picks = await select_grid_symbols(
            self.client, cfg, size, n if n > 0 else 999, budget)
        if not picks:
            return
        limit = max(1, n if n > 0 else self._auto_symbol_cap(equity, picks))

        # 选币器返回的是**全部合格标的**（按分数排序）。
        # 粘性只保护「有持仓」的标的 —— 持仓网格被踢来踢去会抖散；
        # 空仓待机的没有沉没成本，按分数公平竞争，否则名额只有 1 个时
        # 先来的币会永远占位，新的好机会永远进不来（实测发生过）。
        prev_syms = [s.get("symbol") for s in (self.config.get("grid_selection") or [])
                     if s.get("symbol")]
        by_sym = {p["symbol"]: p for p in picks}
        keep = []
        for s in prev_syms:
            if len(keep) >= limit:
                break
            if s not in by_sym:
                continue                      # 不再合格 → 让位（draining 机制收尾）
            st = self.db.get_grid_state(s)
            has_long = st and float(st.get("base_qty", 0) or 0) > 1e-12
            # 有空单（挂单中/持仓中）也不能顶掉 —— 否则空单失去管理（止盈没人做）
            has_short = int(self.db.get_runtime_stat(f"short_state:{s}", 0) or 0) in (1, 2)
            if has_long or has_short:
                keep.append(by_sym[s])        # 有仓在身 → 粘住
        keepset = {k["symbol"] for k in keep}
        merged = keep + [p for p in picks if p["symbol"] not in keepset]
        # 必须按分数重排：keep 是在跑的（有仓的）会插到前面，若直接取前 N，
        # 低分的持仓币会占掉高分新币的名额 → 资金先喂了排名靠后的。
        # 用户明确要求：按排名来，top1 没挂上就不该让 top5 抢到资金。
        merged.sort(key=lambda x: -(x.get("score") or 0))
        if len(merged) > limit:
            log.info(f"选币：{len(merged)} 个可用（其中 {len(keep)} 个为在跑标的保留），"
                     f"按分数取前 {limit} 个")
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
            # 兜底：本轮选币失败/还没跑时 _selection 是空的，就沿用落库的清单。
            # 否则「选中的标的」会瞬间变成 0 个 —— 网格当轮停摆，而且下游那个
            # 「掉出清单就撤单」的清理会误判成「全都不在清单里」。
            for s in (self._selection or (self.config.get("grid_selection") or [])):
                # 区间宽度随标的自身波动率自适应：取 1 倍日振幅，夹在 4%~15%。
                # 固定区间对高波动币会频繁穿出，对低波动币又几乎不成交。
                rng = fixed if fixed > 0 else max(params.RANGE["min"],
                                                 min(params.RANGE["max"],
                                                     float(s.get("range") or params.RANGE["default"])))
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

        # 建仓资金严格按排名分配：高排名想挂没挂上时，低排名的建仓单撤单让位
        try:
            await self._enforce_ladder_priority()
        except Exception as e:
            log.warning(f"GRID 挂单优先级调度失败: {e}")

        specs = self._specs()
        active = {s["symbol"] for s in specs if s.get("symbol")}

        # 掉出清单、又没有仓位的标的 → 立刻撤光它的挂单。必须在 _reconcile_status
        # 之前跑：这类标的没有存货可卖，"只卖不买"对它没有意义，直接清掉更干净。
        try:
            await self._sweep_stray_orders(active)
        except Exception as e:
            log.warning(f"GRID 清理掉队挂单失败: {e}")

        # 处理「掉出名单」：先标记，再继续维护它们的卖单
        await self._reconcile_status(active)

        # 需要维护的标的 = 选中的 + 正在 draining 的（draining 只卖不买）
        manage: list[dict] = list(specs)
        for st in self.db.get_grid_state(None) or []:
            if st["symbol"] not in active:
                manage.append({"symbol": st["symbol"], "lower": float(st["lower"]),
                               "upper": float(st["upper"]), "grids": int(st["grids"]),
                               "size_usdt": float(st["size_usdt"]),
                               "auto_range_pct": float(cfg.get("auto_range_pct") or params.RANGE["default"])})

        symbols = [s["symbol"] for s in manage if s.get("symbol")]
        # 空单还没了结的标的，即使掉出了选币清单也要继续盯 —— 否则它的
        # 止盈/止损没人管、平仓也检测不到（实测漏了 6.5 小时才发现已平仓）。
        managed = set(symbols)
        orphan = []
        for k in self.db.runtime_keys("short_state:"):
            sym = k.split(":", 1)[1]
            if sym in managed:
                continue
            if int(self.db.get_runtime_stat(k, 0) or 0) not in (1, 2):
                continue
            orphan.append(sym)
        symbols = symbols + orphan
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

        # 孤儿空单：只做空单管理（不建多头网格——它已不在选币清单里）
        for sym in orphan:
            try:
                price = self.client.last_price(sym) or await self.client.ensure_price(sym)
                if price > 0:
                    await self._maybe_short(sym, price)
            except Exception as e:
                log.error(f"GRID {sym} 空单跟踪异常: {e}")

    async def _sweep_stray_orders(self, active: set):
        """不在选币清单、又没有仓位的标的 → 撤掉它**全部**挂单并清掉档位。

        用户要的规则：一个币掉出筛选 top5 时，它那些还没成交的建仓单就该取消 ——
        留着只是白占保证金，把排在前面的标的挤到没钱可挂。而且必须**每轮都查**，
        不能只在「有人排在前面却挂不上资金」时才顺手撤（那条路只覆盖清单内的标的）。

        为什么需要单独扫一遍：逐标的维护（step 里的 manage 清单）只包含
        「选中的」+「DB 里有网格状态的」标的。一个只有档位、没有网格状态行的标的
        两边都漏 —— 它的交易所挂单就此无人回收，一直占着钱。

        有持仓的走 draining（只卖不买，卖光后退役），有活跃空单的交给空单逻辑，
        这两类不在这里动。
        """
        if self.client.dry_run:
            return
        if not active:
            return                              # 选币清单为空 = 读不到 ≠ 全都不在，绝不清理
        syms = {lv["symbol"] for lv in self.db.get_grid_levels(None)}
        for sym in sorted(syms):
            if sym in active:
                continue
            st = self.db.get_grid_state(sym)
            # 持仓有两套口径（grid_state.base_qty 与逐笔账本），必须**都**看 ——
            # 只看一个的话，账面 0 但账本有货的标的会被当成空仓清掉。
            lq, _ = self.db.lots_qty_cost(sym)
            if (st and float(st.get("base_qty", 0) or 0) > 1e-12) or lq > 1e-12:
                continue                        # 有货 → draining 收尾
            if int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0) in (1, 2):
                continue                        # 有空单 → 空单逻辑管
            levels = self.db.get_grid_levels(sym)
            n, failed = 0, 0
            for lv in levels:
                if not lv.get("order_id"):
                    continue
                try:
                    okc = await self.client.cancel_order(lv["order_id"], sym, "futures")
                except Exception as e:
                    log.warning(f"GRID {sym}: 撤单 {lv['order_id']} 异常: {e}")
                    okc = False
                n += 1 if okc else 0
                failed += 0 if okc else 1
            # DB 丢了单号、交易所上却还挂着的，一并撤掉（QUSDT 实测出现过：
            # 交易所有 3 条买单，本地一条单号都没有）。
            try:
                # strict：查询失败不能当成「没有挂单」，否则下面的清档会把
                # 交易所上撤不掉的单从本地抹掉，从此无人回收。
                live = await self.client.fetch_open_orders(sym, "futures", strict=True)
            except Exception as e:
                log.warning(f"GRID {sym}: 清理残留挂单时查询失败，本轮跳过: {e}")
                continue
            for o in live:
                info = o.get("info") or {}
                if str(info.get("positionSide") or "LONG").upper() != "LONG":
                    continue                    # 空单腿不动
                try:
                    okc = await self.client.cancel_order(str(o.get("id")), sym, "futures")
                except Exception as e:
                    okc = False
                n += 1 if okc else 0
                failed += 0 if okc else 1
            if failed:
                # 还有没撤掉的单 → 本轮不清档位/状态，下一轮继续撤。
                # 反过来做的话，那张单会连同它的单号一起从本地消失。
                log.warning(f"GRID {sym}: 已掉出清单但仍有 {failed} 条挂单撤销失败"
                            f" → 保留档位，下轮重试")
                continue
            self.db.replace_grid_levels(sym, [])
            if st:
                self.db.conn.execute("DELETE FROM grid_state WHERE symbol=?", (sym,))
                self.db.conn.commit()
            self.db.set_runtime_stat(f"tp_high:{sym}", 0)
            log.warning(f"GRID {sym}: 已掉出选币清单且无持仓 → 撤销 {n} 条挂单、清空档位"
                        f"（不再占用建仓资金）")

    async def _enforce_ladder_priority(self):
        """建仓资金严格按排名分配（top1 → top5 逐个查）：

        · 有持仓 / 空单在跑：自管理，跳过
        · 交易所已有建仓挂单：已就座，跳过
        · 完全没挂单：要么在等回落（位置/锚门），要么是刚选上还没初始化 —— 跳过
        · **有建仓单想挂但没挂上（DB 里有档、交易所没单号）= 排到它了但资金不足**
          → 从它往后，所有「无持仓」标的已挂出的建仓单全部撤销让位，
          下一 tick 它自然按排名先挂上。保证「资金不足只苦排名靠后的」。
        """
        ranked = [s.get("symbol") for s in (self._selection or []) if s.get("symbol")]
        if len(ranked) < 2:
            return
        deficit = None
        for sym in ranked:
            st = self.db.get_grid_state(sym)
            if st and float(st.get("base_qty", 0) or 0) > 1e-12:
                continue                       # 有多仓：自管理
            if int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0) in (1, 2):
                continue                       # 有空单：自管理
            levels = self.db.get_grid_levels(sym)
            if any(l["status"] == "pending" and l.get("order_id") for l in levels):
                continue                       # 已挂单：就座
            if any(l["status"] == "pending" and not l.get("order_id") for l in levels):
                deficit = sym                  # 想挂但没挂上 = 排到它了但资金不足
                break
            # 完全没档位：等回落 / 待初始化，不构成资金抢占
        if not deficit:
            return
        di = ranked.index(deficit)
        for sym in ranked[di + 1:]:
            st = self.db.get_grid_state(sym)
            if st and float(st.get("base_qty", 0) or 0) > 1e-12:
                continue
            if int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0) in (1, 2):
                continue
            levels = [l for l in self.db.get_grid_levels(sym)
                      if l["status"] == "pending" and l.get("order_id")]
            if not levels:
                continue
            s = _throttle.emit(f"prio_cancel:{sym}")
            if s is not None:
                log.warning(f"GRID {sym}: 排名靠后让位（{deficit} 排在前面还没挂上资金）"
                            f"→ 撤销 {len(levels)} 条建仓挂单{s}")
            for l in levels:
                await self.client.cancel_order(l["order_id"], sym, "futures")
                self.db.conn.execute("UPDATE grid_levels SET order_id='' WHERE id=?", (l["id"],))
            self.db.conn.commit()

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
                pct = float(spec.get("auto_range_pct") or self.cfg.get("auto_range_pct")
                        or params.RANGE["fallback"])
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
                    # 空仓、且不建多头网格 —— 但空单仍要判断，不能在这里 return 掉
                    await self._maybe_short(sym, price)
                    return
                need_init = False     # 保留旧网格（下方买单仍在），继续正常维护
            if need_init:
                await self._init_grid(sym, lower, upper, grids, size_usdt, price)
                st = self.db.get_grid_state(sym)
                if not st:
                    # 这次没建成多头网格（位置太高 / 冷却期内）—— 位置高恰恰是
                    # 该考虑开空的时候，绝不能在这里 return，否则空单永远没机会
                    await self._maybe_short(sym, price)
                    return


        if not self.client.dry_run:
            await self._trim_sell_excess(sym, st)
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

        # 对账必须放在**成交检测之后** —— 先把已知成交落账，再对余额。
        # 反过来的话：对账先把本地持仓拉平到交易所，紧接着 _check_fills
        # 又把同一笔成交记一次 → 本地数量翻倍、下个 tick 再补一条幽灵平仓。
        # 与交易所对账持仓：交易所为准。
        if not self.client.dry_run:
            try:
                positions = await self.client.get_positions()
            except Exception as e:
                # 读不到 ≠ 没有持仓。绝不把"查不到"当成"已平仓"，否则一次接口抖动
                # 就会把仓位记录误清空。本轮直接跳过对账。
                positions = None
                log.warning(f"GRID {sym}: 读取持仓失败，本轮跳过对账: {e}")
            if positions is not None:
                ex_qty, ex_entry = 0.0, 0.0
                for p in positions:
                    if (self.client.user_symbol(p.get("symbol", "")) == sym
                            and p.get("side") == "long"):
                        ex_qty = float(p.get("contracts", 0) or 0)
                        ex_entry = float(p.get("entryPrice", 0) or 0)
                db_qty = float(st.get("base_qty", 0) or 0)
                db_cost = float(st.get("total_cost", 0) or 0)
                db_avg = (db_cost / db_qty) if db_qty > 0 else 0
                if ex_qty <= 1e-12 and db_qty > 1e-12:
                    # 交易所已经没持仓、本地却记着有 → 被外部平掉了（手工平仓/强平）。
                    # 必须清空本地并撤掉失效挂单，同时**把这笔平仓补记进成交记录**，
                    # 否则成交记录里凭空少一笔、已实现盈亏也对不上。
                    ref = [float(l["price"]) for l in self.db.get_grid_levels(sym)
                           if l["side"] == "sell"]
                    exit_px, ours, ex_pnl = await self._recent_exit(
                        sym, db_qty, "sell", ref, position_side="LONG")
                    exit_px = exit_px or price
                    consumed = self.db.consume_lots(sym, exit_px, db_qty)
                    taken = sum(t for _, t in consumed)
                    pnl = sum((exit_px - lp) * t for lp, t in consumed)
                    if taken < db_qty - 1e-9:
                        pnl += (exit_px - db_avg) * (db_qty - taken)
                    if ex_pnl is not None:
                        pnl = ex_pnl          # 交易所的已实现盈亏最权威，优先用
                    self.db.insert_order({
                        "mode": "grid", "symbol": sym, "side": "sell",
                        "type": "grid_sell" if ours else "grid_external",
                        "price": exit_px, "amount": db_qty,
                        "cost": round(exit_px * db_qty, 6),
                        "fee": round(exit_px * db_qty * 0.0005, 6),
                        "pnl": round(pnl, 6),
                        "pnl_rate": round(pnl / (db_avg * db_qty), 8) if db_avg > 0 else 0,
                        "dry_run": False,
                        "note": "网格卖出（挂单成交·对账补记）" if ours else "外部平仓（非本程序发起）"})
                    self.db.upsert_grid_state(
                        sym, base_qty=0.0, total_cost=0.0,
                        realized_pnl=float(st.get("realized_pnl", 0) or 0) + pnl,
                        sell_count=int(st.get("sell_count", 0) or 0) + 1)
                    await self._cancel_level_orders(sym)
                    self.db.replace_grid_levels(sym, [])
                    self.db.clear_lots(sym)
                    self.db.set_runtime_stat(f"grid_exit:{sym}", exit_px)
                    self.db.set_runtime_stat(f"tp_high:{sym}", 0)
                    self.db.set_runtime_stat(f"grid_ext_close:{sym}", time.time())
                    st = self.db.get_grid_state(sym) or st
                    log.warning(f"GRID {sym}: 交易所已无持仓（本地记 {db_qty:.0f} 张）→ "
                                f"判定为外部平仓：按成交价 {exit_px:.6g} 补记一笔"
                                f"（盈亏 {pnl:+.4f}U），并撤掉失效挂单")
                else:
                    # 数量不符，或均价与交易所偏差超过 0.5% → 按交易所重建
                    qty_bad = ex_qty > 0 and abs(ex_qty - db_qty) >= 1
                    avg_bad = (ex_qty > 0 and ex_entry > 0 and db_qty > 0
                               and abs(db_avg - ex_entry) / ex_entry > 0.005)
                    if qty_bad or avg_bad:
                        # 只做持仓校正，**不在这里记账**。
                        # 曾试过按数量差补一条成交记录，结果与 _check_fills /
                        # _reconcile_orders 双重记账：本 tick 对账补 +N，紧接着
                        # _check_fills 又记一次 +N → 下个 tick 对账发现本地多出一倍，
                        # 再补一条 -N 的「平仓」（pnl 恒为 0）。实测产生 14 条幽灵记录。
                        # 真实成交由 _reconcile_orders（查已撤/已成订单）用账本核销，
                        # 那条路径 PnL 才是对的。
                        self.db.upsert_grid_state(sym, base_qty=ex_qty, total_cost=ex_qty * ex_entry)
                        # 账本以交易所为准重建（对账差异说明账本已不可信）
                        self.db.reset_lots(sym, ex_qty, ex_entry, "对账重建")
                        st = self.db.get_grid_state(sym)
                        log.warning(f"GRID {sym}: 持仓对账 数量 {db_qty:.0f}→{ex_qty:.0f} / "
                                    f"均价 {db_avg:.6f}→{ex_entry:.6f}，成本已按交易所重算")
                        # 数量对不上 = 必有一笔成交没落到 orders 表（孤儿单成交 /
                        # 刚成交就被误清单号）。补录（幂等）→ 成交记录与盈亏才完整。
                        await self._backfill_trades(sym)
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
            # 高位试空。**draining 也要调**：掉出清单的标的如果还挂着一张没成交的
            # 开空单（或在持仓中），跳过它就没人撤单、没人管止盈止损了。
            # 「不开新仓」由 _maybe_short 内部的 draining 判断负责。
            try:
                await self._maybe_short(sym, price)
            except Exception as e:
                log.error(f"GRID {sym} 空单逻辑异常: {e}")

    async def cancel_waiting(self, sym: str) -> tuple[bool, str]:
        """「取消并重新筛选」：撤掉该标的全部挂单、清掉它的网格状态，把它移出
        本轮选币清单，并强制下一轮重新选币。

        只对**空仓待机**（已挂单、未成交）的标的生效 —— 有持仓的直接拒绝，
        免得误清仓位。刻意**不设静置期**：重选到它就照常挂单，选到别的也一样。
        """
        st = self.db.get_grid_state(sym)
        # st 为 None = 选上了但还没建仓（入场闸门在等回落）→ 同样允许取消重选
        if st is not None and (st.get("status") or "active") == "draining":
            return False, "该标的正在退出（只卖不买），不能取消重选"
        lq, _ = self.db.lots_qty_cost(sym)
        if (st is not None and float(st.get("base_qty", 0) or 0) > 1e-12) or lq > 1e-12:
            return False, "该标的有持仓，不能取消重选（请先平仓）"
        if int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0) in (1, 2):
            return False, "该标的有空单在跑，不能取消重选"
        if not self.client.dry_run:
            ex_qty, _ = await self._current_entry(sym)
            if ex_qty > 1e-12:
                return False, "交易所有持仓，不能取消重选"
        await self._cancel_level_orders(sym)
        await self._cancel_short_stop(sym)
        self.db.replace_grid_levels(sym, [])
        self.db.clear_lots(sym)
        self.db.conn.execute("DELETE FROM grid_state WHERE symbol=?", (sym,))
        self.db.conn.commit()
        fresh = self.db.load_config()
        fresh["grid_selection"] = [s for s in (fresh.get("grid_selection") or [])
                                   if s.get("symbol") != sym]
        self.db.save_config(fresh)
        self.db.set_runtime_stat("grid_selection_ts", 0)   # 强制下一轮重新选币
        log.info(f"GRID {sym}: 已取消全部挂单并移出清单 → 下一轮重新选币")
        return True, "已取消挂单，正在重新选币"

    async def _cancel_level_orders(self, sym: str):
        """撤掉该标的在交易所上所有挂着的档位单（DB 档位由调用方清）。"""
        if self.client.dry_run:
            return
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] == "pending" and lv.get("order_id"):
                await self.client.cancel_order(lv["order_id"], sym, "futures")

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

    async def _trim_sell_excess(self, sym: str, st: dict):
        """卖单总量不得超过持仓量 —— 超出的部分在交易所必然被拒（-4118），
        还会永远重试刷屏。对账补记/部分成交/手动改单都可能造成「卖单 > 持仓」。

        从最高价往下保留，累计到持仓量为止；超出的档位：已挂的撤单，
        然后把边界档位缩到剩余额度，完全放不下的直接删除。
        """
        qty = float(st.get("base_qty", 0) or 0)
        if qty <= 0:
            return
        sells = sorted([l for l in self.db.get_grid_levels(sym)
                        if l["status"] == "pending" and l["side"] == "sell"],
                       key=lambda l: -float(l["price"]))
        total = sum(float(l["amount"]) for l in sells)
        if total <= qty + 1e-9:
            return
        excess = total - qty
        s = _throttle.emit(f"grid_trim:{sym}")
        if s is not None:
            log.warning(f"GRID {sym}: 卖单总量 {total:.0f} > 持仓 {qty:.0f} → 裁掉 {excess:.0f} 张{s}")
        remain = qty
        for lv in sells:
            a = float(lv["amount"])
            if remain >= a - 1e-9:
                remain -= a
                continue
            # 这一档超出额度：缩量或删除
            new_a = max(0.0, remain)
            remain = 0.0
            if lv.get("order_id") and not self.client.dry_run:
                await self.client.cancel_order(lv["order_id"], sym, "futures")
            if new_a < 1e-9:
                self.db.conn.execute("DELETE FROM grid_levels WHERE id=?", (lv["id"],))
            else:
                # 缩量后需要重新挂（撤过了）；置空单号交给 _place_live 重挂
                self.db.conn.execute(
                    "UPDATE grid_levels SET amount=?, order_id='' WHERE id=?",
                    (new_a, lv["id"]))
            self.db.conn.commit()
        # 注意：删除/缩量后卖单覆盖可能 < 持仓？不会 —— 裁剪只发生在 total>qty 时，
        # 保留部分正好等于 qty，覆盖不变量仍成立。

    async def _ensure_sell_coverage(self, sym: str, st: dict):
        """确保卖单覆盖持仓 —— **以交易所实况为准**，不是 DB 账面。

        部分成交、用户手动改单/撤单都会让 DB 与交易所偏离（实测 1087→97、86→6），
        按账面算覆盖永远对不上。每次直接对账：
          covered = 交易所当前卖单总量（含用户手动挂的）
          short   = 持仓 - covered
        补挂方式：并入价格最高的卖档 —— 撤掉该档的交易所余量重挂（余量+short）；
        撤不掉（不是我们的单）就只挂 short。一条卖档都没有则在区间上沿挂。
        """
        qty = float(st.get("base_qty", 0) or 0)
        if qty <= 0:
            return
        try:
            live = await self.client.fetch_open_orders(sym, "futures")
        except Exception:
            return
        live_sells = [o for o in live if o.get("side") == "sell"]
        covered = sum(float(o.get("amount") or 0) for o in live_sells)
        if covered >= qty - 1e-9:
            return
        short = qty - covered
        s = _throttle.emit(f"grid_cover:{sym}")
        if s is not None:
            log.warning(f"GRID {sym}: 交易所卖单仅覆盖 {covered:.0f}/{qty:.0f} 张 → 补 {short:.0f} 张{s}")
        rows = sorted([l for l in self.db.get_grid_levels(sym)
                       if l["status"] == "pending" and l["side"] == "sell"],
                      key=lambda l: -float(l["price"]))
        if rows:
            top = rows[0]
            tp = float(top["price"])
            top_live = sum(float(o.get("amount") or 0) for o in live_sells
                           if abs(float(o.get("price") or 0) - tp) <= tp * 1e-6)
            cancelled = True
            if top.get("order_id") and not self.client.dry_run:
                cancelled = await self.client.cancel_order(top["order_id"], sym, "futures")
            new_amt = short + (top_live if cancelled else 0.0)
            rp = self.client.round_price(sym, tp, "futures")
            res = await self.client.create_limit_order(
                sym, "sell", new_amt, rp, "futures", position_side="LONG")
            oid = res["order_id"] if res else ""
            self.db.conn.execute("UPDATE grid_levels SET amount=?, order_id=? WHERE id=?",
                                 (new_amt, oid, top["id"]))
            self.db.conn.commit()
        else:
            upper = float(st.get("upper", 0) or 0)
            if upper <= 0:
                return
            rp = self.client.round_price(sym, upper, "futures")
            res = await self.client.create_limit_order(sym, "sell", short, rp, "futures",
                                                       position_side="LONG")
            oid = res["order_id"] if (res and not res.get("dry_run")) else ""
            self.db.insert_grid_level(sym, len(self.db.get_grid_levels(sym)), rp, "sell", short)
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

        pct = float(spec.get("auto_range_pct") or cfg.get("auto_range_pct")
                    or params.RANGE["default"])
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
        # 手工平仓冷静期：只对「已掉出筛选清单」的标的生效。
        # 还在名单里的不冷静 —— 不追高锚点已经挡住了追高，时间惩罚是双重管束
        # （用户反馈：写得太死）；掉出名单后在冷静期内被重新选中时才需要等。
        cool = float(cfg.get("ext_close_cooldown_min", 30) or 0) * 60
        last = float(self.db.get_runtime_stat(f"grid_ext_close:{sym}", 0) or 0)
        if cool > 0 and last and (time.time() - last) < cool:
            in_sel = any(s.get("symbol") == sym for s in (self._selection or []))
            if not in_sel and cfg.get("auto_select", False):
                self._warn_once(f"grid_extcool:{sym}",
                                f"GRID {sym}: 手工平仓且已掉出筛选清单，"
                                f"{(cool-(time.time()-last))/60:.0f} 分钟内暂不重新建仓")
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
    async def _current_short(self, sym: str) -> tuple[float, float, bool]:
        """返回 (空头张数, 开仓均价, 是否读到了)。

        第三个值很关键：**接口失败必须与「真的没有持仓」区分开**。
        把查询失败当成 0 张，会被下面的状态机读成「空头已经平掉了」——
        于是补记一笔假平仓、撤掉止损单、把管理状态清零，交易所上却还挂着一笔
        **没有止损保护的裸空头**。空头亏损无上限，这个误判不能出现。
        """
        try:
            for p in await self.client.get_positions():
                if self.client.user_symbol(p.get("symbol", "")) == sym and p.get("side") == "short":
                    return float(p.get("contracts", 0) or 0), float(p.get("entryPrice", 0) or 0), True
        except Exception as e:
            log.warning(f"GRID {sym}: 读取空头持仓失败（本轮不推进空单状态）: {e}")
            return 0.0, 0.0, False
        return 0.0, 0.0, True

    async def _maybe_short(self, sym: str, price: float):
        """价格到当日区间顶部时的单次试空。

        用户定的规则：挂单开空、数量最小、只做一单、必带止损、不加仓，
        平掉之后该币不再开空。与多单互斥（同一个币同时只能一个方向）。

        **开关在「交易配置」里（grid.short_enabled）**。关掉之后：
          · 不再扫描任何开空机会（该币连持仓都不查，整个空单功能不运转）
          · 「挂着但没成交」的开空单会被撤掉
          · **已经成交**的空头持仓照常管理止盈/止损，一直管到平掉为止
            （止损是交易所条件单，止盈是软件触发，两者都不会因为关开关而失效）
        """
        if self.client.dry_run:
            return                                   # 空单只做实盘：dry_run 无真实持仓可依
        cfg = self.cfg
        enabled = bool(cfg.get("short_enabled", False))
        state = int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0)
        # 开关关掉、该币又没有空头在途 → 彻底不碰（不查持仓、不扫机会）。
        if not enabled and state == 0:
            return
        st = self.db.get_grid_state(sym) or {}
        draining = (st.get("status") or "active") == "draining"

        # 允许多空同时存在（对冲模式）：做多网格与高位试空互不干扰，各管各的。
        sq, entry, ok = await self._current_short(sym)
        if not ok:
            return                                       # 仓位读不到 → 本轮什么也不做

        if state >= 2 and sq <= 1e-12:
            # 空头已平（多数是交易所止损单触发，或手工平）→ 补记成交 + 一次性用完
            q0 = float(self.db.get_runtime_stat(f"short_qty:{sym}", 0) or 0)
            e0 = float(self.db.get_runtime_stat(f"short_entry:{sym}", 0) or 0)
            if q0 > 0 and e0 > 0:
                exit_px, _, ex_pnl = await self._recent_exit(sym, q0, side="buy",
                                                             position_side="SHORT")
                exit_px = exit_px or price
                # 空头：出场价高于入场 = 亏损 = 止损；低于 = 盈利 = 止盈
                why = "止损" if exit_px >= e0 else "止盈"
                self._record_short_close(sym, q0, e0, exit_px, why, pnl_override=ex_pnl)
            await self._cancel_short_stop(sym)       # 清掉可能残留的止损单
            self._finalize_short(sym)
            log.info(f"GRID {sym}: 空单已平仓 → 该币不再开空")
            return
        if sq > 1e-12:
            # 成交了 → 状态推进到「持仓中」。不推进的话，后面的平仓检测
            # （要求 state>=2）永远不会触发，整个空单会卡死在「挂单中」。
            if state < 2:
                self.db.set_runtime_stat(f"short_state:{sym}", 2)
                # 开仓成交才记一笔（挂单时只是挂着，不算成交）——否则成交记录里
                # 只有平仓没有开仓，看起来像"没下单就直接平了"
                self.db.insert_order({
                    "mode": "grid", "symbol": sym, "side": "sell", "type": "short_open",
                    "price": entry or price, "amount": sq,
                    "cost": round((entry or price) * sq, 6),
                    "fee": round((entry or price) * sq * 0.0005, 6),
                    "pnl": 0, "pnl_rate": 0,
                    "dry_run": self.client.dry_run, "note": "空单开仓（高位试空）"})
                log.info(f"GRID {sym}: 空单开仓已记账 —— {sq:.0f} 张 @ {entry or price:.6g}")
            self.db.set_runtime_stat(f"short_entry:{sym}", entry or price)
            self.db.set_runtime_stat(f"short_qty:{sym}", sq)
            if not enabled:
                self._warn_once(f"short_off_hold:{sym}",
                                f"GRID {sym}: 空单开关已关闭，但该币仍持有空头 "
                                f"{sq:.0f} 张 → 继续挂止盈/止损，直到平掉")
            await self._manage_short(sym, sq, entry or price, price)
            return
        if state == 1:
            if not enabled:
                # 关掉开关：只撤「还没成交」的开空挂单。从没成交过 = 这一轮空单
                # 没发生过，所以不把 short_done 置位（重新打开开关时它还能再试）。
                await self._cancel_short_entry(sym, "空单开关已关闭")
                return
            if not self._in_selection(sym):
                # 用户规则：挂着还没成交，币却已经掉出 top5 → 撤掉这张开空挂单。
                # 它已经不在名单里了，再等成交只是白占保证金。
                await self._cancel_short_entry(sym, "已掉出选币 top5 且开空单尚未成交")
                return
            if await self._short_entry_open(sym):
                return                               # 开空单还挂着，等它成交
            # 挂单已不在交易所（被撤/过期/或已成交后平掉）→ 这轮空单结束
            log.info(f"GRID {sym}: 开空挂单已不在交易所 → 判定该轮空单结束（该币不再开空）")
            await self._cancel_short_stop(sym)
            self._finalize_short(sym)
            return

        # ---- 没有空头持仓、也没有开空挂单 ----
        if not enabled:
            return                                   # 开关关闭：不找新机会
        if self.db.get_runtime_stat(f"short_done:{sym}", 0) > 0:
            return                                   # 该币的空单额度已用完
        if draining:
            return                                   # 正在退出：可以管理存量空单，但不新开
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

        offset = float(cfg.get("short_entry_offset", 0.025) or 0.025)
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

    def _finalize_short(self, sym: str):
        """空单收尾：统一重置全部状态位。

        任何空单平仓路径（检测到已平/软件止盈/软件止损/挂单消失）都必须调它，
        别各自手动清零 —— 之前只清 short_state/done、漏掉 short_qty，
        导致已平空单的旧数量被 _short_view 回退逻辑显示成「还在持仓」。
        """
        self.db.set_runtime_stat(f"short_state:{sym}", 0)
        self.db.set_runtime_stat(f"short_done:{sym}", 1)
        self.db.set_runtime_stat(f"short_stop_oid:{sym}", 0)
        self.db.set_runtime_stat(f"short_qty:{sym}", 0)
        self.db.set_runtime_stat(f"short_entry:{sym}", 0)

    async def _cancel_short_stop(self, sym: str):
        """撤掉空单的止损单。

        注意：交易所的条件单是「算法单」，普通挂单接口看不到、也撤不掉，
        必须走算法单接口 —— 否则平仓后止损单会残留，之后触发会反手开多。
        """
        try:
            n = await self.client.cancel_algo_orders(sym)
            if n:
                log.info(f"GRID {sym}: 已撤销 {n} 条交易所条件单")
        except Exception as e:
            log.warning(f"GRID {sym}: 撤止损单失败: {e}")

    def _in_selection(self, sym: str) -> bool:
        """该标的是否还在当前选币清单（top5）里。"""
        sel = self._selection or (self.config.get("grid_selection") or [])
        return any(s.get("symbol") == sym for s in sel)

    async def _cancel_short_entry(self, sym: str, why: str = "空单开关已关闭") -> int:
        """撤掉「挂着但还没成交」的开空单，并清掉这轮空单的挂单状态。

        只撤 positionSide=SHORT 的卖单 —— 多头网格的卖档同是 sell，但属于 LONG
        腿，不能误伤。没有成交过就不置 short_done：这一轮空单等于没发生，
        之后该币还能再试一次。
        """
        n = 0
        try:
            # strict：拉不到挂单列表时**抛出**而不是返回空表。若把失败当成「没有
            # 挂单」，下面会把状态清零 → 交易所上那张单再没人认领，一旦成交就是
            # 一笔没有止损的裸空头。宁可本轮不动、下轮重试。
            orders = await self.client.fetch_open_orders(sym, "futures", strict=True)
        except Exception as e:
            log.warning(f"GRID {sym}: 查询开空挂单失败，本轮不撤、状态保持，下轮重试: {e}")
            return 0
        targets = []
        for o in orders:
            info = o.get("info") or {}
            if (o.get("side") == "sell"
                    and str(info.get("positionSide") or "").upper() == "SHORT"):
                targets.append(o)
        failed = 0
        for o in targets:
            try:
                okc = await self.client.cancel_order(str(o.get("id")), sym, "futures")
            except Exception as e:
                log.warning(f"GRID {sym}: 撤销开空挂单 {o.get('id')} 异常: {e}")
                okc = False
            if okc:
                n += 1
            else:
                failed += 1
        if failed:
            # 还有没撤掉的单 → 保留状态，下一轮继续撤。绝不能在"单还在交易所上"
            # 的时候把状态清零，否则这张单就永远没人管了。
            log.warning(f"GRID {sym}: {failed} 条开空挂单撤销失败 → 状态保持，下轮重试")
            return n
        # 顺带清掉可能残留的止损条件单：没有持仓时它一旦触发会反手开多。
        await self._cancel_short_stop(sym)
        self.db.set_runtime_stat(f"short_state:{sym}", 0)
        self.db.set_runtime_stat(f"short_stop_oid:{sym}", 0)
        self.db.set_runtime_stat(f"short_qty:{sym}", 0)
        self.db.set_runtime_stat(f"short_entry:{sym}", 0)
        log.warning(f"GRID {sym}: {why} → 撤销 {n} 条未成交的开空挂单")
        return n

    async def _recent_exit(self, sym: str, qty: float, side: str = "sell",
                           ref_prices: list = None, position_side: str = "LONG"):
        """还原「最近这批平仓」的真实成交均价，并判断是不是我们挂的单成交的。

        判断「是不是我们自己的单」有两条依据（任一命中即可）：
          1. 成交明细里的 clientOrderId 带 `ec` 前缀（本程序下的单都会带）；
          2. 成交价精确命中我们挂过的档位价（ref_prices）—— 老单没有前缀时用这条，
             手工市价平仓不会刚好落在挂单价上。
        两条都不命中才归为「外部平仓」（可能是人工、强平或交易所触发）。
        返回 (均价 or None, 是否我们自己的单, 交易所权威盈亏 or None)。
        """
        # 放宽到 24 小时：孤儿空单的平仓可能很久以后才被发现，窗口太小就
        # 找不到那笔成交，只能拿当前市价凑（实测差了 1%）。
        since = int((time.time() - 24 * 3600) * 1000)
        trades = await self.client.fetch_my_trades(sym, since, 200)
        if not trades:
            return None, False, None
        left, num, den, ours, ex_pnl = float(qty), 0.0, 0.0, False, None
        for t in sorted(trades, key=lambda x: x.get("timestamp") or 0, reverse=True):
            if str(t.get("side")) != side:
                continue
            # 对冲模式下必须再看 positionSide：sell 既可能是「多头平仓」，
            # 也可能是「空单开仓」。不区分就会拿空单开仓价当多头平仓价
            # （BATUSDT 实测：把 05:40 的空单开仓 0.20161 当成了多头平仓价）。
            ps = str((t.get("info") or {}).get("positionSide") or "").upper()
            if ps and ps != position_side.upper():
                continue
            a = float(t.get("amount") or 0)
            p = float(t.get("price") or 0)
            if a <= 0 or p <= 0:
                continue
            coid = str((t.get("info") or {}).get("clientOrderId") or "")
            if coid.startswith("ec"):
                ours = True
            elif ref_prices:
                for rp0 in ref_prices:
                    if rp0 and abs(p - float(rp0)) <= float(rp0) * 0.0015:
                        ours = True          # 成交价命中我方挂单档位
                        break
            rp = (t.get("info") or {}).get("realizedPnl")   # 交易所权威盈亏
            if rp not in (None, ""):
                try:
                    ex_pnl = (ex_pnl or 0.0) + float(rp)
                except (TypeError, ValueError):
                    pass
            take = min(a, left)
            num += p * take
            den += take
            left -= take
            if left <= 1e-9:
                break
        return ((num / den) if den > 0 else None), ours, ex_pnl

    async def _short_entry_open(self, sym: str) -> bool:
        """开空的那张限价卖单是否还挂在交易所。

        必须按 positionSide=SHORT 区分 —— 多头网格的卖档也是 sell，但它们
        属于 LONG 腿，不能混为一谈。
        """
        try:
            for o in await self.client.fetch_open_orders(sym, "futures"):
                ps = str((o.get("info") or {}).get("positionSide") or "").upper()
                if o.get("side") == "sell" and ps == "SHORT":
                    return True
        except Exception as e:
            log.warning(f"GRID {sym}: 查询开空挂单失败: {e}")
            return True                              # 查不到就当作还挂着，别误判结束
        return False

    async def _manage_short(self, sym: str, qty: float, entry: float, price: float):
        """空头持仓管理：止盈用软件平；止损优先挂交易所条件单，挂不上则软件兜底。"""
        cfg = self.cfg
        sl = float(cfg.get("short_stop_pct", 0.035) or 0.035)
        tp = float(cfg.get("short_tp_pct", 0.10) or 0.10)
        stop_px = entry * (1 + sl)
        tp_px = entry * (1 - tp)
        placed = float(self.db.get_runtime_stat(f"short_stop_oid:{sym}", 0) or 0) > 0

        # 止盈：软件平。先撤掉交易所止损单 —— 否则平完仓后它一旦触发会反手开多。
        if price <= tp_px:
            log.warning(f"GRID {sym}: 空单止盈（现价 {price:.6g} ≤ {tp_px:.6g}）→ 市价平空")
            await self._cancel_short_stop(sym)
            res = await self.client.create_market_order(sym, "buy", qty, "futures",
                                                        position_side="SHORT")
            if not res:
                log.error(f"GRID {sym}: 空单止盈市价平仓失败，状态保留，下一轮重试")
                return
            await self._record_short_fill(sym, qty, entry, res, price, "止盈")
            self._finalize_short(sym)
            return

        # 止损：交易所条件单挂上了就靠它；没挂上则软件兜底
        if not placed:
            if price >= stop_px:
                log.warning(f"GRID {sym}: 空单止损（现价 {price:.6g} ≥ {stop_px:.6g}）→ 市价平空")
                res = await self.client.create_market_order(sym, "buy", qty, "futures",
                                                            position_side="SHORT")
                if not res:
                    log.error(f"GRID {sym}: 空单止损市价平仓失败，状态保留，下一轮重试")
                    return
                await self._record_short_fill(sym, qty, entry, res, price, "止损")
                self._finalize_short(sym)
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
        # 市价单返回的 price 常是下单瞬间的行情快照，不是成交价（PONSUSDT 实测
        # 记成 0.3 而真实成交 0.2981）。取交易所成交明细里的真实均价。
        fill, _, _ = await self._recent_exit(sym, qty, side="sell", position_side="LONG")
        fill = fill or float(res.get("price") or price)
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
        await self._cancel_level_orders(sym)

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
            # strict：拉不到挂单列表就整轮跳过。若把失败当成「空列表」，
            # 下面会把该标的所有挂单都判成「已消失」→ 集体误清 + 重复挂单。
            open_orders = await self.client.fetch_open_orders(sym, "futures", strict=True)
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
                continue
            if st in ("canceled", "expired"):
                # 交易所明确说「撤了/过期了」→ 确实没成交，清掉单号重挂
                self.db.clear_grid_order_id(lv["id"])
                s = _throttle.emit(f"grid_stale:{sym}")
                if s is not None:
                    log.warning(f"GRID {sym}: 挂单 {lv['order_id']} 已被撤/过期"
                                f"(状态={st}) → 清空并重新挂单{s}")
                continue
            # 状态既非「已成」也非「已撤」，但已不在挂单列表 —— 实测这是
            # 「刚成交、状态接口还没翻页」的竞态（日志里 18 次全部如此）。
            # 旧逻辑直接清空单号，而 _check_fills 只在「有 order_id」时才认成交，
            # 于是这一档的成交被永久吞掉（ENA/OP/PONS/FIL 实测各丢一笔）。
            # 改为：先拿交易所成交明细核实并补录（幂等），核实到才算成交。
            got = await self._backfill_trades(sym, lv)
            if got:
                self.db.fill_grid_level(lv["id"])
                log.warning(f"GRID {sym}: 挂单 {lv['order_id']} 已不在列表且状态={st}"
                            f" → 按成交明细核实为「已成交」，已补录")
            else:
                self.db.clear_grid_order_id(lv["id"])
                s = _throttle.emit(f"grid_stale:{sym}")
                if s is not None:
                    log.warning(f"GRID {sym}: 挂单 {lv['order_id']} 已不在交易所"
                                f"(状态={st}) 且无对应成交 → 清空并重新挂单{s}")

    # ===== 成交补录（交易所为准，幂等）=====
    async def _backfill_trades(self, sym: str, only_level: dict = None) -> int:
        """用交易所成交明细补录本地漏掉的成交。

        触发点：
          · 持仓对账发现本地数量与交易所不符（说明有成交没落账）；
          · _reconcile_orders 发现挂单莫名消失、状态又非「已撤」。

        幂等靠交易所的成交 id（seen_trades 表）。只补 **orders 表**（成交记录）
        与已实现盈亏；持仓数量/成本一律交给对账，绝不在这里加减 ——
        上一版在持仓对账里按差额记账，与 _check_fills 双重记账，产生了 14 条
        幽灵记录（详见 28b9a83 的修复说明）。

        返回补录/确认的成交笔数。
        """
        try:
            since = int((time.time() - 48 * 3600) * 1000)
            trades = await self.client.fetch_my_trades(sym, since, 200)
        except Exception as e:
            log.warning(f"GRID {sym}: 拉取成交明细失败，跳过补录: {e}")
            return 0
        if not trades:
            return 0
        # 本地已有、但还没绑定交易所成交号的记录 —— 用于识别「_check_fills 已经记过」
        # 的那些成交，避免二次记账。按时间就近 + 数量匹配（同价同量的重复单靠
        # used 集合逐一消费，不会一笔交易匹配到两条记录）。
        local = [o for o in self.db.get_orders(400, mode="grid")
                 if o["symbol"] == sym and float(o["amount"] or 0) > 0
                 and o["type"] in ("grid_buy", "grid_base", "dca_buy", "grid_sell",
                                   "grid_trail", "grid_external", "close",
                                   "short_open", "short_close")]
        used, got = set(), 0
        for t in sorted(trades, key=lambda x: x.get("timestamp") or 0):
            tid = str(t.get("id") or "")
            if not tid or self.db.trade_seen(sym, tid):
                continue
            info = t.get("info") or {}
            ps = str(info.get("positionSide") or "LONG").upper()
            side = str(t.get("side") or "")
            amt = float(t.get("amount") or 0)
            px = float(t.get("price") or 0)
            tms = int(t.get("timestamp") or 0)
            if amt <= 0 or px <= 0:
                continue
            if only_level is not None:
                # 只核实「这一档」是否成交
                lvp = float(only_level["price"])
                if side != only_level["side"]:
                    continue
                if abs(amt - float(only_level["amount"])) > max(1e-9, float(only_level["amount"]) * 0.02):
                    continue
                # 同标的同方向的等量挂单很常见（网格里多档金额一样），只按数量匹配
                # 会把别的档位的成交算到这一档头上。补两条判据：
                #   价格 —— 买成交不会高于挂单价、卖成交不会低于挂单价；
                #   时效 —— 挂单刚刚才消失，成交不会发生在很久以前。
                if lvp > 0 and ((side == "buy" and px > lvp * 1.001)
                                or (side == "sell" and px < lvp * 0.999)):
                    continue
                if abs(tms / 1000 - time.time()) > 6 * 3600:
                    continue
            # 1) 已被 _check_fills / 其他路径记过 → 只打标记
            hit = None
            for o in local:
                if o["id"] in used:
                    continue
                want = ("buy" if o["type"] in ("grid_buy", "grid_base", "dca_buy", "short_close")
                        else "sell")
                if want != side:
                    continue
                if abs(float(o["amount"]) - amt) > max(1e-9, amt * 0.02):
                    continue
                try:
                    ots = datetime.fromisoformat(o["ts"]).timestamp() * 1000
                except Exception:
                    continue
                if abs(ots - tms) > 300000:
                    continue
                hit = o
                break
            if hit is not None:
                used.add(hit["id"])
                self.db.mark_trade_seen(sym, tid, str(tms))
                got += 1
                continue
            # 2) 真·漏记 → 补一条成交记录
            ex_pnl = info.get("realizedPnl")
            try:
                ex_pnl = float(ex_pnl) if ex_pnl not in (None, "") else None
            except (TypeError, ValueError):
                ex_pnl = None
            if ps == "SHORT":
                ptype = "short_open" if side == "sell" else "short_close"
                note = "空单开仓（成交明细补录）" if side == "sell" else "空单平仓（成交明细补录）"
                pnl = 0.0 if side == "sell" else (ex_pnl or 0.0)
            elif side == "buy":
                ptype, note, pnl = "grid_buy", "网格买入（成交明细补录）", 0.0
            else:
                ptype, note = "grid_sell", "网格卖出（成交明细补录）"
                pnl = ex_pnl or 0.0          # 交易所已实现盈亏最权威
            ts = datetime.fromtimestamp(tms / 1000, timezone.utc).isoformat() if tms else None
            self.db.insert_order({
                "mode": "grid", "symbol": sym, "side": side, "type": ptype,
                "ts": ts, "price": px, "amount": amt, "cost": round(px * amt, 6),
                "fee": round(float(info.get("commission") or 0) or px * amt * 0.0005, 6),
                "pnl": round(pnl, 6), "pnl_rate": 0,
                "dry_run": False, "note": note})
            if pnl:
                st = self.db.get_grid_state(sym) or {}
                self.db.upsert_grid_state(
                    sym, realized_pnl=float(st.get("realized_pnl", 0) or 0) + pnl)
            self.db.mark_trade_seen(sym, tid, str(tms))
            got += 1
            log.warning(f"GRID {sym}: 成交明细补录 —— {side} {amt:.0f} @ {px:.6g} "
                        f"({ptype}, 盈亏 {pnl:+.4f}U, tradeId={tid})")
        return got

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
            st_lv = self.db.get_grid_state(sym) or {}
            if lv["side"] == "sell":
                # 卖出受持仓容量约束：持仓减去交易所上已占用的卖单量。
                # 用户手动改单/部分成交都会让"账面量"与"可卖量"有差，
                # 不夹住就会 -4118 无限重试。
                try:
                    occupied = sum(float(o.get("amount") or 0) for o in
                                   (await self.client.fetch_open_orders(sym, "futures"))
                                   if o.get("side") == "sell")
                except Exception:
                    occupied = 0
                capacity = float(st_lv.get("base_qty", 0) or 0) - occupied
                amt_eff = min(float(lv["amount"]), max(0.0, capacity))
                if amt_eff < float(lv["amount"]) - 1e-9:
                    if amt_eff < 1e-9:
                        continue          # 没有容量了：留给下一轮裁剪/对账处理
                    self.db.conn.execute("UPDATE grid_levels SET amount=? WHERE id=?",
                                         (amt_eff, lv["id"]))
                    self.db.conn.commit()
                    lv = dict(lv, amount=amt_eff)
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
        # 同一标的的多空腿先对冲，再按标的绝对值求和。
        # 否则多空并存时会把敞口算成两倍，凭空把自己卡住。
        net: dict[str, float] = {}
        for p in positions:
            us = self.client.user_symbol(p.get("symbol", ""))
            mk = float(p.get("markPrice", 0) or p.get("entryPrice", 0) or 0)
            c = float(p.get("contracts", 0) or 0)
            cs = float(self.client.market_info(us, "futures").get("contractSize", 1) or 1)
            sign = -1.0 if p.get("side") == "short" else 1.0
            net[us] = net.get(us, 0.0) + sign * mk * c * cs
        total = sum(abs(v) for v in net.values())
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

    async def _record_short_fill(self, sym: str, qty: float, entry: float,
                                 res: dict, price: float, why: str):
        """市价平空后记账：价格/盈亏一律以**交易所真实成交**为准。

        不能用市价单返回的 price —— 它常是下单瞬间的行情快照（`average` 缺失时
        回退到 last price），与真实成交价能差 2%。QUSDT 实测：记成 0.054247
        （盈亏 -0.2373U），交易所真实成交 0.053143（盈亏 -0.1280U），凭空多亏 0.109U。
        """
        fill, _, ex_pnl = await self._recent_exit(sym, qty, side="buy", position_side="SHORT")
        fill = fill or float(res.get("price") or price)
        self._record_short_close(sym, qty, entry, fill, why, pnl_override=ex_pnl)

    def _record_short_close(self, sym: str, qty: float, entry: float,
                            exit_px: float, why: str, pnl_override: float = None):
        """把空单平仓补记进成交记录。空头是「跌了才赚」：pnl = (入场 − 出场) × 数量。"""
        if qty <= 0 or entry <= 0 or exit_px <= 0:
            return
        pnl = pnl_override if pnl_override is not None else (entry - exit_px) * qty
        self.db.insert_order({
            "mode": "grid", "symbol": sym, "side": "buy", "type": "short_close",
            "price": exit_px, "amount": qty, "cost": round(exit_px * qty, 6),
            "fee": round(exit_px * qty * 0.0005, 6),
            "pnl": round(pnl, 6),
            "pnl_rate": round(pnl / (entry * qty), 8),
            "dry_run": self.client.dry_run, "note": f"空单平仓·{why}"})
        log.info(f"GRID {sym}: 空单平仓已记账 —— 入场 {entry:.6g} / 出场 {exit_px:.6g} / "
                 f"盈亏 {pnl:+.4f}U（{why}）")

    def _short_view(self, sym: str, price: float):
        """空头腿的展示数据；该币从未参与空单则返回 None。

        多单卡片只读多头腿的数据，空头腿单独走这里 —— 否则同币多空并存时
        两边的强平价/浮盈会互相覆盖。
        """
        cfg = self.cfg
        m = (getattr(self, "_meta_short", {}) or {}).get(sym) or {}
        state = int(self.db.get_runtime_stat(f"short_state:{sym}", 0) or 0)
        done = self.db.get_runtime_stat(f"short_done:{sym}", 0) > 0
        # 数量/入场价优先取交易所元数据；只有 state 活跃(1挂单中/2持仓中)时才
        # 回退到本地持久化值 —— state=0（已平）时绝不回退，否则已平空单的残留
        # short_qty 会被当成「还在持仓」显示（BATUSDT 实测：空单早平了却显示持仓中）。
        qty = float(m.get("contracts", 0) or 0)
        if state in (1, 2) and qty <= 0:
            qty = float(self.db.get_runtime_stat(f"short_qty:{sym}", 0) or 0)
        if qty <= 0 and state != 1:
            # 既没持仓、也没有在挂的开空单 → 不显示这一块
            # （含"已平/不再开"的收尾状态：卡片上留一块空壳只是噪音）
            return None
        entry = float(m.get("entry", 0) or 0)
        if state in (1, 2) and entry <= 0:
            entry = float(self.db.get_runtime_stat(f"short_entry:{sym}", 0) or 0)
        sl = float(cfg.get("short_stop_pct", 0.035) or 0.035)
        tp = float(cfg.get("short_tp_pct", 0.10) or 0.10)
        return {
            "qty": qty,
            "entry": round(entry, 8),
            "stop": round(entry * (1 + sl), 8) if entry > 0 else 0,
            "tp": round(entry * (1 - tp), 8) if entry > 0 else 0,
            "liq_price": float(m.get("liq_price", 0) or 0),
            "unrealized": round(float(m.get("unrealized", 0) or 0), 4),
            # 空头：价格跌才赚，所以盈利率 = (入场价 − 现价)/入场价
            "upnl_rate": round((entry - price) / entry, 6) if entry > 0 else 0,
            "stop_on_exchange": bool(self.db.get_runtime_stat(f"short_stop_oid:{sym}", 0)),
            "state": "持仓中" if qty > 0 else ("挂单中" if state == 1 else "已平(不再开)"),
            "done": done,
        }

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
        # 读取交易所持仓元数据（强平价 / 杠杆 / 保证金模式）—— 安全信息，必须可见。
        # 多空腿分开存：同一个币可以同时有多单和空单，只按币种做键会互相覆盖。
        metaL, metaS = {}, {}
        live_ids: dict = {}      # symbol -> set(交易所当前挂单 id)，用于「已挂/待挂」的实况判定
        try:
            for p in await self.client.get_positions():
                us = self.client.user_symbol(p.get("symbol", ""))
                d = {
                    "liq_price": float(p.get("liquidationPrice", 0) or 0),
                    "leverage": p.get("leverage") or self.cfg.get("leverage"),
                    "margin_mode": p.get("marginMode") or "",
                    "unrealized": float(p.get("unrealizedPnl", 0) or 0),
                    "mark": float(p.get("markPrice", 0) or 0),
                    "entry": float(p.get("entryPrice", 0) or 0),
                    "contracts": float(p.get("contracts", 0) or 0),
                }
                (metaS if p.get("side") == "short" else metaL)[us] = d
        except Exception as e:
            log.warning(f"读取持仓元数据失败: {e}")
        meta = metaL                  # 多单卡片只看多头腿
        self._meta_short = metaS      # 空头腿单独给 _short_view 用

        out = []
        for st in self.db.get_grid_state(None) or []:
            sym = st["symbol"]
            price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            qty = float(st.get("base_qty", 0) or 0)
            cost = float(st.get("total_cost", 0) or 0)
            avg = cost / qty if qty > 0 else 0
            lv = self.db.get_grid_levels(sym)
            # 「已挂/待挂」以交易所为准：单号存在于交易所当前挂单里才算已挂。
            # DB 里存的单号可能已被成交/撤销/手动改单 —— 不能只信本地账。
            try:
                live_ids[sym] = {str(o.get("id")) for o in
                                 await self.client.fetch_open_orders(sym, "futures")}
            except Exception:
                live_ids[sym] = set()
            # 24h 振幅区间：现价落在当日高低的哪个位置（0=最低 1=最高）——
            # 选币时只给了振幅百分比，看不出「现在是在高点还是低点」。
            hl = self.client._hl.get(sym) or (0.0, 0.0)
            day_high, day_low = float(hl[0]), float(hl[1])
            day_pos = (max(0.0, min(1.0, (price - day_low) / (day_high - day_low)))
                       if day_high > day_low > 0 and price > 0 else 0.0)
            out.append({
                "symbol": sym,
                "day_high": round(day_high, 10), "day_low": round(day_low, 10),
                "day_pos": round(day_pos, 4),
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
                     "amount": l["amount"], "status": l["status"],
                     "placed": bool(l.get("order_id"))
                               and l["order_id"] in live_ids.get(sym, set())}
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
                # 空头腿（与多单并存；高位试空，单次/最小仓/带止损）
                "short": self._short_view(sym, price),
            })
        # 选中但还没建仓的标的（入场闸门在等回落）也要出卡片 —— 否则它会「隐形」：
        # 挂在选币清单里、空单也可能在跑，用户却在页面上找不到这个币。
        seen = {o["symbol"] for o in out}
        entry_max = float(self.cfg.get("entry_pos_max", 0.6) or 0.6)
        for s in (self._selection or (self.config.get("grid_selection") or [])):
            sym = s.get("symbol")
            if not sym or sym in seen:
                continue
            try:
                price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            except Exception:
                price = 0.0
            hl = self.client._hl.get(sym) or (0.0, 0.0)
            day_high, day_low = float(hl[0]), float(hl[1])
            day_pos = (max(0.0, min(1.0, (price - day_low) / (day_high - day_low)))
                       if day_high > day_low > 0 and price > 0 else 0.0)
            out.append({
                "symbol": sym, "status": "waiting",
                "lower": 0, "upper": 0, "grids": 0,
                "size_usdt": float(self.cfg.get("size_usdt", 0) or 0),
                "price": price, "base_qty": 0, "avg_price": 0,
                "position_value": 0, "cost_value": 0,
                "unrealized_pnl": 0, "unrealized_pnl_rate": 0,
                "realized_pnl": 0, "realized_pnl_rate": 0,
                "leverage": None, "margin_mode": "", "liq_price": 0, "liq_distance": None,
                "buy_count": 0, "sell_count": 0, "levels": [], "lots": [],
                "short": self._short_view(sym, price),
                "day_high": round(day_high, 10), "day_low": round(day_low, 10),
                "day_pos": round(day_pos, 4), "entry_pos_max": entry_max,
            })

        # 第三来源：**有活跃空单**但不在清单、也没有网格状态的标的（孤儿空单）。
        # 只修「管理」不修「显示」的话，它掉出清单的那一刻就会从页面上消失，
        # 用户无从知道手里还压着一笔空单（1000PEPE 实测发生两次）。
        for k in self.db.runtime_keys("short_state:"):
            sym = k.split(":", 1)[1]
            if sym in seen:
                continue
            if int(self.db.get_runtime_stat(k, 0) or 0) not in (1, 2):
                continue
            try:
                price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            except Exception:
                price = 0.0
            hl = self.client._hl.get(sym) or (0.0, 0.0)
            day_high, day_low = float(hl[0]), float(hl[1])
            day_pos = (max(0.0, min(1.0, (price - day_low) / (day_high - day_low)))
                       if day_high > day_low > 0 and price > 0 else 0.0)
            out.append({
                "symbol": sym, "status": "short_only",
                "lower": 0, "upper": 0, "grids": 0,
                "size_usdt": float(self.cfg.get("size_usdt", 0) or 0),
                "price": price, "base_qty": 0, "avg_price": 0,
                "position_value": 0, "cost_value": 0,
                "unrealized_pnl": 0, "unrealized_pnl_rate": 0,
                "realized_pnl": 0, "realized_pnl_rate": 0,
                "leverage": None, "margin_mode": "", "liq_price": 0, "liq_distance": None,
                "buy_count": 0, "sell_count": 0, "levels": [], "lots": [],
                "short": self._short_view(sym, price),
                "day_high": round(day_high, 10), "day_low": round(day_low, 10),
                "day_pos": round(day_pos, 4), "entry_pos_max": entry_max,
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
