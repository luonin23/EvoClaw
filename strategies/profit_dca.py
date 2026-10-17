"""利润定投 —— 用合约赚到的钱买现货。

用户定的规则：
  · **每档**合约累计已实现盈利每上涨 `step_usdt`，就算一个台阶；
  · 每个台阶拿出**该台阶利润**的 `ratio%` 买入现货；
  · **不能循环定投**——不是「每轮循环投一次」，而是「盈利真的涨上去了才投」。

「不循环」靠一条**只升不降的水位线**（runtime_stat `profit_dca_anchor`）实现：

    盈利  0 ─────► 24 ─────► 12 ─────► 28 ─────► 44
    水位  0        0         20        20        40
    触发  —        ✅投6U    ❌不投    ❌不投    ✅投6U

  盈利回吐到 12 再涨回 28：**不触发**（水位线没降，必须有新利润）。
  涨到 44 才越过 20→40 的新台阶 → 再投一次。

**资金在合约账户**：下单前必须先把 USDT 从合约划转到现货（否则现货余额不够、
订单会被拒）。只划缺口，绝不多划。

**支持挂单**：`entry_offset > 0` 时按「现价 ×(1−offset)」挂限价买单；
挂单跨 tick 跟踪，**只有真的成交才推进水位线**。`entry_offset = 0` 时市价买入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import params as P

log = logging.getLogger(__name__)

ANCHOR_KEY = "profit_dca_anchor"
NOTE = "利润定投"
PURPOSE = "profit_dca"

# 单笔允许超出预算的上限。低于它才买：交易所的「最小下单额」会把小额预算抬高
# （币安现货 ≈5U），不对齐步长又会溢出一点，所以留 20% 余量。
MAX_OVERSPEND = 1.2


class ProfitDCA:
    """无状态（全部状态落库），每个 tick 新建也无所谓。"""

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    @property
    def cfg(self) -> dict:
        return self.config.get("profit_dca", {}) or {}

    def _num(self, key: str) -> float:
        """读数值参数：缺键时用**注册表**的默认值，不再写字面量。

        以前这里用 0 作为兜底，而注册表默认是 2%（挂单）—— 两者不一致，
        「改了没生效」就是这么来的。
        """
        full = f"profit_dca.{key}"
        p = P.by_key()[full]
        v = P.dotted_get(self.config, full, None)
        return float(p.default if v is None else v)

    def _anchor(self) -> float:
        return float(self.db.get_runtime_stat(ANCHOR_KEY, 0) or 0)

    def _set_anchor(self, v: float):
        self.db.set_runtime_stat(ANCHOR_KEY, float(v))

    # ---------- 触发计算（无副作用） ----------
    def pending_steps(self) -> tuple[int, float, float]:
        """返回 (可执行档数, 每档投入 U, 本次应投 U)。**无任何副作用**。"""
        cfg = self.cfg
        if not cfg.get("enabled"):
            return 0, 0.0, 0.0
        step = float(cfg.get("step_usdt") or 0)
        ratio = float(cfg.get("ratio") or 0)
        if step <= 0 or ratio <= 0:
            return 0, 0.0, 0.0
        per = step * ratio
        gap = self.db.realized_pnl_total() - self._anchor()
        n = int(gap // step) if gap >= step else 0
        if n <= 0:
            return 0, per, 0.0
        cap = float(cfg.get("max_total") or 0)
        if cap > 0:
            left = cap - self.db.dca_invested_total(NOTE)
            n = min(n, int(left / per)) if per > 0 else 0
            if n <= 0:
                return 0, per, 0.0
        return n, per, n * per

    # ---------- 主流程 ----------
    async def step(self):
        cfg = self.cfg
        if not cfg.get("enabled"):
            return

        # 1) 先把已挂出的限价单结掉（成交/撤销）。有挂单在途时本轮不再开新单。
        if await self._settle_pending():
            return

        # 2) 算触发
        n, per_step, invest = self.pending_steps()
        if n <= 0 or invest <= 0:
            return
        symbols = [s for s in (cfg.get("symbols") or []) if s]
        if not symbols:
            log.warning(f"{NOTE}: 已触发 {n} 档（应投 {invest:.2f}U）但没有勾选任何标的，"
                        f"先在「利润定投 · 定投标的」里选几个")
            return

        offset = self._num("entry_offset")
        ratio = float(cfg.get("ratio") or 0)
        if offset > 0:
            await self._place_limits(symbols, invest, ratio, offset)
        else:
            await self._place_market(symbols, invest, ratio, n)

    # ---------- 限价挂单 ----------
    async def _place_limits(self, symbols: list[str], invest: float, ratio: float,
                            offset: float):
        """按「现价 ×(1−offset)」挂限价买单。先把资金从合约划到现货。"""
        try:
            await self.client.ensure_spot()
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"{NOTE}: 行情加载失败，本轮跳过: {e}")
            return

        each = invest / max(1, len(symbols))
        intents = []
        for sym in symbols:
            price = await self.client.ensure_price(sym)
            if price <= 0:
                log.info(f"{NOTE} {sym}: 拿不到价格，跳过")
                continue
            limit_px = self.client.round_price(sym, price * (1 - offset), "spot")
            if limit_px <= 0:
                continue
            qty = self.client.amount_for_notional(sym, each, limit_px, "spot")
            if qty <= 0:
                continue
            notional = qty * limit_px
            if notional > each * MAX_OVERSPEND:
                log.info(f"{NOTE} {sym}: 最小一手 {notional:.2f}U 远超本次预算 "
                         f"{each:.2f}U，跳过（等攒够）")
                continue
            intents.append({"symbol": sym, "amount": qty, "price": limit_px,
                            "notional": notional})
        if not intents:
            log.warning(f"{NOTE}: 没有可挂的标的（预算 {each:.2f}U/个，"
                        f"可能低于交易所最小下单额），水位线不推进")
            return

        need = sum(i["notional"] for i in intents)
        ok, moved = await self.client.ensure_spot_funds(need)
        if not ok:
            log.warning(f"{NOTE}: 现货可用不足且无法从合约划转（需 {need:.2f}U），"
                        f"本轮不挂单；水位线不推进，下轮再试")
            return

        placed = 0
        for it in intents:
            res = await self.client.create_limit_order(
                it["symbol"], "buy", it["amount"], it["price"], "spot")
            if not res:
                continue
            self.db.insert_spot_order({
                "symbol": it["symbol"], "side": "buy", "amount": it["amount"],
                "price": it["price"], "cost_usdt": it["notional"],
                "order_id": res.get("order_id", ""), "purpose": PURPOSE,
                # 这笔单对应的利润份额：花掉的钱 ÷ 比例
                "profit_share": (it["notional"] / ratio) if ratio > 0 else 0.0,
            })
            placed += 1
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        if placed:
            log.warning(f"{tag} {NOTE}: 已挂 {placed} 张限价买单（现价下 {offset*100:.1f}%，"
                        f"合计 {need:.2f}U；本轮划转 {moved:.2f}U）。成交后才推进水位线")

    async def _settle_pending(self) -> bool:
        """结掉在途的限价单。返回 True 表示本轮有挂单需要继续等待。"""
        rows = self.db.pending_spot_orders(PURPOSE)
        if not rows:
            return False
        still = False
        for row in rows:
            if self.client.dry_run:
                # 模拟盘没有真实订单可查：按挂单价成交，走完整记账流程
                await self._on_filled(row, float(row["price"]), float(row["amount"]))
                self.db.update_spot_order(row["id"], "filled",
                                          datetime.now(timezone.utc).isoformat())
                continue
            o = await self.client.fetch_spot_order(row["order_id"], row["symbol"])
            if o is None:
                still = True
                continue
            st = str(o.get("status") or "")
            if st in ("closed", "filled"):
                fill = float(o.get("average") or o.get("price") or row["price"])
                amt = float(o.get("filled") or row["amount"])
                await self._on_filled(row, fill, amt)
                self.db.update_spot_order(row["id"], "filled",
                                          datetime.now(timezone.utc).isoformat())
                log.warning(f"{NOTE} {row['symbol']}: 限价单成交 {amt:.8g} @ {fill:.6g}，"
                            f"水位线推进 {row['profit_share']:.2f}U 利润")
            elif st in ("canceled", "expired", "rejected"):
                self.db.update_spot_order(row["id"], "cancelled")
                log.warning(f"{NOTE} {row['symbol']}: 挂单已{st}（未成交），"
                            f"水位线不推进，下轮可重新挂")
            else:
                still = True                      # open / partially_filled
        return still or bool(self.db.pending_spot_orders(PURPOSE))

    async def _on_filled(self, row: dict, fill: float, amount: float):
        """一笔限价单成交 → 记账 + 推进水位线。"""
        cost = fill * amount
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_dca_record({"ts": now, "symbol": row["symbol"], "price": fill,
                                   "amount": amount, "cost": cost,
                                   "fee": cost * 0.001,
                                   "dry_run": self.client.dry_run, "note": NOTE})
        st = self.db.get_dca_state(row["symbol"]) or {}
        new_cost = float(st.get("total_cost", 0) or 0) + cost
        new_qty = float(st.get("total_qty", 0) or 0) + amount
        self.db.upsert_dca_state(row["symbol"], total_cost=new_cost, total_qty=new_qty,
                                 avg_price=(new_cost / new_qty if new_qty > 0 else 0),
                                 buy_count=int(st.get("buy_count", 0) or 0) + 1,
                                 last_buy_ts=now)
        self.db.insert_order({"mode": "dca", "symbol": row["symbol"], "side": "buy",
                              "type": "dca_buy", "price": fill, "amount": amount,
                              "cost": round(cost, 6), "fee": round(cost * 0.001, 6),
                              "dry_run": self.client.dry_run, "note": NOTE})
        share = float(row.get("profit_share", 0) or 0)
        if share > 0:
            self._set_anchor(min(self._anchor() + share, self.db.realized_pnl_total()))

    # ---------- 市价买入（offset=0） ----------
    async def _place_market(self, symbols: list[str], invest: float, ratio: float,
                            n_steps: int):
        try:
            await self.client.ensure_spot()
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"{NOTE}: 行情加载失败，本轮跳过: {e}")
            return
        ok, moved = await self.client.ensure_spot_funds(invest)
        if not ok:
            log.warning(f"{NOTE}: 现货可用不足且无法从合约划转（需 {invest:.2f}U），"
                        f"本轮不买；水位线不推进，下轮再试")
            return
        spent, count, detail = await self._allocate(symbols, invest)
        if count <= 0:
            log.warning(f"{NOTE}: 本档买入全部失败，水位线不推进，下轮重试。{detail}")
            return
        # 市价路径按「实际花掉 ÷ 每档预算」推进**整档**（部分成交不推进）
        step_usdt = float(self.cfg.get("step_usdt") or 0)
        per_step = step_usdt * ratio
        steps_done = min(n_steps, int(spent / per_step + 1e-9)) if per_step > 0 else 0
        if steps_done > 0:
            self._set_anchor(min(self._anchor() + steps_done * step_usdt,
                                 self.db.realized_pnl_total()))
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} {NOTE}: 市价买入 {count} 笔，实际成交 {spent:.2f}U"
                    f"（划转 {moved:.2f}U）；水位线 {self._anchor():.2f}U。{detail}")

    async def _allocate(self, symbols: list[str], invest: float) -> tuple[float, int, str]:
        each = invest / max(1, len(symbols))
        spent, ok, fails = 0.0, 0, []
        for sym in symbols:
            try:
                got = await self._buy_one(sym, each)
                if got > 0:
                    spent += got
                    ok += 1
                else:
                    fails.append(sym)
            except Exception as e:
                fails.append(f"{sym}({e})")
        detail = (f"跳过 {len(fails)} 个：{', '.join(fails[:4])}" if fails else "全部成交")
        if fails and ok == 0:
            detail += f"（份额 {each:.2f}U 低于交易所最小下单额，攒够会自动投）"
        return spent, ok, detail

    async def _buy_one(self, sym: str, amount_usdt: float) -> float:
        if amount_usdt <= 0:
            return 0.0
        price = await self.client.ensure_price(sym)
        if price <= 0:
            return 0.0
        qty = self.client.amount_for_notional(sym, amount_usdt, price, "spot")
        if qty <= 0:
            return 0.0
        notional = qty * price
        if notional > amount_usdt * MAX_OVERSPEND:
            log.info(f"{NOTE} {sym}: 最小一手 {notional:.2f}U 远超本次预算 "
                     f"{amount_usdt:.2f}U，跳过（等攒够）")
            return 0.0
        res = await self.client.create_market_order(sym, "buy", qty, "spot")
        if not res:
            return 0.0
        fill = float(res.get("price") or price)
        cost = fill * qty
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_dca_record({"ts": now, "symbol": sym, "price": fill, "amount": qty,
                                   "cost": cost, "fee": cost * 0.001,
                                   "dry_run": self.client.dry_run, "note": NOTE})
        st = self.db.get_dca_state(sym) or {}
        new_cost = float(st.get("total_cost", 0) or 0) + cost
        new_qty = float(st.get("total_qty", 0) or 0) + qty
        self.db.upsert_dca_state(sym, total_cost=new_cost, total_qty=new_qty,
                                 avg_price=(new_cost / new_qty if new_qty > 0 else 0),
                                 buy_count=int(st.get("buy_count", 0) or 0) + 1,
                                 last_buy_ts=now)
        self.db.insert_order({"mode": "dca", "symbol": sym, "side": "buy", "type": "dca_buy",
                              "price": fill, "amount": qty, "cost": round(cost, 6),
                              "fee": round(cost * 0.001, 6),
                              "dry_run": self.client.dry_run, "note": NOTE})
        return cost

    # ---------- 展示 ----------
    def status(self) -> dict:
        cfg = self.cfg
        step = float(cfg.get("step_usdt") or 0)
        ratio = float(cfg.get("ratio") or 0)
        realized = self.db.realized_pnl_total()
        anchor = self._anchor()
        n, per_step, invest = self.pending_steps()
        pend = self.db.pending_spot_orders(PURPOSE)
        # 最小下单额跟随交易所规则（各币对不同，取所配标的里最高的做保守提示）
        try:
            floors = [self.client.min_notional_of(s, "spot")
                      for s in (cfg.get("symbols") or []) if s]
        except Exception:
            floors = []
        return {
            "enabled": bool(cfg.get("enabled")),
            "realized_total": round(realized, 4),
            "anchor": round(anchor, 4),
            "step_usdt": step,
            "ratio": ratio,
            "per_step_usdt": round(step * ratio, 4),
            "pending_steps": n,
            "pending_usdt": round(invest, 4),
            "to_next_step": round(max(0.0, anchor + step - realized), 4),
            "invested_total": round(self.db.dca_invested_total(NOTE), 4),
            "max_total": float(cfg.get("max_total") or 0),
            "symbols": list(cfg.get("symbols") or []),
            "per_symbol_usdt": round(per_step / max(1, len(cfg.get("symbols") or [])), 4),
            "min_notional": max(floors) if floors else 5.0,
            "entry_offset": self._num("entry_offset"),
            "pending_orders": [
                {"symbol": p["symbol"], "amount": p["amount"], "price": p["price"],
                 "cost_usdt": p.get("cost_usdt", 0), "order_id": p["order_id"]}
                for p in pend
            ],
            "holdings": self._holdings(),
        }

    def _holdings(self) -> list[dict]:
        """利润定投的现货持仓 —— 只买不卖，持仓 = 成交买入的累计。

        从 spot_orders(purpose=profit_dca) 推导（单一真源），与现货交易
        （马丁格尔）的持仓互不混账。现价用行情缓存的最新值。
        """
        out = []
        syms = {r["symbol"] for r in self.db.filled_spot_orders(PURPOSE)}
        for sym in sorted(syms):
            fills = [r for r in self.db.filled_spot_orders(PURPOSE, sym)
                     if r["side"] == "buy"]
            qty = sum(float(r["amount"]) for r in fills)
            cost = sum(float(r["cost_usdt"]) for r in fills)
            if qty <= 1e-12:
                continue
            price = self.client.last_price(sym) or 0
            value = qty * price
            out.append({
                "symbol": sym,
                "qty": qty,
                "cost": round(cost, 4),
                "avg_price": round(cost / qty, 8),
                "price": price,
                "value": round(value, 4),
                "pnl": round(value - cost, 4),
                "pnl_rate": round((value - cost) / cost, 6) if cost > 0 else 0,
                "buy_count": len(fills),
            })
        return out
