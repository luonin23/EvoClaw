"""强平记录：监控同步、统计、成交记录、网格展示规则。

数据源是币安 forceOrders（逐仓/全仓被强平的每一笔平仓单），
order_id 唯一约束保证重复同步幂等。
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

from engine import Engine
from tests.harness import FakeClient, grid_cfg, make_grid, temp_db


def run(coro):
    return asyncio.run(coro)


def force_order(oid="F1", symbol="XUSDT", ts=None, side="sell", ps="LONG",
                price=0.9, qty=100):
    return {
        "order_id": oid,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "symbol": symbol, "side": side, "position_side": ps,
        "price": price, "amount": qty, "notional": price * qty,
        "margin_mode": "", "raw": "",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()
        self.cl = FakeClient(price=1.0)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)


class TestStore(Base):
    def test_insert_is_idempotent(self):
        r = force_order()
        self.assertTrue(self.db.insert_liquidation(r))
        self.assertFalse(self.db.insert_liquidation(r), "同一 order_id 不能重复计数")
        self.assertEqual(self.db.liquidation_stats()["count"], 1)

    def test_stats(self):
        self.db.insert_liquidation(force_order("F1", "XUSDT", price=1.0, qty=100))
        self.db.insert_liquidation(force_order("F2", "YUSDT", price=2.0, qty=50))
        s = self.db.liquidation_stats()
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["symbols"], 2)
        self.assertAlmostEqual(s["total_notional"], 200.0, places=6)

    def test_liquidation_near_matches_symbol_and_time(self):
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_liquidation(force_order("F1", "XUSDT", ts=now))
        self.assertIsNotNone(self.db.liquidation_near("XUSDT", now, 600))
        self.assertIsNone(self.db.liquidation_near("YUSDT", now, 600))

    def test_record_order_deduplicates_within_window(self):
        now = datetime.now(timezone.utc).isoformat()
        rec = {"ts": now, "mode": "grid", "symbol": "XUSDT", "side": "sell",
               "price": 0.9, "amount": 100, "cost": 90, "fee": 0.045,
               "pnl": -9.1, "pnl_rate": -0.09, "dry_run": False,
               "note": "强制平仓", "order_id": "F1"}
        self.assertTrue(self.db.record_liquidation_order(rec))
        self.assertFalse(self.db.record_liquidation_order({**rec, "amount": 99}),
                         "时间窗内不应重复插入")
        rows = [o for o in self.db.get_orders(10) if o["type"] == "liquidation"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 100, "默认不覆盖对账数据")

    def test_record_order_overwrite_uses_local_accounting(self):
        now = datetime.now(timezone.utc).isoformat()
        base = {"ts": now, "mode": "grid", "symbol": "XUSDT", "side": "sell",
                "price": 0.9, "amount": 100, "cost": 90, "fee": 0,
                "pnl": 0, "pnl_rate": 0, "dry_run": False,
                "note": "强制平仓", "order_id": "F1"}
        self.db.record_liquidation_order(base)
        self.db.record_liquidation_order({**base, "price": 0.88, "pnl": -12.5,
                                          "pnl_rate": -0.125}, overwrite=True)
        rows = [o for o in self.db.get_orders(10) if o["type"] == "liquidation"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["price"]), 0.88, places=9)
        self.assertAlmostEqual(float(rows[0]["pnl"]), -12.5, places=9)


class TestEngineSync(Base):
    def test_sync_inserts_liquidations_and_orders(self):
        self.cl.force_orders = [force_order("F1"), force_order("F2", "YUSDT")]
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())
        self.assertEqual(self.db.liquidation_stats()["count"], 2)
        rows = [o for o in self.db.get_orders(10) if o["type"] == "liquidation"]
        self.assertEqual(len(rows), 2, "强平也要出现在成交记录里")

    def test_sync_is_idempotent(self):
        self.cl.force_orders = [force_order("F1")]
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())
        self.db.set_runtime_stat("liq_sync_ts", 0)      # 强制下一轮再同步
        run(eng._sync_liquidations())
        self.assertEqual(self.db.liquidation_stats()["count"], 1)
        rows = [o for o in self.db.get_orders(10) if o["type"] == "liquidation"]
        self.assertEqual(len(rows), 1)

    def test_sync_throttled(self):
        self.cl.force_orders = [force_order("F1")]
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())
        self.cl.force_orders = [force_order("F2", "YUSDT")]
        run(eng._sync_liquidations())                   # 5 分钟内不再拉
        self.assertEqual(self.db.liquidation_stats()["count"], 1)

    def test_sync_enriches_realized_pnl_from_income(self):
        """forceOrders 没有盈亏 → 用同秒收入流水还原（REALIZED_PNL + COMMISSION）。"""
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.cl.force_orders = [force_order("F1", "XUSDT", ts=ts, price=1.0, qty=100)]
        self.cl.income_records = [
            {"symbol": "XUSDT", "incomeType": "REALIZED_PNL", "income": "-10.5"},
            {"symbol": "XUSDT", "incomeType": "COMMISSION", "income": "-0.1"},
            {"symbol": "YUSDT", "incomeType": "REALIZED_PNL", "income": "-99"},
        ]
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())
        liq = self.db.get_liquidations(10)[0]
        self.assertAlmostEqual(float(liq["pnl"]), -10.6, places=6,
                               msg="只汇总同标的的盈亏与手续费")
        order = [o for o in self.db.get_orders(10) if o["type"] == "liquidation"][0]
        self.assertAlmostEqual(float(order["pnl"]), -10.6, places=6,
                               msg="成交记录里也要带上真实强平盈亏")

    def test_sync_splits_pnl_across_legs_by_notional(self):
        """同标的多空两条腿同时被强平 → 盈亏按名义分摊，合计不重复。"""
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.cl.force_orders = [
            force_order("F1", "XUSDT", ts=ts, side="sell", ps="LONG", price=1.0, qty=100),
            force_order("F2", "XUSDT", ts=ts, side="buy", ps="SHORT", price=1.0, qty=300),
        ]
        self.cl.income_records = [
            {"symbol": "XUSDT", "incomeType": "REALIZED_PNL", "income": "-40"},
        ]
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())
        rows = sorted(self.db.get_liquidations(10), key=lambda x: x["order_id"])
        self.assertAlmostEqual(sum(float(x["pnl"]) for x in rows), -40.0, places=6)
        self.assertAlmostEqual(float(rows[0]["pnl"]), -10.0, places=6)   # 100/400
        self.assertAlmostEqual(float(rows[1]["pnl"]), -30.0, places=6)   # 300/400

    def test_sync_failure_does_not_break_engine(self):
        async def boom(since_ms=None, end_ms=None, limit=100):
            raise RuntimeError("接口 503")
        self.cl.fetch_force_orders = boom
        eng = Engine(self.cl, self.db)
        run(eng._sync_liquidations())                   # 不应抛异常
        self.assertEqual(self.db.liquidation_stats()["count"], 0)


class TestGridDisplayRule(Base):
    """强平/清仓后：无持仓且无挂单 → 不展示；有其一 → 继续展示。"""

    def test_hides_empty_grid_state(self):
        self.db.upsert_grid_state("XUSDT", lower=0.8, upper=1.2, grids=8,
                                  size_usdt=5.0, base_qty=0.0, total_cost=0.0)
        g = make_grid(self.cl, self.db, grid_cfg(auto_select=False), selection=["XUSDT"])
        s = run(g.status())
        self.assertEqual(s["symbols"], [], "无持仓无挂单的网格不该展示")

    def test_shows_grid_with_pending_orders(self):
        self.db.upsert_grid_state("XUSDT", lower=0.8, upper=1.2, grids=8,
                                  size_usdt=5.0, base_qty=0.0, total_cost=0.0)
        self.db.insert_grid_level("XUSDT", 0, 0.85, "buy", 6)
        g = make_grid(self.cl, self.db, grid_cfg(auto_select=False), selection=["XUSDT"])
        s = run(g.status())
        self.assertEqual(len(s["symbols"]), 1, "有挂单（等待建仓）应继续展示")

    def test_shows_grid_with_position(self):
        self.db.upsert_grid_state("XUSDT", lower=0.8, upper=1.2, grids=8,
                                  size_usdt=5.0, base_qty=100.0, total_cost=95.0)
        g = make_grid(self.cl, self.db, grid_cfg(auto_select=False), selection=["XUSDT"])
        s = run(g.status())
        self.assertEqual(len(s["symbols"]), 1, "有持仓应继续展示")


if __name__ == "__main__":
    unittest.main()
