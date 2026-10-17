"""现货交易 —— 现货马丁格尔网格。

用户定的方向：现货就用网格来交易（马丁格尔）。一轮循环：

    ① 首单：市价买入 amount_usdt，立即建仓
    ② 同时挂两张单等成交：
       · 加仓单  上一档成交价 ×(1−step_dev)，金额 = 上一档 × step_scale
         （跌了加倍摊平 —— 马丁的核心）；步数用完或单轮投入到顶就不再加仓
       · 止盈单  加权均价 ×(1+tp_pct)，卖量 = 本轮全部持仓
    ③ 加仓成交 → 均价下降 → 撤旧止盈、挂新止盈 + 下一档加仓
    ④ 止盈成交 → 本轮结束 → 撤掉还在等的加仓单 → 立即开下一轮

状态单一真源：spot_orders 表里 purpose="dca" 的成交记录。持仓/均价/档位
全部由成交记录**推导**，不存影子状态 —— 与利润定投（purpose="profit_dca"，
只买不卖）天然隔离：同币同开互不污染，卖出也绝不会把利润定投买的币卖掉。

资金：与利润定投同一套划转 —— 现货缺多少从合约划多少，只划缺口；划不动
就不下单，等下一 tick 再试。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import params as P
from strategies.profit_dca import MAX_OVERSPEND

log = logging.getLogger(__name__)

PURPOSE = "dca"
NOTE_BUY = "现货网格"
NOTE_SELL = "现货网格·止盈"
FEE = 0.001                    # 币安现货 taker 0.1%，双边都按它近似
SEEN_KEY = "dca_seen:{sym}"    # 新标的第一次被策略见到的时刻（首轮延迟用）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_asset(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


class DCAStrategy:
    mode = "dca"

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    @property
    def cfg(self) -> dict:
        return self.config.get("dca", {}) or {}

    def _num(self, key: str) -> float:
        """读数值参数：缺键时用**注册表**的默认值，不写字面量兜底。"""
        full = f"dca.{key}"
        p = P.by_key()[full]
        v = P.dotted_get(self.config, full, None)
        return float(p.default if v is None else v)

    # ---------- 主循环 ----------
    async def step(self):
        cfg = self.cfg
        # 引擎在 enabled 或 mode="dca" 时才会构造本策略；这里只拦「标的钱数」
        symbols = [s for s in (cfg.get("symbols") or []) if s]
        if not symbols or self._num("amount_usdt") <= 0:   # 哨兵：0 = 不建仓
            return
        try:
            await self.client.ensure_spot()
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"现货网格: 行情加载失败，本轮跳过: {e}")
            return
        for sym in symbols:
            try:
                await self._tick_symbol(sym)
            except Exception as e:
                log.error(f"现货网格 {sym}: 处理失败: {e}", exc_info=True)

    async def _tick_symbol(self, sym: str):
        # 1) 先结在途挂单（成交/撤销）——里面可能触发撤旧止盈、撤加仓
        await self._settle_pending(sym)
        # 2) 从成交记录推导本轮状态（单一真源）
        st = self._round_state(sym)
        # 3) 无持仓：在途单撤干净了才开新一轮（撤单失败还挂着就不急这一 tick）
        if st["qty"] <= 1e-12:
            if not self._pending_of(sym):
                await self._start_round(sym, st)
            return
        # 4) 持仓中：保证止盈单和（允许的）加仓单都挂着
        await self._ensure_quotes(sym, st)

    # ---------- 轮次状态：全部由成交记录推导 ----------
    def _round_state(self, sym: str) -> dict:
        """本轮 = 最近一张成交卖单之后的全部成交买单（含那张卖单本身）。

        轮结束（整轮卖出）后累计值要清零重来，所以不能简单 Σ 全表 —— 必须按
        卖单切片。空仓时 cost/avg 归零，历史轮数单独由 sells 计数。
        """
        fills = self.db.filled_spot_orders(PURPOSE, sym)
        buys = [r for r in fills if r["side"] == "buy"]
        sells = [r for r in fills if r["side"] == "sell"]
        last_sell_id = max((int(r["id"]) for r in sells), default=0)
        rb = [r for r in buys if int(r["id"]) > last_sell_id]
        rs = [r for r in sells if int(r["id"]) > last_sell_id]
        qty = sum(float(r["amount"]) for r in rb) - sum(float(r["amount"]) for r in rs)
        cost = sum(float(r["cost_usdt"]) for r in rb)
        buy_qty = sum(float(r["amount"]) for r in rb)
        last = rb[-1] if rb else None
        return {
            "qty": qty,
            "cost": cost,
            "avg": (cost / buy_qty) if buy_qty > 0 else 0.0,
            "steps": max(0, len(rb) - 1),      # 首单不算加仓
            "last_price": float(last["price"]) if last else 0.0,
            "last_cost": float(last["cost_usdt"]) if last else 0.0,
            "rounds": len(sells),              # 历史完成的轮数
        }

    def _pending_of(self, sym: str) -> list[dict]:
        return [r for r in self.db.pending_spot_orders(PURPOSE) if r["symbol"] == sym]

    # ---------- 在途挂单结算 ----------
    async def _settle_pending(self, sym: str):
        for row in self._pending_of(sym):
            cur = self.db.get_spot_order(row["id"])
            if not cur or cur["status"] != "pending":
                # 同一 tick 里已被连带撤掉/结掉（如加仓成交 → 撤旧止盈）
                continue
            if self.client.dry_run:
                # 模拟盘没有真实订单可查：按挂单价成交，走完整记账流程
                await self._on_fill(row, float(row["price"]), float(row["amount"]))
                self.db.update_spot_order(row["id"], "filled", _now())
                continue
            try:
                o = await self.client.fetch_spot_order(row["order_id"], row["symbol"])
            except Exception as e:
                log.warning(f"现货网格 {sym}: 查单失败，保留挂单状态下轮再查: {e}")
                continue
            if o is None:
                continue
            stat = str(o.get("status") or "")
            if stat in ("closed", "filled"):
                fill = float(o.get("average") or o.get("price") or row["price"])
                amt = float(o.get("filled") or row["amount"])
                await self._on_fill(row, fill, amt)
                self.db.update_spot_order(row["id"], "filled", _now())
            elif stat in ("canceled", "expired", "rejected"):
                self.db.update_spot_order(row["id"], "cancelled")
                log.warning(f"现货网格 {sym}: 挂单被交易所撤销（{stat}），下轮重新挂")

    async def _on_fill(self, row: dict, fill: float, amount: float):
        if row["side"] == "buy":
            await self._record_buy(row["symbol"], fill, amount)
            # 均价已变 → 旧止盈价作废，撤掉重挂
            await self._cancel_pending(row["symbol"], "sell")
        else:
            await self._record_sell(row, fill, amount)
            # 本轮结束 → 撤掉还没成交的加仓单
            await self._cancel_pending(row["symbol"], "buy")

    async def _cancel_pending(self, sym: str, side: str):
        """撤掉某个方向的在途单。撤失败就保留 pending：下轮 settle 再查 ——
        若其实已成交会照常记账，若还挂着会再撤一次。宁可慢一步，不能账实不符。"""
        for row in self._pending_of(sym):
            if row["side"] != side:
                continue
            ok = True
            if not self.client.dry_run:
                try:
                    ok = await self.client.cancel_spot_order(row["order_id"], row["symbol"])
                except Exception as e:
                    log.warning(f"现货网格 {sym}: 撤单失败，保留待下轮: {e}")
                    ok = False
            if ok:
                self.db.update_spot_order(row["id"], "cancelled")

    # ---------- 成交记账 ----------
    async def _record_buy(self, sym: str, fill: float, amount: float):
        cost = fill * amount
        self.db.insert_dca_record({"ts": _now(), "symbol": sym, "price": fill,
                                   "amount": amount, "cost": cost, "fee": cost * FEE,
                                   "dry_run": self.client.dry_run, "note": NOTE_BUY})
        # dca_state 是现货总持仓台账（利润定投的买入也累在这里），同步更新
        st = self.db.get_dca_state(sym) or {}
        new_cost = float(st.get("total_cost", 0) or 0) + cost
        new_qty = float(st.get("total_qty", 0) or 0) + amount
        self.db.upsert_dca_state(sym, total_cost=new_cost, total_qty=new_qty,
                                 avg_price=(new_cost / new_qty if new_qty > 0 else 0),
                                 buy_count=int(st.get("buy_count", 0) or 0) + 1,
                                 last_buy_ts=_now())
        self.db.insert_order({"mode": "dca", "symbol": sym, "side": "buy",
                              "type": "dca_buy", "price": fill, "amount": amount,
                              "cost": round(cost, 6), "fee": round(cost * FEE, 6),
                              "dry_run": self.client.dry_run, "note": NOTE_BUY})
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} 现货网格 {sym}: 买入 {amount:.8g} @ {fill:.6g}（≈{cost:.2f}U）")

    async def _record_sell(self, row: dict, fill: float, amount: float):
        sym = row["symbol"]
        proceeds = fill * amount
        # 本轮成本 = 上一张更早的卖单之后的全部买入成本
        fills = self.db.filled_spot_orders(PURPOSE, sym)
        buys = [r for r in fills if r["side"] == "buy" and int(r["id"]) < int(row["id"])]
        earlier_sells = [r for r in fills if r["side"] == "sell"
                         and int(r["id"]) < int(row["id"])]
        last_sell_id = max((int(r["id"]) for r in earlier_sells), default=0)
        cost = sum(float(r["cost_usdt"]) for r in buys if int(r["id"]) > last_sell_id)
        fees = (cost + proceeds) * FEE
        pnl = proceeds - cost - fees
        self.db.insert_dca_record({"ts": _now(), "symbol": sym, "price": fill,
                                   "amount": amount, "cost": proceeds, "fee": fees,
                                   "dry_run": self.client.dry_run, "note": NOTE_SELL})
        self.db.insert_order({"mode": "dca", "symbol": sym, "side": "sell",
                              "type": "dca_sell", "price": fill, "amount": amount,
                              "cost": round(proceeds, 6), "fee": round(fees, 6),
                              "pnl": round(pnl, 6),
                              "pnl_rate": round(pnl / cost, 6) if cost > 0 else 0,
                              "dry_run": self.client.dry_run, "note": NOTE_SELL})
        # 总台账同步减仓（只减本轮卖出的量，利润定投买的份额不动）
        st = self.db.get_dca_state(sym) or {}
        new_qty = max(0.0, float(st.get("total_qty", 0) or 0) - amount)
        new_cost = max(0.0, float(st.get("total_cost", 0) or 0) - cost)
        self.db.upsert_dca_state(sym, total_cost=new_cost, total_qty=new_qty,
                                 avg_price=(new_cost / new_qty if new_qty > 0 else 0))
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} 现货网格 {sym}: 止盈卖出 {amount:.8g} @ {fill:.6g}，"
                    f"本轮投入 {cost:.2f}U → 盈亏 {pnl:+.2f}U，一轮完成")

    # ---------- 开新一轮 ----------
    async def _start_round(self, sym: str, st: dict):
        amount = self._num("amount_usdt")
        if amount <= 0:
            return
        if st["rounds"] == 0 and not self._first_due(sym):
            return
        price = await self.client.ensure_price(sym)
        if price <= 0:
            return
        qty = self.client.amount_for_notional(sym, amount, price, "spot")
        if qty <= 0:
            log.info(f"现货网格 {sym}: 首单 {amount:.2f}U 低于最小一手，先攒着")
            return
        notional = qty * price
        if notional > amount * MAX_OVERSPEND:
            log.warning(f"现货网格 {sym}: 最小一手 {notional:.2f}U 远超首单预算 "
                        f"{amount:.2f}U，跳过（换标的或调大首单金额）")
            return
        ok, moved = await self.client.ensure_spot_funds(notional)
        if not ok:
            log.warning(f"现货网格 {sym}: 现货可用不足且无法从合约划转"
                        f"（需 {notional:.2f}U），本轮不开仓，下轮再试")
            return
        res = await self.client.create_market_order(sym, "buy", qty, "spot")
        if not res:
            return
        fill = float(res.get("price") or price)
        # 市价单即时成交：直接落一条 filled 行，让持仓推导从这里开始
        self.db.insert_spot_order({"symbol": sym, "side": "buy", "amount": qty,
                                   "price": fill, "cost_usdt": fill * qty,
                                   "order_id": res.get("order_id", ""),
                                   "purpose": PURPOSE, "status": "filled",
                                   "filled_ts": _now()})
        await self._record_buy(sym, fill, qty)
        await self._ensure_quotes(sym, self._round_state(sym))

    def _first_due(self, sym: str) -> bool:
        """新标的的第一轮：默认立即建仓；开关关闭时要等 interval_hours
        （从策略第一次见到这个标的算起）。之后的每一轮都是止盈后立即重启。"""
        if self.cfg.get("first_buy_immediately", True):
            return True
        key = SEEN_KEY.format(sym=sym)
        seen = float(self.db.get_runtime_stat(key, 0) or 0)
        if seen <= 0:
            self.db.set_runtime_stat(key, time.time())
            return False
        return (time.time() - seen) >= self._num("interval_hours") * 3600

    # ---------- 挂单维护 ----------
    async def _ensure_quotes(self, sym: str, st: dict):
        pend = self._pending_of(sym)
        if not any(r["side"] == "sell" for r in pend):
            await self._place_tp(sym, st)
        if st["steps"] < int(self._num("max_steps")) and \
                not any(r["side"] == "buy" for r in pend):
            await self._place_next_buy(sym, st)

    async def _place_tp(self, sym: str, st: dict):
        tp = self._num("tp_pct")
        px = self.client.round_price(sym, st["avg"] * (1 + tp), "spot")
        if px <= 0:
            return
        qty = st["qty"]
        if not self.client.dry_run:
            try:
                # 物理余额可能比推导持仓略少（手续费扣币等），卖出量绝不能超过余额
                bal = await self.client.get_spot_balance(_base_asset(sym))
                qty = min(qty, float(bal.get("free", 0) or 0))
            except Exception as e:
                log.info(f"现货网格 {sym}: 余额查询失败，按推导持仓挂卖单: {e}")
        # 模拟盘没有真实余额可言，直接按推导持仓挂 —— 否则卖量恒为 0，链路走不通
        qty = self.client.round_amount(sym, qty, "spot")
        if qty <= 0:
            return
        lo = self.client.min_amount(sym, px, "spot")
        if qty < lo:
            log.info(f"现货网格 {sym}: 可卖量 {qty:.8g} 不足最小一手 {lo:.8g}，"
                     f"先攒着")
            return
        res = await self.client.create_limit_order(sym, "sell", qty, px, "spot")
        if not res:
            return
        self.db.insert_spot_order({"symbol": sym, "side": "sell", "amount": qty,
                                   "price": px, "cost_usdt": qty * px,
                                   "order_id": res.get("order_id", ""), "purpose": PURPOSE})
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} 现货网格 {sym}: 挂止盈卖单 {qty:.8g} @ {px:.6g}"
                    f"（均价 {st['avg']:.6g} 上 {tp * 100:.1f}%）")

    async def _place_next_buy(self, sym: str, st: dict):
        dev = self._num("step_dev")
        cost = st["last_cost"] * self._num("step_scale")
        cap = self._num("max_total_usdt")
        if cap > 0 and st["cost"] + cost > cap:
            return    # 单轮预算用完：躺平等反弹到止盈
        px = self.client.round_price(sym, st["last_price"] * (1 - dev), "spot")
        if px <= 0:
            return
        qty = self.client.amount_for_notional(sym, cost, px, "spot")
        if qty <= 0:
            return
        notional = qty * px
        if notional > cost * MAX_OVERSPEND:
            log.info(f"现货网格 {sym}: 下一档加仓最小一手 {notional:.2f}U 超过预算 "
                     f"{cost:.2f}U，不再加仓")
            return
        # 现货限价买单下单时就要有真金白银，先把缺口从合约划过来
        ok, moved = await self.client.ensure_spot_funds(notional)
        if not ok:
            log.warning(f"现货网格 {sym}: 现货可用不足且无法从合约划转"
                        f"（需 {notional:.2f}U），加仓单先不挂，下轮再试")
            return
        res = await self.client.create_limit_order(sym, "buy", qty, px, "spot")
        if not res:
            return
        self.db.insert_spot_order({"symbol": sym, "side": "buy", "amount": qty,
                                   "price": px, "cost_usdt": notional,
                                   "order_id": res.get("order_id", ""), "purpose": PURPOSE})
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} 现货网格 {sym}: 挂第 {st['steps'] + 2} 档加仓买单 "
                    f"{qty:.8g} @ {px:.6g}（≈{notional:.2f}U，划转 {moved:.2f}U）")

    # ---------- 展示 ----------
    async def status(self) -> dict:
        symbols = [s for s in (self.cfg.get("symbols") or []) if s]
        dev = self._num("step_dev")
        max_steps = int(self._num("max_steps"))
        tp = self._num("tp_pct")
        rounds, realized = self.db.dca_round_stats()
        out = []
        total_cost = total_value = 0.0
        for st_row in self.db.get_dca_state(None) or []:
            sym = st_row["symbol"]
            price = self.client.last_price(sym) or await self.client.ensure_price(sym)
            qty = float(st_row.get("total_qty", 0) or 0)
            cost = float(st_row.get("total_cost", 0) or 0)
            value = qty * price
            total_cost += cost
            total_value += value
            row = {
                "symbol": sym,
                "total_cost": round(cost, 4),
                "total_qty": qty,
                "avg_price": round(float(st_row.get("avg_price", 0) or 0), 6),
                "price": price,
                "value": round(value, 4),
                "pnl": round(value - cost, 4),
                "pnl_rate": round((value - cost) / cost, 6) if cost > 0 else 0,
                "buy_count": st_row.get("buy_count", 0),
                "last_buy_ts": st_row.get("last_buy_ts", ""),
            }
            if sym in symbols:
                g = self._round_state(sym)
                row["grid"] = {
                    "active": g["qty"] > 1e-12,
                    "steps": g["steps"],
                    "max_steps": max_steps,
                    "round_qty": round(g["qty"], 8),
                    "round_cost": round(g["cost"], 4),
                    "round_avg": round(g["avg"], 6),
                    "unreal_pnl": round(g["qty"] * price - g["cost"], 4) if g["qty"] > 0 else 0,
                    "tp_price": round(g["avg"] * (1 + tp), 8) if g["qty"] > 0 else None,
                    "next_buy_price": (round(g["last_price"] * (1 - dev), 8)
                                       if g["qty"] > 0 and g["steps"] < max_steps else None),
                    "pending": [
                        {"side": p["side"], "price": p["price"], "amount": p["amount"],
                         "cost_usdt": p.get("cost_usdt", 0), "order_id": p["order_id"]}
                        for p in self._pending_of(sym)],
                }
            out.append(row)
        return {
            "symbols": out,
            "total_cost": round(total_cost, 4),
            "total_value": round(total_value, 4),
            "total_pnl": round(total_value - total_cost, 4),
            "rounds": rounds,
            "realized_pnl": round(realized, 4),
        }
