"""方案C — 现货定投。

原理：按固定时间间隔、固定金额买入现货主流币，摊平成本、长期持有。
      现货无杠杆、无强平、无资金费，「不止损」天然成立。

风控：累计投入达到 max_total_usdt 后自动停止买入；只用现货可用余额。
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)


class DCAStrategy:
    mode = "dca"

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    @property
    def cfg(self) -> dict:
        return self.config.get("dca", {}) or {}

    async def step(self):
        cfg = self.cfg
        symbols = cfg.get("symbols", [])
        amount = float(cfg.get("amount_usdt", 0) or 0)
        interval_h = float(cfg.get("interval_hours", 24) or 24)
        max_total = float(cfg.get("max_total_usdt", 0) or 0)
        if not symbols or amount <= 0:
            return

        try:
            await self.client.ensure_spot()
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"DCA: market/price load failed, skip this tick: {e}")
            return

        now = datetime.now(timezone.utc)
        due_total = 0.0
        plan = []
        for sym in symbols:
            st = self.db.get_dca_state(sym) or {}
            invested = float(st.get("total_cost", 0) or 0)
            if max_total > 0:
                remain = max_total - sum(
                    float((self.db.get_dca_state(s) or {}).get("total_cost", 0) or 0)
                    for s in symbols)
                if remain <= 0:
                    continue
            last = st.get("last_buy_ts") or ""
            due = True
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    due = (now - last_dt) >= timedelta(hours=interval_h)
                except Exception:
                    due = True
            if due:
                plan.append(sym)
                due_total += amount

        if not plan:
            return
        if not cfg.get("first_buy_immediately", True):
            # 仅当所有标的都是首次（无 last_buy_ts）时才允许立刻买
            if all(not (self.db.get_dca_state(s) or {}).get("last_buy_ts") for s in plan):
                return

        # 余额检查（现货）
        if not self.client.dry_run:
            try:
                spot = await self.client.get_spot_balance("USDT")
                if spot.get("free", 0) < due_total:
                    log.warning(f"DCA: 现货可用 {spot.get('free', 0):.2f}U < 本期需 {due_total:.2f}U，跳过本期")
                    return
            except Exception as e:
                log.warning(f"DCA: 余额查询失败，跳过本期: {e}")
                return

        for sym in plan:
            try:
                await self._buy(sym, amount)
            except Exception as e:
                log.error(f"DCA {sym}: buy failed: {e}")

    async def _buy(self, sym: str, amount_usdt: float):
        cfg = self.cfg
        max_total = float(cfg.get("max_total_usdt", 0) or 0)
        st = self.db.get_dca_state(sym) or {}
        invested = float(st.get("total_cost", 0) or 0)
        if max_total > 0 and invested + amount_usdt > max_total:
            amount_usdt = max_total - invested
        if amount_usdt <= 0:
            return

        price = await self.client.ensure_price(sym)
        if price <= 0:
            log.warning(f"DCA {sym}: 无价格，跳过")
            return
        qty = self.client.amount_for_notional(sym, amount_usdt, price, "spot")
        if qty <= 0:
            log.warning(f"DCA {sym}: 数量计算为 0（金额 {amount_usdt}U 可能低于最小下单额）")
            return

        res = await self.client.create_market_order(sym, "buy", qty, "spot")
        if not res:
            return
        fill_price = float(res.get("price") or price)
        cost = fill_price * qty
        fee = cost * 0.001  # 现货 taker 0.1%
        now_iso = datetime.now(timezone.utc).isoformat()

        self.db.insert_dca_record({"ts": now_iso, "symbol": sym, "price": fill_price,
                                   "amount": qty, "cost": cost, "fee": fee,
                                   "dry_run": self.client.dry_run})
        new_cost = invested + cost
        new_qty = float(st.get("total_qty", 0) or 0) + qty
        self.db.upsert_dca_state(sym, total_cost=new_cost, total_qty=new_qty,
                                 avg_price=(new_cost / new_qty if new_qty > 0 else 0),
                                 buy_count=int(st.get("buy_count", 0) or 0) + 1,
                                 last_buy_ts=now_iso)
        self.db.insert_order({"mode": "dca", "symbol": sym, "side": "buy", "type": "dca_buy",
                              "price": fill_price, "amount": qty, "cost": round(cost, 6),
                              "fee": round(fee, 6), "dry_run": self.client.dry_run,
                              "note": "定期定投"})
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.info(f"{tag} DCA {sym} 买入 {qty} @ {fill_price:.6g} (≈{cost:.2f}U)")

    async def status(self) -> dict:
        out = []
        total_cost = total_value = 0.0
        for st in self.db.get_dca_state(None) or []:
            sym = st["symbol"]
            price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            qty = float(st.get("total_qty", 0) or 0)
            cost = float(st.get("total_cost", 0) or 0)
            value = qty * price
            total_cost += cost
            total_value += value
            out.append({
                "symbol": sym,
                "total_cost": round(cost, 4),
                "total_qty": qty,
                "avg_price": round(float(st.get("avg_price", 0) or 0), 6),
                "price": price,
                "value": round(value, 4),
                "pnl": round(value - cost, 4),
                "pnl_rate": round((value - cost) / cost, 6) if cost > 0 else 0,
                "buy_count": st.get("buy_count", 0),
                "last_buy_ts": st.get("last_buy_ts", ""),
            })
        return {
            "symbols": out,
            "total_cost": round(total_cost, 4),
            "total_value": round(total_value, 4),
            "total_pnl": round(total_value - total_cost, 4),
        }
