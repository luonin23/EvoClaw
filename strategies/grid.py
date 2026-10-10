"""方案B — 低杠杆网格。

模型（订单式，避免"瞬发成交"错误）：
  · 在 [lower, upper] 内布 N 个价格档位。
  · 现价**下方**的最近若干档挂买单；现价**上方**的最近若干档挂卖单。
  · 卖单由「底仓」背书 —— 初始化时按卖单数量市价买入底仓。
  · 买单成交 → 在上一档挂卖单；卖单成交 → 在下一档挂买单。如此循环吃档差。
  · 不设止损：价格跌出区间就一直持有底仓等回来；靠低杠杆 + 敞口闸门防强平。
"""

import logging

from exchange_client import _throttle

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

    @property
    def cfg(self) -> dict:
        return self.config.get("grid", {}) or {}

    # ===== 主循环 =====
    async def step(self):
        specs = self.cfg.get("symbols", [])
        if not specs:
            return
        symbols = [s.get("symbol") for s in specs if s.get("symbol")]
        try:
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"GRID 行情刷新失败，跳过本轮: {e}")
            return
        equity = await self._equity()
        for spec in specs:
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
                pct = float(self.cfg.get("auto_range_pct", 0.25) or 0.25)
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

        await self._check_fills(sym, price)
        if not self.client.dry_run:
            await self._place_live(sym, equity)

    # ===== 初始化 =====
    async def _init_grid(self, sym: str, lower: float, upper: float,
                         grids: int, size_usdt: float, price: float):
        pts = grid_points(lower, upper, grids)
        if not pts:
            return
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

        # 底仓：为卖单背书
        base_amt = 0.0
        for p in above:
            a = self.client.amount_for_notional(sym, size_usdt, p, "futures")
            if a > 0:
                base_amt += a
        created = 0
        if above:
            if base_amt <= 0:
                need = self.client.min_notional_of(sym, "futures")
                s = _throttle.emit(f"grid_nocap:{sym}")
                if s is not None:
                    log.warning(f"GRID {sym}: 单格 {size_usdt:.2f}U 买不起「一手」"
                                f"（最小约 {need:.1f}U/手）→ 跳过。请加大单格金额或换低价标的{s}")
                return
            self.db.insert_order({
                "mode": "grid", "symbol": sym, "side": "buy", "type": "grid_base",
                "price": price, "amount": base_amt, "cost": round(base_amt * price, 6),
                "fee": round(base_amt * price * 0.0005, 6),
                "dry_run": self.client.dry_run, "note": "网格底仓",
            })
            self.db.upsert_grid_state(sym, base_qty=base_amt, total_cost=base_amt * price)
            created += 1

        for i, p in enumerate(pts):
            a = self.client.amount_for_notional(sym, size_usdt, p, "futures")
            if a <= 0:
                continue
            rp = self.client.round_price(sym, p, "futures")
            if p < price and p in below:
                self.db.insert_grid_level(sym, i, rp, "buy", a)
                created += 1
            elif p > price and p in above:
                self.db.insert_grid_level(sym, i, rp, "sell", a)
                created += 1
        log.info(f"GRID {sym}: 区间 {lower:.6g}~{upper:.6g} / {grids} 档 / 单格 {size_usdt}U "
                 f"/ 底仓 {base_amt} / 挂单 {created - (1 if above else 0)} 条 (现价 {price:.6g})")

    # ===== 成交检测 =====
    async def _check_fills(self, sym: str, price: float):
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending":
                continue
            side, op = lv["side"], float(lv["price"])
            if side == "buy" and price <= op:
                await self._on_fill(sym, lv, "buy", op, price)
            elif side == "sell" and price >= op:
                await self._on_fill(sym, lv, "sell", op, price)

    async def _on_fill(self, sym: str, lv: dict, side: str, fill_price: float, market: float):
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
        await self._arm_next(sym, side, fill_price)

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

    async def _arm_next(self, sym: str, filled_side: str, fill_price: float):
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
        rp = self.client.round_price(sym, want, "futures")
        if self._has_pending(sym, rp):
            return
        amt = self.client.amount_for_notional(sym, size, rp, "futures")
        if amt <= 0:
            return
        idx = min(range(len(pts)), key=lambda i: abs(pts[i] - want))
        self.db.insert_grid_level(sym, idx, rp, side, amt)

    # ===== 实盘挂单 =====
    async def _place_live(self, sym: str, equity: float):
        for lv in self.db.get_grid_levels(sym):
            if lv["status"] != "pending" or lv["order_id"]:
                continue
            if lv["side"] == "buy" and not await self._exposure_ok(sym, equity):
                continue
            res = await self.client.create_limit_order(
                sym, lv["side"], float(lv["amount"]), float(lv["price"]), "futures")
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
                "unrealized_pnl": round((price - avg) * qty, 4) if qty > 0 else 0,
                "realized_pnl": round(float(st.get("realized_pnl", 0) or 0), 4),
                "buy_count": st.get("buy_count", 0), "sell_count": st.get("sell_count", 0),
                "levels": [
                    {"idx": l["level_idx"], "price": l["price"], "side": l["side"],
                     "amount": l["amount"], "status": l["status"]}
                    for l in lv if l["status"] == "pending"
                ],
            })
        return {"symbols": out}
