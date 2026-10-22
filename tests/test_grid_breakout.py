"""价格跌破网格下轨后的向下补仓（用户批准的规则）。

背景：网格档位只存在于 [lower, upper]。价格跌破下轨后，所有买档都已成交，
区间下方没有买档；「亏损时不重排网格」又挡住了动态跟随 —— 结果是
ZEREBRO 跌到 -20%、24h 区间 2% 位置时一手不补（用户实测提问）。

规则：
  · 只对已持仓、现价低于持仓均价的标的启用
  · 每再跌破下轨 breakout_step（默认 3%）补一档，最多 breakout_max（默认 3）档
  · 每档金额 = 单格金额，不做马丁式加倍
  · 补仓成交后卖价不得低于当前均价×(1+min_tp) —— 绝不亏损卖出
"""
from __future__ import annotations

import asyncio
import os
import unittest

from tests.harness import FakeClient, grid_cfg, make_grid, temp_db

SYM = "X"
LOWER = 0.0360825
UPPER = 0.0488175
ENTRY = 0.038578
QTY = 531.0
COST = ENTRY * QTY


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()
        self.cl = FakeClient(price=0.030757)
        self.cl._positions = [{"symbol": SYM, "side": "long", "contracts": QTY,
                               "entryPrice": ENTRY}]
        self.db.upsert_grid_state(SYM, lower=LOWER, upper=UPPER, grids=8,
                                  size_usdt=5.0, base_qty=QTY, total_cost=COST)
        self.db.add_lot(SYM, ENTRY, QTY, "测试持仓")

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def make(self, **over):
        return make_grid(self.cl, self.db,
                         grid_cfg(auto_select=False, **over), selection=[SYM])

    def ext_levels(self):
        return [l for l in self.db.get_grid_levels(SYM)
                if l["side"] == "buy" and float(l["price"]) < LOWER * (1 - 1e-9)]


class TestExtendDown(Base):
    def test_adds_three_levels_when_price_is_far_below(self):
        """现价 0.030757 已跌破下轨 14.8% → 3 档全部补出（上限 3）。"""
        g = self.make(breakout_extend=True, breakout_step=0.03, breakout_max=3)
        added = run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertTrue(added)
        exts = self.ext_levels()
        self.assertEqual(len(exts), 3)
        got = sorted(float(l["price"]) for l in exts)
        want = sorted(LOWER * (0.97 ** k) for k in (1, 2, 3))
        for a, b in zip(got, want):
            self.assertAlmostEqual(a, b, places=7)   # 挂单价会对齐到交易所精度
        # 每档金额 = 单格金额（不做马丁式加倍）
        for l in exts:
            self.assertAlmostEqual(float(l["price"]) * float(l["amount"]), 5.0, places=6)

    def test_is_idempotent(self):
        g = self.make(breakout_max=3)
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertEqual(len(self.ext_levels()), 3, "重复调用不应重复补档")

    def test_respects_max_levels(self):
        g = self.make(breakout_max=2)
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertEqual(len(self.ext_levels()), 2)

    def test_only_extends_as_price_falls(self):
        """跌 3% 补一档；再跌 3% 补第二档。"""
        g = self.make(breakout_max=3, breakout_step=0.03)
        run(g._maybe_extend_down(SYM, LOWER * 0.965, 5.0, False))
        self.assertEqual(len(self.ext_levels()), 1)
        run(g._maybe_extend_down(SYM, LOWER * 0.935, 5.0, False))
        self.assertEqual(len(self.ext_levels()), 2)

    def test_no_extension_above_lower(self):
        g = self.make()
        run(g._maybe_extend_down(SYM, LOWER * 1.01, 5.0, False))
        self.assertEqual(self.ext_levels(), [], "现价还在区间内不该补")

    def test_disabled_switch(self):
        g = self.make(breakout_extend=False)
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertEqual(self.ext_levels(), [])

    def test_no_extension_when_flat(self):
        self.db.clear_lots(SYM)
        self.db.upsert_grid_state(SYM, base_qty=0, total_cost=0)
        self.cl._positions = []
        g = self.make()
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertEqual(self.ext_levels(), [])

    def test_no_extension_while_draining(self):
        g = self.make()
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, True))
        self.assertEqual(self.ext_levels(), [])

    def test_no_extension_when_price_above_entry(self):
        """价格虽然跌破下轨，但仍在成本之上 → 交给正常重排，不走补仓。"""
        self.db.clear_lots(SYM)
        self.db.add_lot(SYM, 0.0300, QTY, "低成本持仓")
        self.cl._positions = [{"symbol": SYM, "side": "long", "contracts": QTY,
                               "entryPrice": 0.0300}]
        g = self.make()
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        self.assertEqual(self.ext_levels(), [])


