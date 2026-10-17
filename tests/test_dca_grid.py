"""现货交易 = 现货马丁格尔网格（用户定的方向）。

一轮循环：首单市价建仓 → 挂「上一档价×(1−间隔)」的加仓买单 +
「均价×(1+止盈)」的止盈卖单 → 加仓成交撤旧止盈挂新止盈 → 止盈成交
整轮卖出并立即开下一轮。

状态全部由 spot_orders（purpose="dca"）的成交记录推导 —— 与利润定投
（purpose="profit_dca"）天然隔离，本文件专门有一条测试守住这一点。
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest

from strategies.dca import PURPOSE, DCAStrategy
from tests.harness import FakeClient, temp_db


def cfg(**over):
    d = {"enabled": True, "symbols": ["DOGEUSDT"], "amount_usdt": 5,
         "step_dev": 0.02, "step_scale": 1.5, "max_steps": 3,
         "tp_pct": 0.015, "max_total_usdt": 0,
         "first_buy_immediately": True, "interval_hours": 24}
    d.update(over)
    return {"dca": d}


def run(c):
    return asyncio.run(c)


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()
        self.cl = FakeClient(price=100.0, spot_free=1000.0)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def make(self, **over):
        p = DCAStrategy(self.cl, self.db, cfg(**over))
        return p

    def pending(self):
        return self.db.pending_spot_orders(PURPOSE)

    def sells_of(self, rows):
        return [r for r in rows if r["side"] == "sell"]

    def buys_of(self, rows):
        return [r for r in rows if r["side"] == "buy"]


class TestFirstRound(Base):
    def test_first_round_places_tp_and_step(self):
        """首单市价建仓后，立刻挂出止盈卖单 + 第一档加仓买单。"""
        p = self.make()
        run(p.step())
        self.assertEqual(len(self.cl.market_orders), 1)
        sym, side, amt = self.cl.market_orders[0]
        self.assertEqual((side,), ("buy",))
        self.assertAlmostEqual(amt, 0.05, places=9)          # 5U @ 100
        self.assertEqual(len(self.cl.limit_orders), 2)
        # 挂单顺序：先止盈卖单，再加仓买单
        s_sym, s_side, s_amt, s_px, s_kind, _ = self.cl.limit_orders[0]
        b_sym, b_side, b_amt, b_px, b_kind, _ = self.cl.limit_orders[1]
        self.assertEqual((s_side, s_kind), ("sell", "spot"))
        self.assertAlmostEqual(s_px, 101.5, places=6)        # 100 × 1.015
        self.assertAlmostEqual(s_amt, 0.05, places=9)        # 卖量 = 全部持仓
        self.assertEqual((b_side, b_kind), ("buy", "spot"))
        self.assertAlmostEqual(b_px, 98.0, places=6)         # 100 × 0.98
        self.assertAlmostEqual(b_amt * b_px, 7.5, places=6)  # 5 × 1.5
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual(self.cl.transfers, [], "现货钱够就不该划转")

    def test_transfer_only_shortfall_for_first_order(self):
        self.cl = FakeClient(price=100.0, spot_free=1.0, futures_available=100.0)
        p = self.make()
        run(p.step())
        # 第一笔划转 = 首单缺口（5U 需求 − 1U 现货）；后面加仓单还缺口 7.5U
        self.assertEqual(self.cl.transfers[0], 4.0, "首单需 5U，现货只有 1U → 只划缺口")

    def test_no_first_order_when_transfer_impossible(self):
        self.cl = FakeClient(price=100.0, spot_free=1.0, futures_available=1.0)
        p = self.make()
        run(p.step())
        self.assertEqual(self.cl.market_orders, [])
        self.assertEqual(self.cl.limit_orders, [])

    def test_overspend_guard_skips_tiny_first_order(self):
        """最小一手远超首单预算（7U > 5U×1.2）→ 不开仓，不硬买。"""
        self.cl = FakeClient(price=100.0, spot_free=1000.0, min_notional=7.0)
        p = self.make()
        run(p.step())
        self.assertEqual(self.cl.market_orders, [])
        self.assertEqual(self.cl.limit_orders, [])

    def test_first_buy_delayed_by_switch(self):
        """first_buy_immediately=False：新标的要等 interval_hours。"""
        p = self.make(first_buy_immediately=False)
        run(p.step())
        self.assertEqual(self.cl.market_orders, [], "第一次见到不该立刻买")
        key = "dca_seen:DOGEUSDT"
        seen = float(self.db.get_runtime_stat(key, 0) or 0)
        self.assertGreater(seen, 0, "应记下首次见到的时刻")
        run(p.step())
        self.assertEqual(self.cl.market_orders, [], "没到等待时间不该买")
        # 时间拨到 25 小时后 → 到点开仓
        self.db.set_runtime_stat(key, time.time() - 25 * 3600)
        run(p.step())
        self.assertEqual(len(self.cl.market_orders), 1)

    def test_max_steps_zero_skips_buy_order(self):
        p = self.make(max_steps=0)
        run(p.step())
        self.assertEqual(len([t for t in self.cl.limit_orders if t[1] == "sell"]), 1)
        self.assertEqual([t for t in self.cl.limit_orders if t[1] == "buy"], [],
                         "max_steps=0 不该有加仓单")


class TestStepBuying(Base):
    def test_step_fill_updates_tp_and_places_next(self):
        """第一档加仓成交 → 均价下降 → 撤旧止盈、挂新止盈 + 第二档。"""
        p = self.make()
        run(p.step())
        first_sell = self.cl.limit_orders[0]
        buy_row = self.buys_of(self.pending())[0]
        self.cl.spot_orders_status[buy_row["order_id"]] = {
            "status": "closed", "average": 98.0, "filled": buy_row["amount"]}
        run(p.step())
        # 旧止盈被撤
        self.assertIn(("DOGEUSDT", str(first_sell[5])), self.cl.cancelled)
        # 新持仓：0.05@100 + 0.0765…@98 → 均价 ≈98.801
        st = p._round_state("DOGEUSDT")
        self.assertAlmostEqual(st["qty"], 0.05 + 7.5 / 98.0, places=9)
        self.assertAlmostEqual(st["avg"], 12.5 / (0.05 + 7.5 / 98.0), places=6)
        self.assertEqual(st["steps"], 1)
        pend = self.pending()
        self.assertEqual(len(self.sells_of(pend)), 1, "应挂新止盈")
        new_sell = self.sells_of(pend)[0]
        self.assertAlmostEqual(new_sell["price"], st["avg"] * 1.015, places=6)
        self.assertEqual(len(self.buys_of(pend)), 1, "应挂第二档加仓")
        nxt = self.buys_of(pend)[0]
        self.assertAlmostEqual(nxt["price"], 98.0 * 0.98, places=6)   # 96.04
        self.assertAlmostEqual(nxt["cost_usdt"], 11.25, places=6)     # 7.5 × 1.5

    def test_cap_stops_step_buying(self):
        """单轮投入到顶（5+7.5 > 10）→ 不再挂加仓单。"""
        p = self.make(max_total_usdt=10)
        run(p.step())
        pend = self.pending()
        self.assertEqual(len(self.sells_of(pend)), 1)
        self.assertEqual(self.buys_of(pend), [], "单轮预算用完不该挂加仓")


class TestRoundClose(Base):
    def test_tp_fill_closes_round_and_starts_next(self):
        """止盈成交 → 一轮完成（盈亏入账）→ 撤加仓 → 立即开下一轮。"""
        p = self.make(max_steps=0)
        run(p.step())
        sell_row = self.sells_of(self.pending())[0]
        self.cl.spot_orders_status[sell_row["order_id"]] = {
            "status": "closed", "average": 101.5, "filled": sell_row["amount"]}
        run(p.step())
        # 一轮的账：投入 5U @100 → 卖出 0.05 @101.5
        n, realized = self.db.dca_round_stats()
        self.assertEqual(n, 1)
        self.assertAlmostEqual(realized, 5.075 - 5.0 - (5.0 + 5.075) * 0.001, places=9)
        # 立即开下一轮：第二笔市价首单 + 新止盈单
        self.assertEqual(len(self.cl.market_orders), 2)
        sells = [t for t in self.cl.limit_orders if t[1] == "sell"]
        self.assertEqual(len(sells), 2, "新一轮应挂出新的止盈单")

    def test_pending_buy_blocks_new_round_after_cancel_failure(self):
        """止盈成交但加仓单撤不掉 → 先不开新一轮，等撤干净。"""
        p = self.make(max_steps=1)
        run(p.step())
        sell_row = self.sells_of(self.pending())[0]
        self.cl.spot_orders_status[sell_row["order_id"]] = {
            "status": "closed", "average": 101.5, "filled": sell_row["amount"]}
        self.cl.cancel_ok = False          # 撤单失败
        run(p.step())
        self.assertEqual(len(self.cl.market_orders), 1, "撤单失败就不该开新一轮")
        self.assertEqual(len(self.pending()), 1, "加仓单保留 pending，下轮再撤")

    def test_cancelled_buy_is_replaced(self):
        """加仓单被交易所撤销 → 下轮重新挂出（价格不变，等回落）。"""
        p = self.make()
        run(p.step())
        buy_row = self.buys_of(self.pending())[0]
        self.cl.spot_orders_status[buy_row["order_id"]] = {"status": "canceled"}
        run(p.step())
        pend = self.pending()
        self.assertEqual(len(self.buys_of(pend)), 1, "撤销后应重新挂")
        self.assertAlmostEqual(self.buys_of(pend)[0]["price"], 98.0, places=6)


class TestIsolation(Base):
    def test_profit_dca_fills_do_not_leak_into_grid(self):
        """利润定投买的币（purpose=profit_dca）绝不混进马丁的持仓推导。"""
        self.db.insert_spot_order({"symbol": "DOGEUSDT", "side": "buy",
                                   "amount": 1.0, "price": 100.0, "cost_usdt": 100.0,
                                   "order_id": "PD1", "purpose": "profit_dca",
                                   "status": "filled", "filled_ts": "2026-01-01T00:00:00"})
        p = self.make()
        run(p.step())
        st = p._round_state("DOGEUSDT")
        self.assertAlmostEqual(st["qty"], 0.05, places=9,
                               msg="马丁持仓不含利润定投的量")
        self.assertAlmostEqual(st["avg"], 100.0, places=6)
        sells = [t for t in self.cl.limit_orders if t[1] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0][3], 101.5, places=6,
                               msg="止盈价只按马丁自己的均价")
        self.assertAlmostEqual(sells[0][2], 0.05, places=9,
                               msg="卖量只卖马丁自己的持仓")


class TestStatus(Base):
    def test_status_shows_grid_state(self):
        p = self.make()
        run(p.step())
        s = run(p.status())
        row = [r for r in s["symbols"] if r["symbol"] == "DOGEUSDT"][0]
        g = row["grid"]
        self.assertTrue(g["active"])
        self.assertEqual(g["steps"], 0)
        self.assertAlmostEqual(g["tp_price"], 101.5, places=6)
        self.assertAlmostEqual(g["next_buy_price"], 98.0, places=6)
        self.assertEqual(len(g["pending"]), 2)
        self.assertEqual(s["rounds"], 0)

    def test_status_shows_completed_rounds(self):
        p = self.make(max_steps=0)
        run(p.step())
        sell_row = self.sells_of(self.pending())[0]
        self.cl.spot_orders_status[sell_row["order_id"]] = {
            "status": "closed", "average": 101.5, "filled": sell_row["amount"]}
        run(p.step())
        s = run(p.status())
        self.assertEqual(s["rounds"], 1)
        # status() 里盈亏按 4 位小数展示
        self.assertAlmostEqual(s["realized_pnl"], 0.0649, places=6)


class TestSellAmount(Base):
    def test_sell_qty_never_exceeds_real_balance(self):
        """物理余额比推导持有时 → 卖出量取余额（绝不能超卖）。"""
        p = self.make(max_steps=0)
        run(p.step())
        self.cl.asset_free = 0.03          # 实际只有 0.03 个币
        st = p._round_state("DOGEUSDT")
        run(p._place_tp("DOGEUSDT", st))
        sells = [t for t in self.cl.limit_orders if t[1] == "sell"]
        # 第一次挂单在 asset_free 设置之前（量 0.05），第二次挂单被余额截断为 0.03
        self.assertAlmostEqual(sells[-1][2], 0.03, places=9)


if __name__ == "__main__":
    unittest.main()
