"""用**真实的数量换算代码**测试超支护栏（审核指出的假安全感问题）。

之前 tests/harness.py 的 FakeClient.amount_for_notional 用 `max(u, min_notional)/p`
近似建模，绕过了真实的「先向下取整到步长、再抬到最小一手」路径 ——
而那条路径正是「买 12U 的 BTC 变成买 1 整枚（8 万 U）」事故的发生地。

这里不用近似：直接构造真实 ExchangeClient（跳过 __init__，不连网），
注入与币安一致的精度/限额表，让 min_amount → _round_amount →
amount_for_notional 整条真实代码链路跑起来。
"""
from __future__ import annotations

import asyncio
import math
import os
import unittest

from exchange_client import ExchangeClient
from strategies.profit_dca import MAX_OVERSPEND, NOTE, ProfitDCA
from tests.harness import temp_db


class _CcxtStub:
    """只复刻 ccxt 的 amount_to_precision（币安对数量用 TRUNCATE = 向下截断到步长）。

    我们自己的 ceil/floor 逻辑在 _round_amount 里，这个桩只负责最后那一步
    「按步长截断」，与 ccxt 行为一致。
    """

    def __init__(self, steps: dict[str, float]):
        self._steps = steps

    def amount_to_precision(self, ccxt_sym: str, amt: float) -> str:
        step = self._steps.get(ccxt_sym, 1e-8)
        return f"{math.floor(round(amt / step, 9)) * step:.8f}".rstrip("0").rstrip(".")


# 与币安一致的两种典型市场（真实参数来自 2026-10 抽样）：
BTC_MARKET = {  # 高价币：数量步长 1e-5，最小数量 1e-5，最小名义 5U
    "precision": {"amount": 1e-05}, "contractSize": None,
    "limits": {"amount": {"min": 1e-05, "max": 9000.0},
               "cost": {"min": 5.0, "max": 9000000.0}},
}
DOGE_MARKET = {  # 低价币：数量步长 1（整数枚），最小名义 1U
    "precision": {"amount": 1.0}, "contractSize": None,
    "limits": {"amount": {"min": 1.0, "max": 9000000.0},
               "cost": {"min": 1.0, "max": 9000000.0}},
}


def real_client() -> ExchangeClient:
    """构造真实 ExchangeClient（绕过 __init__，不建 ccxt 连接）。"""
    cl = ExchangeClient.__new__(ExchangeClient)
    cl.config = {}
    cl.dry_run = True
    cl.min_order_notional = 5.0
    cl.notional_safety = 1.02
    cl._fut_markets = {}
    cl._spot_markets = {"BTC/USDT": BTC_MARKET, "DOGE/USDT": DOGE_MARKET}
    cl._spot = _CcxtStub({"BTC/USDT": 1e-05, "DOGE/USDT": 1.0})
    return cl


