"""自动选币（方案B 网格专用）。

筛选逻辑（全部条件同时满足才算合格）：
  1. U 本位**永续**合约（排除交割合约、黄金/股票类 TradFi 永续）
  2. 状态为 TRADING（排除结算中/下架中）
  3. 24h 成交额 ≥ min_quote_volume（保证挂单能成交、滑点小）
  4. 24h 振幅在 [min_daily_range, max_daily_range]（太小没差价可吃，太大容易单边穿区）
  5. 按 size_usdt 能买得起「一手」（低价币才布得起多档网格）
  6. 不在黑名单（稳定币等）

排序：振幅 × 流动性权重 —— 既要有波动，又要有深度。

**掉出名单的币不会被抛弃**：由 grid 策略转入 draining（只卖不买），卖光后退役。
"""

import logging
import math

log = logging.getLogger(__name__)

# 稳定币 / 不愿参与网格的标的
DEFAULT_BLACKLIST = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT",
    "EURUSDT", "AEURUSDT", "USDPUSDT", "EURIUSDT",
}


def _user_symbol(ccxt_sym: str) -> str:
    return ccxt_sym.replace("/", "").replace(":USDT", "")


async def select_grid_symbols(client, cfg: dict, size_usdt: float,
                              max_symbols: int = 3) -> list[dict]:
    """返回按得分排序的候选列表，每项含 symbol/price/range/volume/score。"""
    cfg = cfg or {}
    min_vol = float(cfg.get("min_quote_volume", 100_000_000) or 0)
    min_rng = float(cfg.get("min_daily_range", 0.02) or 0)
    max_rng = float(cfg.get("max_daily_range", 0.20) or 1.0)
    blacklist = set(cfg.get("blacklist", []) or []) | DEFAULT_BLACKLIST

    try:
        tickers = await client._safe_call(client.futures.fetch_tickers(), timeout=25)
    except Exception as e:
        log.warning(f"选币：行情拉取失败: {e}")
        return []

    out = []
    for ccxt_sym, t in tickers.items():
        m = client._fut_markets.get(ccxt_sym)
        if not m:
            continue
        info = m.get("info", {}) or {}
        if info.get("contractType") != "PERPETUAL":
            continue
        if info.get("status") != "TRADING":
            continue
        us = _user_symbol(ccxt_sym)
        if us in blacklist:
            continue

        try:
            close = float(t.get("close") or t.get("last") or 0)
            high = float(t.get("high") or 0)
            low = float(t.get("low") or 0)
            qvol = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0 or high <= 0 or low <= 0:
            continue
        if qvol < min_vol:
            continue

        rng = (high - low) / close          # 24h 振幅
        if rng < min_rng or rng > max_rng:
            continue

        # 能否买得起「一手」——决定小资金能不能布多档
        if size_usdt > 0 and client.amount_for_notional(us, size_usdt, close, "futures") <= 0:
            continue

        # 得分：振幅为主，流动性加权（成交量越大越可信）
        liq_w = 0.5 + 0.5 * min(1.0, math.log10(max(qvol, 1)) / 10.0)
        score = rng * liq_w
        out.append({
            "symbol": us, "price": close, "range": round(rng, 4),
            "quote_volume": round(qvol, 0), "score": round(score, 6),
        })

    out.sort(key=lambda x: -x["score"])
    picked = out[:max(1, max_symbols)]
    if picked:
        log.info("选币结果: " + ", ".join(
            f"{p['symbol']}(振幅{p['range']*100:.1f}%,量{p['quote_volume']/1e8:.1f}亿)" for p in picked))
    else:
        log.warning(f"选币：无合格标的（量≥{min_vol/1e8:.0f}亿 振幅{min_rng*100:.0f}~{max_rng*100:.0f}% 单格{size_usdt}U）")
    return picked
