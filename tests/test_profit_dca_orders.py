"""利润定投的「挂单 + 资金划转」链路（用户明确要求）。

三条规则：
  1. 钱在合约账户 → 下单前必须划转到现货，只划缺口
  2. 支持挂单：按「现价 ×(1−折扣)」挂限价买单
  3. 只有**成交后**才推进盈利水位线（挂单在途时不重复下单）
"""
from __future__ import annotations

import asyncio
import os
import unittest

from strategies.profit_dca import NOTE, PURPOSE, ProfitDCA
from tests.harness import FakeClient, temp_db


def cfg(entry_offset=0.02, symbols=("DOGEUSDT",), step=20.0, ratio=0.3):
    return {"profit_dca": {"enabled": True, "step_usdt": step, "ratio": ratio,
                           "max_total": 0, "symbols": list(symbols),
                           "entry_offset": entry_offset}}


def run(c):
    return asyncio.run(c)


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()
        self.cl = FakeClient(price=100.0, spot_free=1000.0)
        self.db.insert_order({"mode": "grid", "symbol": "F", "side": "sell",
                              "type": "grid_sell", "price": 1, "amount": 1, "pnl": 24.0})

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)


class TestTransfer(Base):
    def test_transfers_only_the_shortfall(self):
        """现货已有 5U、需 6U → 只划 1U；绝不多划。"""
        self.cl.spot_free = 5.0
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        self.assertEqual(self.cl.transfers, [1.0], f"应只划缺口 1U，实际 {self.cl.transfers}")

    def test_no_transfer_when_spot_already_has_funds(self):
        self.cl.spot_free = 1000.0
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        self.assertEqual(self.cl.transfers, [], "现货钱够就不该动合约账户")

    def test_no_order_when_transfer_impossible(self):
        """合约也没钱 → 不挂单、水位线不动。"""
        self.cl.spot_free = 0.0
        self.cl.ensure_spot_funds = lambda needed: _false()
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        self.assertEqual(self.cl.limit_orders, [])
        self.assertEqual(p._anchor(), 0.0)


class TestLimitOrders(Base):
    def test_places_limit_below_current_price(self):
        """折扣 2% → 挂单价 = 现价 × 0.98。"""
        p = ProfitDCA(self.cl, self.db, cfg(entry_offset=0.02))
        run(p.step())
        self.assertEqual(len(self.cl.limit_orders), 1)
        sym, side, amt, px, kind, oid = self.cl.limit_orders[0]
        self.assertEqual(side, "buy")
        self.assertAlmostEqual(px, 98.0, places=8, msg="应挂在现价下方 2%")
        self.assertEqual(kind, "spot")
        pend = self.db.pending_spot_orders(PURPOSE)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["order_id"], oid)

    def test_does_not_place_again_while_pending(self):
        """挂单在途时不能每个 tick 都再挂一张。"""
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        run(p.step())
        run(p.step())
        self.assertEqual(len(self.cl.limit_orders), 1, "在途挂单期间重复下单了")

    def test_anchor_does_not_advance_until_filled(self):
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        self.assertEqual(p._anchor(), 0.0, "没成交就推进水位线 = 白吞一次定投")
        # 模拟成交
        pend = self.db.pending_spot_orders(PURPOSE)[0]
        self.cl.spot_orders_status[pend["order_id"]] = {
            "status": "closed", "average": 98.0, "filled": pend["amount"]}
        run(p.step())
        self.assertAlmostEqual(p._anchor(), 20.0, places=6)
        self.assertEqual(self.db.pending_spot_orders(PURPOSE), [], "成交后应清掉在途记录")
        self.assertEqual(len([r for r in self.db.get_dca_records(10)
                              if r["note"] == NOTE]), 1)

    def test_cancelled_order_does_not_advance_anchor(self):
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        pend = self.db.pending_spot_orders(PURPOSE)[0]
        self.cl.spot_orders_status[pend["order_id"]] = {"status": "canceled"}
        run(p.step())
        self.assertEqual(p._anchor(), 0.0)
        # 撤销的那张被清掉，同一 tick 立刻按同样条件重挂一张（新记录）
        pend = self.db.pending_spot_orders(PURPOSE)
        self.assertEqual(len(pend), 1)
        self.assertNotEqual(pend[0]["order_id"], self.cl.limit_orders[0][5],
                            "应该是新挂的单，不是那条已撤销的")
        self.assertEqual(len(self.cl.limit_orders), 2)

    def test_fill_uses_actual_fill_price(self):
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        pend = self.db.pending_spot_orders(PURPOSE)[0]
        self.cl.spot_orders_status[pend["order_id"]] = {
            "status": "closed", "average": 97.5, "filled": pend["amount"]}
        run(p.step())
        rec = [r for r in self.db.get_dca_records(10) if r["note"] == NOTE][0]
        self.assertAlmostEqual(rec["price"], 97.5, places=8,
                               msg="应按真实成交价记账，不是挂单价")

    def test_market_path_when_offset_zero(self):
        p = ProfitDCA(self.cl, self.db, cfg(entry_offset=0.0))
        run(p.step())
        self.assertEqual(self.cl.limit_orders, [])
        self.assertEqual(len(self.cl.market_orders), 1)


class TestStatusShowsPending(Base):
    def test_status_exposes_pending_order(self):
        p = ProfitDCA(self.cl, self.db, cfg())
        run(p.step())
        s = p.status()
        self.assertEqual(len(s["pending_orders"]), 1)
        self.assertAlmostEqual(s["pending_orders"][0]["price"], 98.0, places=6)
        self.assertAlmostEqual(s["entry_offset"], 0.02, places=9)


class TestEngineConcurrency(unittest.TestCase):
    """利润定投 / 现货交易 / 网格 三者可以同时开启，互不冲突。"""

    def strategies(self, cfg):
        from engine import Engine
        from tests.harness import FakeClient, temp_db
        db, path = temp_db()
        try:
            db.save_config(cfg)
            eng = Engine(FakeClient(), db)
            return [type(s).__name__ for s in eng._strategies()]
        finally:
            db.close()
            os.remove(path)

    def test_grid_plus_spot_plus_profit_all_run(self):
        got = self.strategies({
            "mode": "grid", "dry_run": True,
            "dca": {"enabled": True, "symbols": ["BTCUSDT"], "amount_usdt": 10},
            "profit_dca": {"enabled": True, "step_usdt": 20, "ratio": 0.3,
                           "symbols": ["BTCUSDT"], "entry_offset": 0.02},
            "grid": {"size_usdt": 5},
        })
        self.assertIn("GridStrategy", got)
        self.assertIn("DCAStrategy", got)
        self.assertIn("ProfitDCA", got, "利润定投没有和网格同时运行")

    def test_spot_runs_in_grid_mode_when_enabled(self):
        got = self.strategies({"mode": "grid", "dca": {"enabled": True},
                               "profit_dca": {"enabled": False}, "grid": {}})
        self.assertEqual(got, ["GridStrategy", "DCAStrategy"])

    def test_profit_dca_runs_in_dca_mode(self):
        got = self.strategies({"mode": "dca", "dca": {"enabled": False},
                               "profit_dca": {"enabled": True, "symbols": ["BTCUSDT"]}})
        self.assertIn("DCAStrategy", got)
        self.assertIn("ProfitDCA", got)

    def test_off_runs_nothing(self):
        got = self.strategies({"mode": "off", "dca": {"enabled": True},
                               "profit_dca": {"enabled": True}})
        self.assertEqual(got, [])


async def _false():
    return False, 0.0


if __name__ == "__main__":
    unittest.main()
