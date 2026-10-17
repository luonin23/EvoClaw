"""现货筛选榜的数据真实性（用户实测发现错误榜单后补的防线）。

三类污染源，各对应一条守卫：
  1. 退市/停牌币 —— K 线停在几个月前，却按「停牌价 vs 更早的开盘」算出
     假「N 日跌幅」（实测 NFPUSDT：K 线停在 07-10，报出 -77.68% 的「30 日跌幅」）
  2. 上市不满 N 天 —— 窗口不完整，短窗口的极端值污染榜单
  3. 单次拉取超时 —— 头部几个恰好失败时，跌幅第一从 -77% 变成 -17%；
     残缺榜还会被缓存，错误持续 10 分钟
"""
from __future__ import annotations

import asyncio
import unittest

from exchange_client import ExchangeClient
from tests.harness import ROOT  # noqa: F401  (确保 sys.path)


def day_candles(now_ms: int, n: int, first_open: float = 1.0, last_close: float = None,
                step_days_ms: int = 86_400_000):
    """造 n 根日线：等差价格，最后一根的时间戳由 age_days 决定（调用方改）。"""
    last_close = first_open * 0.2 if last_close is None else last_close
    out = []
    for i in range(n):
        p = first_open + (last_close - first_open) * i / max(1, n - 1)
        out.append([now_ms - (n - 1 - i) * step_days_ms, p, p * 1.02, p * 0.98, p, 1000.0])
    return out


def client_with(candles: dict[str, list], markets: dict[str, dict]):
    """真实 ExchangeClient（跳过 __init__），注入可控的 tickers/K 线。"""
    cl = ExchangeClient.__new__(ExchangeClient)
    cl.config = {}
    cl.dry_run = True
    cl._closed = False                          # _safe_call 会检查
    cl.min_order_notional = 5.0
    cl.notional_safety = 1.02
    cl._fut_markets = {}
    cl._spot_markets = markets
    cl._reverse = {sym: sym.replace("/", "") for sym in markets}
    now = candles.get("_now", 1_800_000_000_000)

    class _SpotStub:
        milliseconds = staticmethod(lambda: now)

        async def fetch_tickers(self):
            return {sym: {"last": 1.0, "quoteVolume": 1e8} for sym in markets}

        async def fetch_ohlcv(self, sym, tf, since, limit):
            data = candles.get(sym)
            if data is None:
                raise RuntimeError("模拟接口超时")
            return [list(c) for c in data]

    cl._spot = _SpotStub()
    cl._spot_lock = asyncio.Lock()
    cl._spot_ready = True                       # 测试里市场表已注入，跳过真实加载
    return cl


def mkt(sym: str) -> dict:
    return {"precision": {"amount": 1.0}, "limits": {"amount": {"min": 1.0}}}


NOW = 1_800_000_000_000


class TestSpotScreenTruthfulness(unittest.TestCase):
    def run_screen(self, cl, days=30):
        return asyncio.run(cl.spot_screen(days=days))

    def test_dead_coin_is_excluded(self):
        """K 线停在 90 天前的币：绝不能带着假「N 日跌幅」上榜。"""
        cl = client_with({
            "_now": NOW,
            "ALIVE/USDT": day_candles(NOW, 31),
            "DEAD/USDT": day_candles(NOW - 90 * 86_400_000, 31),   # 停牌 90 天
        }, {"ALIVE/USDT": mkt("A"), "DEAD/USDT": mkt("D")})
        res = self.run_screen(cl)
        syms = {r["symbol"] for r in res["items"]}
        self.assertIn("ALIVEUSDT", syms)
        self.assertNotIn("DEADUSDT", syms, "停牌 90 天的币混进了 30 日榜")
        self.assertEqual(res["failed"], 1)

    def test_younger_than_window_is_excluded(self):
        """上市 10 天的币不能出现在 30 日榜（窗口不完整，口径不成立）。"""
        cl = client_with({
            "_now": NOW,
            "OLD/USDT": day_candles(NOW, 31),
            "NEW/USDT": day_candles(NOW, 10),                      # 只有 10 根
        }, {"OLD/USDT": mkt("O"), "NEW/USDT": mkt("N")})
        res = self.run_screen(cl, days=30)
        syms = {r["symbol"] for r in res["items"]}
        self.assertNotIn("NEWUSDT", syms)
        self.assertIn("OLDUSDT", syms)

    def test_failed_fetch_is_retried_not_dropped(self):
        """第一次拉取超时 → 重试一次成功 → 该币**必须在榜上**。"""
        cl = client_with({
            "_now": NOW,
            "A/USDT": day_candles(NOW, 8),
        }, {"A/USDT": mkt("A")})
        calls = {"n": 0}

        async def flaky(sym, tf, since, limit):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟超时")
            return [list(c) for c in day_candles(NOW, 8)]

        # 手动替换成「第一次失败」的桩
        class _S(cl._spot.__class__):
            async def fetch_ohlcv(self, sym, tf, since, limit):
                return await flaky(sym, tf, since, limit)
        cl._spot = _S()
        res = self.run_screen(cl, days=7)
        self.assertEqual(calls["n"], 2, "没有重试")
        self.assertEqual(len(res["items"]), 1, "重试成功后该币必须在榜上")

    def test_result_reports_failures(self):
        """失败统计要能暴露给调用方 —— 缓存层靠它拒绝缓存残缺榜。"""
        cl = client_with({
            "_now": NOW,
            "OK/USDT": day_candles(NOW, 8),
            "BAD/USDT": None,                       # 拉取必失败
        }, {"OK/USDT": mkt("O"), "BAD/USDT": mkt("B")})
        res = self.run_screen(cl, days=7)
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["failed"], 1)

    def test_change_math_uses_window_base(self):
        """涨跌幅 = 末根收盘 vs 首根开盘（30 个完整交易日 + 今日）。"""
        cl = client_with({
            "_now": NOW,
            "X/USDT": day_candles(NOW, 8, first_open=100.0, last_close=80.0),
        }, {"X/USDT": mkt("X")})
        res = self.run_screen(cl, days=7)
        self.assertAlmostEqual(res["items"][0]["change"], (80 - 100) / 100, places=9)


if __name__ == "__main__":
    unittest.main()