class TestRealAmountMath(unittest.TestCase):
    """真实链路：min_amount → _round_amount → amount_for_notional。"""

    def setUp(self):
        self.cl = real_client()

    def test_btc_12u_buys_about_12u_not_one_whole_coin(self):
        """事故复现用例：买 12U 的 BTC 绝不能变成 1 整枚（8 万 U）。"""
        qty = self.cl.amount_for_notional("BTCUSDT", 12.0, 82000.0, "spot")
        notional = qty * 82000.0
        self.assertGreater(notional, 5.0, "低于最小下单额会被交易所拒单")
        self.assertLessEqual(notional, 12.0 * 1.2,
                             f"买 12U 却算了 {notional:.2f}U 的量")

    def test_btc_tiny_budget_raises_to_min_lot(self):
        """1U 预算买 BTC：向下取整会低于最小额 → 抬到最小一手 ≈5.1U。"""
        qty = self.cl.amount_for_notional("BTCUSDT", 1.0, 82000.0, "spot")
        notional = qty * 82000.0
        self.assertGreaterEqual(notional, 5.0, "必须够得着最小下单额")
        # 抬升是「向上取整到步长」，只会略超，不可能翻倍
        self.assertLess(notional, 8.0, f"最小一手 {notional:.2f}U 太离谱")

    def test_doge_integer_step(self):
        """步长=1 的币（DOGE）：数量必须是整数枚，且金额贴近预算。"""
        qty = self.cl.amount_for_notional("DOGEUSDT", 6.0, 0.08, "spot")
        self.assertAlmostEqual(qty, round(qty), places=9, msg="步长 1 的币必须整数枚")
        self.assertLessEqual(qty * 0.08, 6.0 * 1.2)

    def test_round_amount_no_ceil_spurious_step(self):
        """恰好整数倍时不要被浮点误差多抬一档（ceil 的 epsilon）。"""
        raw = 2 * 1e-05                      # 恰好 2 个步长
        got = self.cl._round_amount("BTCUSDT", raw, "spot", up=True)
        self.assertAlmostEqual(got, 2e-05, places=12,
                               msg="恰好 2 个步长被 ceil 成了 3 个")

    def test_futures_still_uses_whole_contract(self):
        """合约回归：张数必须整数（现货改小数时别把合约改坏了）。"""
        cl = self.cl
        cl._fut_markets = {"X/USDT:USDT": {
            "precision": {"amount": 1.0}, "contractSize": 1,
            "limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}}}}
        cl._spot = None
        cl.futures = _CcxtStub({"X/USDT:USDT": 1.0})
        amt = cl.min_amount("XUSDT", 2.0, "futures")
        self.assertAlmostEqual(amt, 3.0, places=9,
                               msg="5.1U/2.0 = 2.55 张，应 ceil 成整 3 张")


class TestRealPathOverspend(unittest.TestCase):
    """把真实数量代码接进 ProfitDCA._buy_one，验证资金护栏。"""

    def setUp(self):
        self.db, self.path = temp_db()
        self.cl = real_client()
        # 网络方法换成内存实现，其余全走真实代码
        async def _noop(*a, **k):
            return None
        self.cl.ensure_spot = _noop
        self.cl.refresh_prices = _noop

        async def _bal(asset="USDT"):
            return {"free": 1e6, "total": 1e6}
        self.cl.get_spot_balance = _bal

        async def _price(sym, max_age=30.0):
            return 82000.0 if sym.startswith("BTC") else 0.08
        self.cl.ensure_price = _price
        self.orders = []

        async def _order(sym, side, amount, kind="futures", position_side="LONG"):
            self.orders.append((sym, side, amount, kind))
            return {"order_id": "M", "price": 82000.0 if sym.startswith("BTC") else 0.08}
        self.cl.create_market_order = _order

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _earn(self, u):
        self.db.insert_order({"mode": "grid", "symbol": "F", "side": "sell",
                              "type": "grid_sell", "price": 1, "amount": 1, "pnl": u})

    def test_budget_below_min_lot_places_no_order(self):
        """6U 分给 2 个标的 → 每份 3U < 最小一手 → 不该有任何真实下单。"""
        self._earn(20.0)
        p = ProfitDCA(self.cl, self.db, {
            "profit_dca": {"enabled": True, "step_usdt": 20, "ratio": 0.3,
                           "max_total": 0, "symbols": ["BTCUSDT", "BTCUSDT2"],
                           "entry_offset": 0.0}})
        asyncio.run(p.step())
        # BTCUSDT2 不在市场表里 → 拿不到价；BTCUSDT 每份 3U < 5.1U 最小一手 → 跳过
        self.assertEqual(self.orders, [], "预算不足最小一手时下了真实订单")

    def test_single_symbol_buys_within_tolerance(self):
        self._earn(20.0)                      # 1 档 = 6U，单个标的买得起
        p = ProfitDCA(self.cl, self.db, {
            "profit_dca": {"enabled": True, "step_usdt": 20, "ratio": 0.3,
                           "max_total": 0, "symbols": ["BTCUSDT"],
                           "entry_offset": 0.0}})
        asyncio.run(p.step())
        self.assertEqual(len(self.orders), 1)
        recs = [r for r in self.db.get_dca_records(10) if r["note"] == NOTE]
        self.assertEqual(len(recs), 1)
        self.assertLessEqual(recs[0]["cost"], 6.0 * MAX_OVERSPEND,
                             f"预算 6U 实际花了 {recs[0]['cost']:.2f}U")


async def _d(v):
    return v


if __name__ == "__main__":
    unittest.main()
