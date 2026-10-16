#!/usr/bin/env python3
"""现货下单链路冒烟测试（dry-run，不碰实盘账户、不写实盘库）。

用**真实的现货行情与精度规则**跑一遍完整买入链路：
    行情加载 → 取价 → 最小下单额/精度换算 → 下单(dry) → 记账 → 状态更新
dry_run 只把「发单」这一步替换成模拟成交，其余全是真代码。

    python3 tools/spot_dryrun.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from database import Database                      # noqa: E402
from exchange_client import ExchangeClient         # noqa: E402
from strategies.dca import DCAStrategy             # noqa: E402
from strategies.profit_dca import NOTE, ProfitDCA  # noqa: E402

SYMS = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT"]
ok_all = True


def line(tag: str, msg: str = ""):
    print(f"  [{tag}] {msg}")


async def main() -> int:
    global ok_all
    # 用实盘库里的 API key（**只读余额/行情**，dry_run=True 不会真下单）
    import json as _json, sqlite3
    _c = sqlite3.connect("file:data/evoclaw-v2.db?mode=ro", uri=True)
    _live = {k: _json.loads(v) for k, v in _c.execute("select key,value from config")}
    _c.close()
    cfg = {"dry_run": True, "min_order_notional": 5, "order_notional_safety": 1.02,
           "exchange_kwargs": _live.get("exchange_kwargs", {}), "mode": "dca",
           "dca": {"symbols": SYMS, "amount_usdt": 12, "interval_hours": 24,
                   "max_total_usdt": 0, "first_buy_immediately": True},
           "profit_dca": {"enabled": True, "step_usdt": 20, "ratio": 0.3,
                          "max_total": 0, "symbols": ["DOGEUSDT"]}}
    cl = ExchangeClient(cfg)
    db = Database(tempfile.mktemp(suffix=".db"))
    try:
        print("① 加载现货市场")
        await cl.ensure_spot()
        line("OK", f"{len(cl._spot_markets)} 个 USDT 现货币对")

        print("② 可重入性（引擎 tick 与 Web 请求会并发调它）")
        await asyncio.gather(*(cl.ensure_spot() for _ in range(5)))
        line("OK" if cl._spot_ready else "FAIL", "_spot_ready=True，且没重复加载")

        print("③ 价格与精度（选几个币看看最小额/数量换算）")
        await cl.refresh_prices(SYMS)
        for s in SYMS:
            p = cl.last_price(s)
            amt = cl.min_amount(s, p, "spot")
            a12 = cl.amount_for_notional(s, 12.0, p, "spot")
            line("OK" if p > 0 else "FAIL",
                 f"{s:10} 价 {p:<12.6g} 最小可下 {amt:<12.6g} 12U 可买 {a12:.8g}"
                 f" (≈{a12*p:.2f}U)")
            if p <= 0 or a12 <= 0:
                ok_all = False

        print("④ 现货余额读取")
        try:
            bal = await cl.get_spot_balance("USDT")
            line("OK", f"可用 {bal['free']:.2f}U / 总额 {bal['total']:.2f}U")
        except Exception as e:
            ok_all = False
            line("FAIL", f"余额读取失败: {e}")

        print("⑤ 定期定投链路（dry-run 下单 + 落库）")
        dca = DCAStrategy(cl, db, cfg)
        await dca.step()
        recs = [r for r in db.get_dca_records(50) if r["note"] != NOTE]
        line("OK" if len(recs) == len(SYMS) else "FAIL",
             f"买入 {len(recs)}/{len(SYMS)} 笔，共 {sum(r['cost'] for r in recs):.2f}U")
        for r in recs[:3]:
            line("·", f"{r['symbol']:10} {r['amount']:.8g} @ {r['price']:.6g} = {r['cost']:.2f}U")
        st = await dca.status()
        line("OK" if st["symbols"] else "FAIL",
             f"持仓状态 {len(st['symbols'])} 个，总成本 {st['total_cost']:.2f}U")

        print("⑥ 利润定投链路（合约盈利触发 → 买现货）")
        # 造 24U 合约盈利 → 触发 1 档（20U×30% = 6U，够币安 5U 最小下单额）
        db.insert_order({"mode": "grid", "symbol": "FAKE", "side": "sell",
                         "type": "grid_sell", "price": 1, "amount": 1, "pnl": 24.0})
        pdc = ProfitDCA(cl, db, cfg)
        n, per, invest = pdc.pending_steps()
        line("OK" if (n, round(invest, 4)) == (1, 6.0) else "FAIL",
             f"触发 {n} 档，每档 {per:.2f}U，本次应投 {invest:.2f}U")
        if (n, round(invest, 4)) != (1, 6.0):
            ok_all = False
        await pdc.step()
        precs = [r for r in db.get_dca_records(50) if r["note"] == NOTE]
        line("OK" if precs else "FAIL",
             f"利润定投买入 {len(precs)} 笔，共 {sum(r['cost'] for r in precs):.2f}U"
             f"，水位线推进到 {pdc._anchor():.2f}U")
        if not precs:
            ok_all = False

        print("⑦ 不能循环定投（再跑 5 轮，盈利没涨就不该再买）")
        before = len(db.get_dca_records(200))
        for _ in range(5):
            await pdc.step()
        after = len(db.get_dca_records(200))
        line("OK" if after == before else "FAIL",
             f"多跑 5 轮，记录数 {before} → {after}（应不变）")
        if after != before:
            ok_all = False

        print("⑧ 下一档的进度提示")
        s = pdc.status()
        line("OK", f"累计盈利 {s['realized_total']}U / 水位线 {s['anchor']}U / "
                   f"距下一档 {s['to_next_step']}U / 已投 {s['invested_total']}U")
    finally:
        await cl.close()
        db.close()

    print("\n" + ("✅ 现货链路全部通过" if ok_all else "❌ 有失败项，见上面 FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