class TestActualFillPrice(Base):
    def test_marketable_extension_buy_records_actual_fill_price(self):
        """补仓单挂在市价之上会立即成交 → 必须按交易所实际均价记账，不是挂单价。"""
        g = self.make()
        lid = self.db.insert_grid_level(SYM, 1001, 0.035, "buy", 234)
        self.db.conn.execute("UPDATE grid_levels SET order_id='O1' WHERE id=?", (lid,))
        self.db.conn.commit()
        self.cl.fill_prices["O1"] = 0.0308       # 实际以更优价格成交
        run(g._check_fills(SYM, 0.0308, False))
        recs = [o for o in self.db.get_orders(10) if o["symbol"] == SYM]
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(float(recs[0]["price"]), 0.0308, places=9,
                               msg="应按实际成交均价记账，而不是挂单价 0.035")


class TestLevelRotation(Base):
    def test_levels_of_includes_extension_levels(self):
        g = self.make()
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        pts = g._levels_of(SYM)
        for l in self.ext_levels():
            self.assertIn(round(float(l["price"]), 9),
                          [round(p, 9) for p in pts],
                          "扩展档必须参与后续轮转，否则补的仓卖完就丢掉位置")


class TestNeverSellAtLoss(Base):
    def test_sell_after_extension_fill_is_above_average_cost(self):
        """补仓成交后生成的卖单，价格必须 ≥ 新均价×(1+min_tp)。"""
        g = self.make(breakout_step=0.03, breakout_max=3)
        run(g._maybe_extend_down(SYM, 0.030757, 5.0, False))
        ext = sorted(self.ext_levels(), key=lambda l: float(l["price"]))[-1]
        fill = 0.030757          # 限价挂在市价之上，按当前市价成交
        # 模拟这笔扩展买单成交（只记账本，不重复走交易所流程）
        self.db.fill_grid_level(ext["id"])
        self.db.add_lot(SYM, fill, float(ext["amount"]), "向下补仓")
        qty, cost = self.db.lots_qty_cost(SYM)
        min_tp = 0.002
        floor = (cost / qty) * (1 + min_tp)
        run(g._arm_next(SYM, "buy", fill, False))
        sells = [l for l in self.db.get_grid_levels(SYM)
                 if l["side"] == "sell" and l["status"] == "pending"]
        new = [l for l in sells if abs(float(l["price"]) - floor) < 1e-6]
        self.assertTrue(new, f"应挂出卖价 ≥ 均价×(1+{min_tp}) = {floor:.6f}，"
                             f"实际卖单 {[l['price'] for l in sells]}")

    def test_normal_grid_fill_keeps_original_next_level(self):
        """区间内的正常买入成交不受影响：仍按下一档网格价挂卖。"""
        g = self.make(min_tp_ratio=0.002)
        run(g._arm_next(SYM, "buy", LOWER, False))
        sells = [l for l in self.db.get_grid_levels(SYM)
                 if l["side"] == "sell" and l["status"] == "pending"]
        self.assertTrue(sells)
        # 下一档在 LOWER 之上（原网格几何档距）
        self.assertGreater(float(sells[0]["price"]), LOWER)


if __name__ == "__main__":
    unittest.main()
