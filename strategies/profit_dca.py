"""利润定投 —— 用合约赚到的钱买现货。

用户定的规则：
  · **每档**合约累计已实现盈利每上涨 `step_usdt`，就算一个台阶；
  · 每个台阶拿出**该台阶利润**的 `ratio%` 买入现货；
  · **不能循环定投**——不是「每轮循环投一次」，而是「盈利真的涨上去了才投」。

「不循环」靠一条**只升不降的水位线**（runtime_stat `profit_dca_anchor`）实现：

    盈利  0 ─────► 5 ─────► 9 ─────► 3 ─────► 7 ─────► 11
    水位  0        0         5        5         5        10
    触发  —        ✅ 投1.5U  —        ❌ 不投   ❌ 不投  ✅ 投1.5U

  第 3 次盈利从 9 掉到 3 再涨到 7：**不触发**（水位线没降，必须有新利润）。
  涨到 11 才越过 5→10 的新台阶 → 再投一次。

这样既不会把本金投进去（只投利润的一部分），也不会因为盈利上下波动而反复买。

支持一次补多档：机器人离线期间盈利从 0 涨到 12，水位线 0 → 一次投 2 档
（不是漏掉，也不是只投 1 档）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ANCHOR_KEY = "profit_dca_anchor"
NOTE = "利润定投"

# 单笔允许超出预算的上限。低于它才买：交易所的「最小下单额」会把小额预算抬高
# （币安现货 ≈5U），不对齐步长又会溢出一点，所以留 20% 余量。
# 调小 → 更容易跳过、更省；调大 → 更容易投出去、但单笔可能超预算。
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

    def _anchor(self) -> float:
        return float(self.db.get_runtime_stat(ANCHOR_KEY, 0) or 0)

    def _set_anchor(self, v: float):
        self.db.set_runtime_stat(ANCHOR_KEY, float(v))

    def pending_steps(self) -> tuple[int, float, float]:
        """返回 (可执行档数, 每档投入 U, 本次应投 U)。**无任何副作用**。

        档数 = (累计盈利 − 水位线) 里有几个完整台阶。离线期间涨了 3 个台阶
        就一次补 3 档，不漏也不多。
        """
        cfg = self.cfg
        if not cfg.get("enabled"):
            return 0, 0.0, 0.0
        step = float(cfg.get("step_usdt") or 0)
        ratio = float(cfg.get("ratio") or 0)
        if step <= 0 or ratio <= 0:
            return 0, 0.0, 0.0
        per = step * ratio                             # 每档要投的钱
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

    async def step(self):
        """每个 tick 调一次；没有可投的就立刻返回（零开销）。"""
        cfg = self.cfg
        if not cfg.get("enabled"):
            return
        symbols = [s for s in (cfg.get("symbols") or []) if s]
        n, per_step, invest = self.pending_steps()
        if n <= 0 or invest <= 0:
            return
        if not symbols:
            log.warning(f"{NOTE}: 已触发 {n} 档（应投 {invest:.2f}U）但没有勾选任何标的，"
                        f"先在「利润定投 · 定投标的」里选几个")
            return

        try:
            await self.client.ensure_spot()
            await self.client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"{NOTE}: 行情加载失败，本轮跳过: {e}")
            return

        if not self.client.dry_run:
            try:
                spot = await self.client.get_spot_balance("USDT")
                if float(spot.get("free", 0) or 0) < invest:
                    log.warning(f"{NOTE}: 现货可用 {spot.get('free', 0):.2f}U < 本次需 "
                                f"{invest:.2f}U，跳过（水位线不推进，下轮再试）")
                    return
            except Exception as e:
                log.warning(f"{NOTE}: 余额查询失败，本轮跳过: {e}")
                return

        spent, ok, detail = await self._allocate(symbols, invest)
        if ok <= 0:
            log.warning(f"{NOTE}: 本档买入全部失败，水位线不推进，下轮重试。{detail}")
            return

        # **只有真的买成了才推进水位线**，而且**按整档推进**。
        #
        # 不能写成「水位线 += 实际花掉的钱 ÷ 比例」：实际支出会因为「最小一手」
        # 而超出该档预算（例如预算 1U、最小一手 5.1U），那样算出来是 5.1/ratio，
        # 一步就吞掉好几档、把本该留给未来的定投永久吃掉；反过来 ratio 很小时
        # 又会漂移。用户定的规则是「每上涨一档利润、投这一档的一部分」，
        # 所以推进的单位只能是**档**：投出去的钱够几档，就推进几档。
        ratio = float(cfg.get("ratio") or 0)
        steps_done = min(n, int(spent / per_step + 1e-9)) if per_step > 0 else 0
        per_step_profit = (per_step / ratio) if ratio > 0 else 0.0   # 一档对应多少利润
        new_anchor = min(self._anchor() + steps_done * per_step_profit,
                         self.db.realized_pnl_total())
        self._set_anchor(new_anchor)
        tag = "[模拟]" if self.client.dry_run else "[实盘]"
        log.warning(f"{tag} {NOTE}: 触发 {n} 档（累计盈利已越过 "
                    f"{n * per_step:.2f}U 的台阶），计划买 {invest:.2f}U，实际成交 "
                    f"{spent:.2f}U；水位线 {self._anchor():.2f}U。{detail}")

    async def _allocate(self, symbols: list[str], invest: float) -> tuple[float, int, str]:
        """把 invest 平均分给各标的买入。返回 (实际花掉, 成功笔数, 说明)。

        **按份额买得起的才买**：交易所最小下单额（币安现货 ≈5U）会让「一手」的
        实际金额远超分到的那一份。若不拦住，1.5U 的预算会被 BTC 的 5.77U 最小单
        整笔吃掉（实测 1.5U 投出去 16.13U）。买不起的就跳过，钱留着 ——
        水位线只按实际花掉的部分推进，于是**利润会自然攒到够买为止**再投。
        """
        each = invest / len(symbols)
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
            log.warning(f"{NOTE} {sym}: 拿不到价格，跳过")
            return 0.0
        qty = self.client.amount_for_notional(sym, amount_usdt, price, "spot")
        if qty <= 0:
            return 0.0
        notional = qty * price
        if notional > amount_usdt * MAX_OVERSPEND:
            # 最小一手就比预算大太多 → 宁可这一轮不投，也不要超支
            log.info(f"{NOTE} {sym}: 最小一手 {notional:.2f}U 远超本次预算 "
                     f"{amount_usdt:.2f}U，跳过（等攒够）")
            return 0.0
        res = await self.client.create_market_order(sym, "buy", qty, "spot")
        if not res:
            return 0.0
        fill = float(res.get("price") or price)
        cost = fill * qty
        if cost > amount_usdt * MAX_OVERSPEND:
            # 市价单成交价可能高于下单前盯的价，事后复核一遍留痕（单已成交，
            # 撤不回来了，但日志要能解释「为什么这档花了这么多」）
            log.warning(f"{NOTE} {sym}: 实际成交 {cost:.2f}U 超出预算 "
                        f"{amount_usdt:.2f}U 的 {MAX_OVERSPEND:.0%}（滑点/最小一手）")
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

    def status(self) -> dict:
        """给前端：水位线、已投多少、下一档还差多少。"""
        cfg = self.cfg
        step = float(cfg.get("step_usdt") or 0)
        ratio = float(cfg.get("ratio") or 0)
        realized = self.db.realized_pnl_total()
        anchor = self._anchor()
        n, per_step, invest = self.pending_steps()
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
            "max_total": float(cfg.get("max_total", 0) or 0),
            "symbols": list(cfg.get("symbols") or []),
            "per_symbol_usdt": round(per_step / max(1, len(cfg.get("symbols") or [])), 4),
            "min_notional": 5.0,
        }
