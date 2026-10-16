"""历史事故回归测试。

每个用例对应一次真实发生过的事故 —— 改任何相关代码前先跑这个。
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

from tests.harness import FakeClient, grid_cfg, make_grid, temp_db


def run(coro):
    return asyncio.run(coro)


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def trade(tid, ts_ms, side, ps, amt, px, rpnl="0"):
    """造一条币安成交明细（形状与 fetch_my_trades 一致）。"""
    return {"id": tid, "timestamp": ts_ms, "side": side, "amount": amt, "price": px,
            "info": {"positionSide": ps, "realizedPnl": rpnl, "commission": "0.001"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)


# --------------------------------------------------------------------------
# 事故 1：对账与成交检测双重记账 → 14 条幽灵记录（pnl 恒为 0）
# --------------------------------------------------------------------------
class TestNoDoubleCounting(Base):
    def test_reconcile_and_fill_same_tick_do_not_double_count(self):
        db, sym = self.db, "X"
        db.upsert_grid_state(sym, lower=0.9, upper=1.1, grids=6, size_usdt=5,
                             base_qty=90, total_cost=90 * 0.95, realized_pnl=0,
                             buy_count=0, sell_count=0, status="active")
        db.add_lot(sym, 0.95, 90, "底仓")
        db.insert_grid_level(sym, 0, 0.95, "buy", 10)

        cl = FakeClient(price=0.94,
                        positions=[{"symbol": sym, "side": "long", "contracts": 100,
                                    "entryPrice": 0.95}])
        g = make_grid(cl, db, grid_cfg())
        spec = {"symbol": sym, "lower": 0.9, "upper": 1.1, "grids": 6,
                "size_usdt": 5, "auto_range_pct": 0.15}
        run(g._step_symbol(spec, 22.0))

        st = db.get_grid_state(sym)
        backfilled = [o for o in db.get_orders(50) if "对账补记" in (o["note"] or "")]
        self.assertAlmostEqual(st["base_qty"], 100, places=9,
                               msg="双重记账：本地持仓被记了两遍")
        self.assertEqual(backfilled, [], "不应再有对账补记记录")


# --------------------------------------------------------------------------
# 事故 2：成交漏记（孤儿单成交 / 挂单刚成交就被误清单号）
# --------------------------------------------------------------------------
class TestTradeBackfill(Base):
    def _seed(self):
        db, sym = self.db, "X"
        db.upsert_grid_state(sym, lower=0.9, upper=1.1, grids=6, size_usdt=5,
                             base_qty=56, total_cost=56 * 0.18546, realized_pnl=0,
                             buy_count=0, sell_count=0, status="active")
        db.insert_order({"mode": "grid", "symbol": sym, "side": "buy", "type": "grid_buy",
                         "ts": datetime.now(timezone.utc).isoformat(),
                         "price": 0.18546, "amount": 28, "cost": 28 * 0.18546,
                         "pnl": 0, "note": "网格买入"})
        return db, sym

    def test_backfill_is_idempotent_and_never_touches_position(self):
        db, sym = self._seed()
        cl = FakeClient(trades=[trade("t1", now_ms() - 60_000, "buy", "LONG", 28.0, 0.18546),
                                trade("t2", now_ms() - 45_000, "buy", "LONG", 28.0, 0.18546)])
        g = make_grid(cl, db, grid_cfg())

        n1 = run(g._backfill_trades(sym))
        self.assertEqual(n1, 2, "应处理 2 笔：1 笔确认已记 + 1 笔补录")
        self.assertEqual(len([o for o in db.get_orders(50) if o["symbol"] == sym]), 2)

        n2 = run(g._backfill_trades(sym))
        self.assertEqual(n2, 0, "幂等失败：同一批成交被处理了两次")

        self.assertAlmostEqual(db.get_grid_state(sym)["base_qty"], 56, places=9,
                               msg="补录改动了持仓（持仓只能由对账负责）")

    def test_short_close_pnl_comes_from_exchange(self):
        db, sym = self._seed()
        db.insert_order({"mode": "grid", "symbol": sym, "side": "sell", "type": "short_open",
                         "ts": "2026-10-13T00:00:00+00:00", "price": 0.05,
                         "amount": 99, "pnl": 0, "note": "空单开仓"})
        cl = FakeClient(trades=[trade("t3", now_ms() - 30_000, "sell", "SHORT", 99.0, 0.05),
                                trade("t4", now_ms() - 20_000, "buy", "SHORT", 99.0,
                                      0.053143, rpnl="-0.128")])
        g = make_grid(cl, db, grid_cfg())
        run(g._backfill_trades(sym))

        closes = [o for o in db.get_orders(50) if o["type"] == "short_close"]
        self.assertEqual(len(closes), 1)
        self.assertAlmostEqual(closes[0]["pnl"], -0.128, places=9,
                               msg="空单平仓盈亏必须取交易所权威值")


# --------------------------------------------------------------------------
# 事故 3：掉出选币清单却留着挂单，白占保证金
# --------------------------------------------------------------------------
class TestStrayOrderSweep(Base):
    def _level(self, sym, oid, side="buy"):
        self.db.insert_grid_level(sym, 0, 0.95, side, 10)
        self.db.conn.execute("UPDATE grid_levels SET order_id=? WHERE symbol=?", (oid, sym))
        self.db.conn.commit()

    def test_purges_out_of_selection_flat_symbol_only(self):
        db = self.db
        self._level("Y", "111")                       # 不在选、无持仓 → 应清掉
        db.upsert_grid_state("Z", lower=0.9, upper=1.1, grids=6, size_usdt=5,
                             base_qty=20, total_cost=20 * 0.95, status="draining")
        self._level("Z", "222")                       # 不在选但有持仓 → 不能动
        self._level("X", "333")                       # 在选 → 不能动

        cl = FakeClient()
        g = make_grid(cl, db, grid_cfg())
        run(g._sweep_stray_orders({"X"}))

        self.assertEqual(db.get_grid_levels("Y"), [], "掉出清单且无持仓的挂单应被清空")
        self.assertIn(("Y", "111"), cl.cancelled)
        self.assertEqual(len(db.get_grid_levels("Z")), 1, "有持仓的 Z 不该被砍")
        self.assertIsNotNone(db.get_grid_state("Z"))
        self.assertEqual(len(db.get_grid_levels("X")), 1, "在选清单里的 X 不该被砍")

    def test_empty_selection_never_purges(self):
        """选币失败时 active 为空 —— 「读不到」不等于「全都不在」。"""
        self._level("Y", "111")
        cl = FakeClient()
        g = make_grid(cl, db=self.db, config=grid_cfg())
        run(g._sweep_stray_orders(set()))
        self.assertEqual(len(self.db.get_grid_levels("Y")), 1, "空清单时绝不能清理")
        self.assertEqual(cl.cancelled, [])


# --------------------------------------------------------------------------
# 事故 4+5+6：空单开关 / 掉出清单撤单 / 接口失败不得当成「没有」
# --------------------------------------------------------------------------
class TestShortLifecycle(Base):
    def _short_order(self, oid="900"):
        return [{"id": oid, "side": "sell", "amount": 5, "price": 104.0,
                 "info": {"positionSide": "SHORT"}}]

    def test_switch_off_cancels_unfilled_entry(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 1)
        cl = FakeClient(open_orders=self._short_order())
        g = make_grid(cl, db, grid_cfg(short_enabled=False), selection=["X"])
        run(g._maybe_short("X", 100.0))

        self.assertIn(("X", "900"), cl.cancelled, "关掉开关后未成交的开空挂单应被撤销")
        self.assertEqual(db.get_runtime_stat("short_state:X"), 0)
        self.assertEqual(db.get_runtime_stat("short_done:X"), 0,
                         "从没成交过 → 不该消耗该币的空单额度")

    def test_switch_off_still_manages_existing_position(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 2)
        db.set_runtime_stat("short_qty:X", 5)
        db.set_runtime_stat("short_entry:X", 100.0)
        cl = FakeClient(hold=True)
        g = make_grid(cl, db, grid_cfg(short_enabled=False), selection=["Y"])
        run(g._maybe_short("X", 100.0))

        self.assertEqual(cl.cancelled, [], "已成交的空单不该被撤")
        self.assertTrue(cl.stop_orders, "持仓空单仍要挂止损")
        self.assertEqual(db.get_runtime_stat("short_state:X"), 2, "持仓状态应保持")

    def test_switch_off_skips_all_work_when_flat(self):
        cl = FakeClient()
        g = make_grid(cl, self.db, grid_cfg(short_enabled=False), selection=["X"])
        run(g._maybe_short("X", 100.0))
        self.assertEqual(cl.positions_calls, 0,
                         "关闭开关且无空头时不该再有任何空单轮询开销")

    def test_dropout_cancels_unfilled_entry(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 1)
        cl = FakeClient(open_orders=self._short_order("888"))
        g = make_grid(cl, db, grid_cfg(short_enabled=True), selection=["Y"])
        run(g._maybe_short("X", 100.0))
        self.assertIn(("X", "888"), cl.cancelled, "掉出 top5 且未成交 → 应撤单")
        self.assertEqual(db.get_runtime_stat("short_state:X"), 0)

    def test_still_selected_keeps_waiting(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 1)
        cl = FakeClient(open_orders=self._short_order("888"))
        g = make_grid(cl, db, grid_cfg(short_enabled=True), selection=["X"])
        run(g._maybe_short("X", 100.0))
        self.assertEqual(cl.cancelled, [], "还在 top5 里 → 继续等成交")
        self.assertEqual(db.get_runtime_stat("short_state:X"), 1)

    def test_dropout_keeps_managing_filled_position(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 2)
        db.set_runtime_stat("short_qty:X", 5)
        db.set_runtime_stat("short_entry:X", 100.0)
        cl = FakeClient(hold=True)
        g = make_grid(cl, db, grid_cfg(short_enabled=True), selection=["Y"])
        run(g._maybe_short("X", 100.0))
        self.assertEqual(cl.cancelled, [])
        self.assertTrue(cl.stop_orders, "已成交的空单掉出清单后仍要挂止损")


class TestInterfaceFailureSafety(Base):
    """读不到 ≠ 没有。这三条是「留下无止损裸空头」的防线。"""

    def test_position_read_failure_never_fakes_a_close(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 2)
        db.set_runtime_stat("short_qty:X", 5)
        db.set_runtime_stat("short_entry:X", 100.0)
        cl = FakeClient(raise_positions=True)
        g = make_grid(cl, db, grid_cfg(short_enabled=True), selection=["X"])
        run(g._maybe_short("X", 101.0))

        closes = [o for o in db.get_orders(20) if o["type"] == "short_close"]
        self.assertEqual(closes, [], "接口失败不能补记平仓")
        self.assertEqual(db.get_runtime_stat("short_state:X"), 2,
                         "接口失败不能把状态清零（否则裸空头失去止损）")

    def test_open_orders_query_failure_keeps_state(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 1)
        cl = FakeClient(raise_open_orders=True)
        g = make_grid(cl, db, grid_cfg(short_enabled=False), selection=["X"])
        run(g._maybe_short("X", 100.0))
        self.assertEqual(db.get_runtime_stat("short_state:X"), 1,
                         "查询失败时不能清零状态，否则那张单再没人认领")

    def test_cancel_failure_keeps_state(self):
        db = self.db
        db.set_runtime_stat("short_state:X", 1)
        cl = FakeClient(open_orders=[{"id": "777", "side": "sell", "amount": 5,
                                      "price": 104.0, "info": {"positionSide": "SHORT"}}],
                        cancel_ok=False)
        g = make_grid(cl, db, grid_cfg(short_enabled=False), selection=["X"])
        run(g._maybe_short("X", 100.0))
        self.assertEqual(db.get_runtime_stat("short_state:X"), 1,
                         "撤单失败时不能清零状态，否则单子还在交易所却没人管了")


if __name__ == "__main__":
    unittest.main()
