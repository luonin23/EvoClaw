"""方案B — 低杠杆网格。

模型（订单式，避免"瞬发成交"错误）：
  · 在 [lower, upper] 内布 N 个价格档位。
  · 现价**下方**的最近若干档挂买单；现价**上方**的最近若干档挂卖单。
  · 卖单由「底仓」背书 —— 初始化时按卖单数量市价买入底仓。
  · 买单成交 → 在上一档挂卖单；卖单成交 → 在下一档挂买单。如此循环吃档差。
  · 不设止损：价格跌出区间就一直持有底仓等回来；靠低杠杆 + 敞口闸门防强平。
"""

import logging
import time

from exchange_client import _throttle
from selector import select_grid_symbols

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
    async def _refresh_selection(self):
        """按配置周期重新选币（默认 6 小时一次）。

        引擎每 tick 会新建策略实例，因此选币结果与时间戳必须落库，
        否则会在每个 tick 重扫全市场（拉取全部 ticker，代价很高）。
        """
        cfg = self.cfg
        interval = float(cfg.get("refresh_hours", 6) or 6) * 3600
        now = time.time()
        ts = float(self.db.get_runtime_stat("grid_selection_ts", 0) or 0)
        if ts and (now - ts) < interval:
            # 沿用已落库的选币结果，但按当前 max_symbols 截断
            # （改了 max_symbols 不必等 6 小时才生效）
            n = int(cfg.get("max_symbols", 3) or 3)
            saved = (self.config.get("grid_selection") or [])[:n]
            if saved:
                self._selection = saved
                self._sel_ts = ts
                return
        size = float(cfg.get("size_usdt", 0) or 0)
        picks = await select_grid_symbols(
            self.client, cfg, size, int(cfg.get("max_symbols", 3) or 3))
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
            grids = int(cfg.get("grids", 9) or 9)
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
        if cfg.get("auto_select"):
            try:
                await self._refresh_selection()
            except Exception as e:
                log.warning(f"GRID 选币失败，沿用上次结果: {e}")

        specs = self._specs()
        active = {s["symbol"] for s in specs if s.get("symbol")}

        # 处理「掉出名单」：先标记，再继续维护它们的卖单
        self._reconcile_status(active)

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

        equity = await self._equity()
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

        grids = int(spec.get("grids") or 10)
        size_usdt = float(spec.get("size_usdt") or 0)
        if size_usdt <= 0 or grids < 2:
            return
        need_init = (
            st is None
            or abs(float(st["lower"]) - lower) > 1e-9
            or abs(float(st["upper"]) - upper) > 1e-9
            or int(st["grids"]) != grids
            or abs(float(st["size_usdt"]) - size_usdt) > 1e-9
        )
        if need_init:
            await self._init_grid(sym, lower, upper, grids, size_usdt, price)
            st = self.db.get_grid_state(sym)
            if not st:
                return

        draining = (st.get("status") or "active") == "draining"
        await self._check_fills(sym, price, draining)
        if draining:
            st2 = self.db.get_grid_state(sym) or {}
            if float(st2.get("base_qty", 0) or 0) <= 1e-12:
                self._retire(sym)
                return
        if not self.client.dry_run:
            await self._reconcile_orders(sym, price)
            await self._place_live(sym, equity, draining)

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
                         grids: int, size_usdt: float, price: float):
        pts = grid_points(lower, upper, grids)
        if not pts:
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
        # 先清空旧状态
        self.db.replace_grid_levels(sym, [])
        self.db.upsert_grid_state(sym, lower=lower, upper=upper, grids=grids,
                                  size_usdt=size_usdt, base_qty=0.0, total_cost=0.0,
                                  realized_pnl=0.0, buy_count=0, sell_count=0)

        half = max(1, grids // 2)
        below = [p for p in pts if p < price][-half:]      # 现价下方最近的 half 档
        above = [p for p in pts if p > price][:half]       # 现价上方最近的 half 档

        # 先算好卖单（离现价最近的排最后，便于补量）
        sells = []   # [[level_idx, price, amount], ...]
        for i, p in enumerate(pts):
            if p > price and p in above:
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
            if not self.client.dry_run:
                cur = await self._current_position(sym)
                delta = base_amt - cur
                if abs(delta) >= 1:
                    side = "buy" if delta > 0 else "sell"
                    res = await self.client.create_market_order(
                        sym, side, abs(delta), "futures", position_side="LONG")
                    if not res:
                        log.error(f"GRID {sym}: 底仓调整失败({side} {abs(delta):.0f})，放弃本次初始化")
                        return
                    fill = float(res.get("price") or price)
                    log.info(f"GRID {sym}: 底仓调整 {cur:.0f} → {base_amt:.0f} ({side} {abs(delta):.0f})")
                elif cur > 0:
                    fill = await self.client.ensure_price(sym) or price
            self.db.insert_order({
                "mode": "grid", "symbol": sym, "side": "buy", "type": "grid_base",
                "price": fill, "amount": base_amt, "cost": round(base_amt * fill, 6),
                "fee": round(base_amt * fill * 0.0005, 6),
                "dry_run": self.client.dry_run, "note": "网格底仓",
            })
            self.db.upsert_grid_state(sym, base_qty=base_amt, total_cost=base_amt * fill)
            created += 1

        # 挂单：买单（现价下方）+ 卖单（用上面算好的量，保证底仓=卖单总量）
        sell_idx = {s[0]: s for s in sells}
        for i, p in enumerate(pts):
            rp = self.client.round_price(sym, p, "futures")
            if p < price and p in below:
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
            side, op = lv["side"], float(lv["price"])
            if side == "buy" and price <= op:
                await self._on_fill(sym, lv, "buy", op, price, draining)
            elif side == "sell" and price >= op:
                await self._on_fill(sym, lv, "sell", op, price, draining)

    async def _on_fill(self, sym: str, lv: dict, side: str, fill_price: float,
                       market: float, draining: bool = False):
        amt = float(lv["amount"])
        st = self.db.get_grid_state(sym) or {}
        qty = float(st.get("base_qty", 0) or 0)
        cost = float(st.get("total_cost", 0) or 0)
        realized = float(st.get("realized_pnl", 0) or 0)

        self.db.fill_grid_level(lv["id"])
        if side == "buy":
            qty += amt
            cost += fill_price * amt
            self.db.upsert_grid_state(sym, base_qty=qty, total_cost=cost,
                                      buy_count=int(st.get("buy_count", 0) or 0) + 1)
        else:
            avg = cost / qty if qty > 0 else fill_price
            realized += (fill_price - avg) * amt
            qty = max(0.0, qty - amt)
            cost = max(0.0, cost - avg * amt)
            self.db.upsert_grid_state(sym, base_qty=qty, total_cost=cost, realized_pnl=realized,
                                      sell_count=int(st.get("sell_count", 0) or 0) + 1)

        self.db.insert_order({
            "mode": "grid", "symbol": sym, "side": side, "type": f"grid_{side}",
            "price": fill_price, "amount": amt, "cost": round(fill_price * amt, 6),
            "fee": round(fill_price * amt * 0.0005, 6),
            "dry_run": self.client.dry_run, "note": "网格成交",
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

    def _has_pending(self, sym: str, price: float, tol: float = 1e-9) -> bool:
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] == "pending" and abs(float(lv["price"]) - price) <= max(tol, price * 1e-6):
                return True
        return False

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
        if self._has_pending(sym, rp):
            return
        amt = self.client.amount_for_notional(sym, size, rp, "futures")
        if amt <= 0:
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
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending" or lv["order_id"]:
                continue
            if lv["side"] == "buy":
                if draining:
                    continue          # 收缩中：不挂买单
                if not await self._exposure_ok(sym, equity):
                    continue
            res = await self.client.create_limit_order(
                sym, lv["side"], float(lv["amount"]), float(lv["price"]), "futures",
                position_side="LONG")
            if res:
                self.db.conn.execute("UPDATE grid_levels SET order_id=? WHERE id=?",
                                     (res["order_id"], lv["id"]))
                self.db.conn.commit()

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
        log.info(f"GRID {sym}: 存货已清空，网格退役（不再纳入管理）")

    # ===== 前端状态 =====
    async def status(self) -> dict:
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
                "unrealized_pnl": round((price - avg) * qty, 4) if qty > 0 else 0,
                "unrealized_pnl_rate": round((price - avg) / avg, 6) if avg > 0 else 0,
                "realized_pnl": round(float(st.get("realized_pnl", 0) or 0), 6),
                "realized_pnl_rate": round(float(st.get("realized_pnl", 0) or 0) / cost, 6) if cost > 0 else 0,
                "status": st.get("status", "active"),
                "buy_count": st.get("buy_count", 0), "sell_count": st.get("sell_count", 0),
                "levels": [
                    {"idx": l["level_idx"], "price": l["price"], "side": l["side"],
                     "amount": l["amount"], "status": l["status"]}
                    for l in lv if l["status"] == "pending"
                ],
            })
        cfg = self.cfg
        n = int(cfg.get("max_symbols", 3) or 3)
        sel = (self._selection or (self.config.get("grid_selection") or []))[:n]
        return {
            "symbols": out,
            "auto_select": bool(cfg.get("auto_select")),
            "selection": [{"symbol": s["symbol"], "range": s.get("range"),
                           "quote_volume": s.get("quote_volume")} for s in sel],
            "max_symbols": int(cfg.get("max_symbols", 3) or 3),
        }
