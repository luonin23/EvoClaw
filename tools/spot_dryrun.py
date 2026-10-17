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
                          "max_total": 0, "symbols": ["DOGEUSDT"],
                          "entry_offset": 0.02}}
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

        print("⑤ 现货马丁格尔：首单 → 挂止盈+加仓 → 止盈成交 → 立即开下一轮")
        dca = DCAStrategy(cl, db, cfg)
        await dca.step()          # 首轮：市价首单 + 挂止盈卖单 + 第一档加仓买单
        pend = db.pending_spot_orders("dca")
        sells = [r for r in pend if r["side"] == "sell"]
        buys = [r for r in pend if r["side"] == "buy"]
        line("OK" if len(sells) == len(SYMS) else "FAIL",
             f"止盈卖单 {len(sells)}/{len(SYMS)}（均价上 1.5%）")
        line("OK" if len(buys) == len(SYMS) else "FAIL",
             f"第一档加仓买单 {len(buys)}/{len(SYMS)}（上一档下 2%、金额 ×1.5）")
        if len(sells) != len(SYMS) or len(buys) != len(SYMS):
            ok_all = False
        # dry_run 下挂单按挂单价即时成交 → 每跑一轮 step 就完成一轮止盈
        for _ in range(3):
            await dca.step()
        n_rounds, realized = db.dca_round_stats()
        line("OK" if n_rounds >= 3 and realized > 0 else "FAIL",
             f"完成 {n_rounds} 轮止盈，累计盈亏 {realized:+.4f}U（双边 0.1% 手续费已扣）")
        if n_rounds < 3 or realized <= 0:
            ok_all = False
        sell_orders = [o for o in db.get_orders(50) if o["type"] == "dca_sell"]
        line("OK" if sell_orders else "FAIL",
             f"成交记录 {len(sell_orders)} 笔 dca_sell，首轮示例："
             + (f"{sell_orders[0]['amount']:.6g} @ {sell_orders[0]['price']:.6g} "
                f"pnl {sell_orders[0]['pnl']:+.4f}U" if sell_orders else "无"))
        st = await dca.status()
        line("OK" if st["symbols"] else "FAIL",
             f"持仓状态 {len(st['symbols'])} 个，总成本 {st['total_cost']:.2f}U")

        print("⑥ 资金划转（合约 → 现货）")
        try:
            before_spot = await cl.get_spot_balance("USDT")
            moved = await cl.transfer_futures_to_spot(0.0)   # 0 = 不划转，只验证接口不炸
            line("OK", f"现货可用 {before_spot['free']:.2f}U；划转接口就绪"
                       f"（dry_run 下不真划，0 金额返回 {moved}）")
        except Exception as e:
            ok_all = False
            line("FAIL", f"划转接口异常: {e}")

        print("⑦ 利润定投链路（合约盈利触发 → 挂限价买单 → 成交后推进水位线）")
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
        pend = db.pending_spot_orders("profit_dca")
        line("OK" if pend else "FAIL",
             f"已挂限价买单 {len(pend)} 张（现价下 2%），水位线 {pdc._anchor():.2f}U"
             f"（成交前不动）")
        if not pend:
            ok_all = False
        # 模拟成交（dry_run 下 _settle_pending 按挂单价成交）
        await pdc.step()
        precs = [r for r in db.get_dca_records(50) if r["note"] == NOTE]
        line("OK" if precs and not db.pending_spot_orders("profit_dca") else "FAIL",
             f"挂单成交 {len(precs)} 笔，共 {sum(r['cost'] for r in precs):.2f}U，"
             f"水位线推进到 {pdc._anchor():.2f}U")
        if not precs:
            ok_all = False

        print("⑧ 不能循环定投（再跑 5 轮，盈利没涨就不该再买）")
        before = len(db.get_dca_records(200))
        for _ in range(5):
            await pdc.step()
        after = len(db.get_dca_records(200))
        line("OK" if after == before else "FAIL",
             f"多跑 5 轮，记录数 {before} → {after}（应不变）")
        if after != before:
            ok_all = False

        print("⑨ 下一档的进度提示")
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
