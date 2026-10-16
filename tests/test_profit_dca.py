"""利润定投：合约赚的钱按台阶买现货，且**绝不循环定投**。

核心不变量（用户明确要求「不能循环定投，一定是涨了多少后投多少比例」）：
  水位线只升不降 → 盈利回吐再涨回来**不会**重复触发，必须有**新的**利润才投。
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

from strategies.profit_dca import ANCHOR_KEY, NOTE, ProfitDCA
from tests.harness import FakeClient, temp_db


def cfg(enabled=True, step=20.0, ratio=0.3, cap=0.0, symbols=("BTCUSDT",)):
    return {"profit_dca": {"enabled": enabled, "step_usdt": step, "ratio": ratio,
                           "max_total": cap, "symbols": list(symbols)}}


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def earn(self, amount: float, sym: str = "X"):
        """记一笔平仓盈利（写进 orders，与页面「总盈亏」同源）。"""
        self.db.insert_order({"mode": "grid", "symbol": sym, "side": "sell",
                              "type": "grid_sell", "price": 1.0, "amount": 1.0,
                              "pnl": amount, "note": "测试"})

    def pdc(self, **kw):
        return ProfitDCA(FakeClient(), self.db, cfg(**kw))


class TestTriggerMath(Base):
    def test_no_trigger_below_one_step(self):
        self.earn(19.9)
        n, per, invest = self.pdc(step=20.0, ratio=0.3).pending_steps()
        self.assertEqual(n, 0)
        self.assertAlmostEqual(per, 6.0)
        self.assertAlmostEqual(invest, 0.0)

    def test_one_step_triggers_one_investment(self):
        self.earn(20.0)
        n, per, invest = self.pdc(step=20.0, ratio=0.3).pending_steps()
        self.assertEqual(n, 1)
        self.assertAlmostEqual(invest, 6.0, places=9)

    def test_catches_up_multiple_steps_after_downtime(self):
        """离线期间盈利涨了 3 档 → 一次补 3 档，不漏也不多。"""
        self.earn(68.0)
        n, _, invest = self.pdc(step=20.0, ratio=0.3).pending_steps()
        self.assertEqual(n, 3)
        self.assertAlmostEqual(invest, 18.0, places=9)

    def test_disabled_never_triggers(self):
        self.earn(100.0)
        n, _, invest = self.pdc(enabled=False, step=20.0, ratio=0.3).pending_steps()
        self.assertEqual((n, invest), (0, 0.0))

    def test_cap_limits_steps(self):
        self.earn(100.0)
        self.db.insert_dca_record({"symbol": "BTCUSDT", "cost": 4.0, "note": NOTE})
        n, per, invest = self.pdc(step=20.0, ratio=0.3, cap=5.0).pending_steps()
        self.assertEqual(n, 0)
        # 上限剩 5-4=1U，每档 6U → 一档都投不了
        self.assertAlmostEqual(invest, 0.0)


class TestNoLooping(Base):
    """「不能循环定投」的证明。"""

    def test_profit_oscillation_does_not_retrigger(self):
        p = self.pdc(step=20.0, ratio=0.3)
        self.earn(24.0)                          # 累计盈利 24
        self.assertEqual(p.pending_steps()[0], 1)
        p._set_anchor(20.0)                      # 模拟这一档已投完

        # 盈利回吐到 12U：不该有任何待投
        self.earn(-12.0)
        self.assertEqual(p.pending_steps()[0], 0)

        # 又涨回 28U —— 只是回到原来的高度附近，**不该**再投一次
        self.earn(16.0)
        self.assertEqual(p.pending_steps()[0], 0,
                         "盈利回吐再涨回来不能重复触发（水位线只升不降）")

        # 涨到 44U 才越过「20→40」的新台阶
        self.earn(16.0)
        self.assertEqual(p.pending_steps()[0], 1)

    def test_anchor_only_moves_up(self):
        p = self.pdc()
        p._set_anchor(7.0)
        self.earn(-100.0)                        # 大幅亏损
        self.assertEqual(p._anchor(), 7.0, "亏损不能把水位线拉下来")
        self.assertEqual(p.pending_steps()[0], 0)


class TestExecution(Base):
    def test_successful_buy_advances_anchor_and_records(self):
        self.earn(20.0)
        cl = FakeClient(price=100.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("BTCUSDT",)))
        run(p.step())

        self.assertAlmostEqual(p._anchor(), 20.0, places=6, msg="买成了才推进水位线")
        recs = self.db.get_dca_records(10)
        self.assertEqual(len(recs), 1)
        self.assertIn(NOTE, recs[0]["note"], "要能区分「利润定投」与「定期定投」")
        self.assertTrue(self.db.get_dca_state("BTCUSDT"))

    def test_buy_failure_keeps_anchor_so_it_retries(self):
        self.earn(20.0)
        cl = FakeClient(price=100.0)
        cl.create_market_order = lambda *a, **k: _none()
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("BTCUSDT",)))
        run(p.step())
        self.assertEqual(p._anchor(), 0.0, "没买成不能推进水位线，否则永久吞掉一次定投")
        self.assertEqual(len(self.db.get_dca_records(10)), 0)

    def test_no_symbols_does_not_advance(self):
        self.earn(20.0)
        p = ProfitDCA(FakeClient(), self.db, cfg(step=20.0, ratio=0.3, symbols=()))
        run(p.step())
        self.assertEqual(p._anchor(), 0.0)

    def test_second_tick_after_success_is_noop(self):
        self.earn(20.0)
        cl = FakeClient(price=100.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("BTCUSDT",)))
        run(p.step())
        run(p.step())                            # 再跑一次不该又买
        self.assertEqual(len(self.db.get_dca_records(10)), 1)


class TestMinimumOrderGuard(Base):
    """交易所最小下单额（币安现货 ≈5U）—— 不够就跳过，绝不超支。"""

    def test_share_below_min_notional_skips_and_accumulates(self):
        self.earn(20.0)                       # 触发 1 档 = 6U
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3,
                                       symbols=("AUSDT", "BUSDT", "CUSDT")))
        run(p.step())
        # 6U 分给 3 个标的 → 每个 2U < 5U 最小额 → 一个都买不了
        self.assertEqual(len(self.db.get_dca_records(10)), 0, "买不起就不该下单")
        self.assertEqual(p._anchor(), 0.0, "没买成不能推进水位线")

        # 再赚 20U（累计 40U）→ 攒够 2 档 → 下次应投 12U
        self.earn(20.0)
        n, _, invest = p.pending_steps()
        self.assertEqual(n, 2, "水位线没动，档位会自然累积")
        self.assertAlmostEqual(invest, 12.0, places=6)

    def test_affordable_share_buys_normally(self):
        self.earn(20.0)
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("AUSDT",)))
        run(p.step())
        recs = self.db.get_dca_records(10)
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(recs[0]["cost"], 6.0, places=6)

    def test_never_overspends_beyond_the_step_budget(self):
        """1.5U 的预算绝不能下出 16U 的单（实测过的 bug）。"""
        self.earn(20.0)
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=5.0, ratio=0.3,
                                       symbols=("AUSDT", "BUSDT")))
        run(p.step())
        total = sum(r["cost"] for r in self.db.get_dca_records(10))
        self.assertLessEqual(total, 1.5 * 1.5 + 1e-9,
                             f"预算 1.5U 却花掉 {total:.2f}U")


class TestAnchorAdvancesByWholeSteps(Base):
    """水位线只能**按整档**推进 —— 否则会被超支放大、一步吞掉好几档。"""

    def test_overspend_does_not_swallow_extra_steps(self):
        """预算 1U/档、最小一手把实际支出抬到 5U 时，只能推进 1 档，不能是 5 档。"""
        self.earn(60.0)                          # 累计 60 → 3 档（step=20）
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.05, symbols=("AUSDT",)))
        # 每档预算 = 20×0.05 = 1U，分给 1 个标的 → 1U < 最小一手 5U
        # 但 5U ≤ 1U×1.2? 不成立 → 会被跳过，anchor 不动
        run(p.step())
        self.assertEqual(p._anchor(), 0.0, "买不起就攒着，不动水位线")

    def test_partial_fill_does_not_advance(self):
        self.earn(20.0)                          # 1 档 = 6U
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3,
                                       symbols=("AUSDT", "BUSDT", "CUSDT")))
        run(p.step())                            # 每份 2U < 5U → 全跳过
        self.assertEqual(p._anchor(), 0.0)

    def test_full_fill_advances_exactly_one_step(self):
        self.earn(20.0)
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("AUSDT",)))
        run(p.step())
        self.assertAlmostEqual(p._anchor(), 20.0, places=6,
                               msg="投出去 6U（正好一档预算）→ 水位线推进整 20U")

    def test_two_steps_advance_two_steps(self):
        self.earn(40.0)
        cl = FakeClient(price=100.0, min_notional=5.0)
        p = ProfitDCA(cl, self.db, cfg(step=20.0, ratio=0.3, symbols=("AUSDT",)))
        run(p.step())
        self.assertAlmostEqual(p._anchor(), 40.0, places=6)


class TestStatus(Base):
    def test_status_reports_distance_to_next_step(self):
        self.earn(3.2)
        s = self.pdc(step=20.0, ratio=0.3).status()
        self.assertAlmostEqual(s["realized_total"], 3.2, places=6)
        self.assertAlmostEqual(s["to_next_step"], 16.8, places=6)
        self.assertEqual(s["pending_steps"], 0)


async def _none():
    return None


if __name__ == "__main__":
    unittest.main()
