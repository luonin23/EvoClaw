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
                              max_symbols: int = 3,
                              budget_notional: float = 0.0) -> list[dict]:
    """返回按得分排序的候选列表，每项含 symbol/price/range/volume/score。

    budget_notional：单个标的能承受的**名义价值**上限（= 权益 × 敞口上限 / 名额）。
    最小一手就超过它的标的直接排除 —— 否则会选出一个「根本买不起」的币（实测
    ZECUSDT 一手 = 1 张 = 1089U，而我们总共只有 30U）：它长期霸占 top1，还会
    因为「排在前面却挂不上资金」把后面所有标的的建仓单反复撤掉。
    """
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
    too_big: list[str] = []
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
        # 最小一手的**名义价值**必须在本标的的预算之内。只判断「数量>0」不够：
        # 交易所会按最小变动单位向上取整，1089U 的币照样返回「1 张」。
        if budget_notional > 0:
            lot = client.min_amount(us, close, "futures")
            if lot <= 0 or lot * close > budget_notional:
                too_big.append(f"{us}(一手{lot*close:.0f}U>"f"{budget_notional:.0f}U)")
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
    if too_big:
        log.info(f"选币：排除 {len(too_big)} 个「一手就超预算」的标的（共 {len(out)} 个可用）："
                 + ", ".join(too_big[:6]) + ("…" if len(too_big) > 6 else ""))
    if picked:
        log.info("选币结果: " + ", ".join(
            f"{p['symbol']}(振幅{p['range']*100:.1f}%,量{p['quote_volume']/1e8:.1f}亿)" for p in picked))
    else:
        log.warning(f"选币：无合格标的（量≥{min_vol/1e8:.0f}亿 振幅{min_rng*100:.0f}~{max_rng*100:.0f}% 单格{size_usdt}U）")
    return picked


async def qualify_symbol(client, cfg: dict, symbol: str) -> tuple[bool, str]:
    """下单前复核：该标的**当前**是否仍满足入选条件。

    选币每 6 小时才刷新一次；补仓（新开敞口）前用单标的 ticker 再核一次，
    避免在流动性枯竭 / 状态异常时继续加仓。开销很小（一次 ticker 调用）。
    拿不到数据时返回 True（不误伤），由后续下单逻辑兜底。
    """
    cfg = cfg or {}
    try:
        ccxt_sym = client.to_ccxt(symbol, "futures")
        t = await client._safe_call(client.futures.fetch_ticker(ccxt_sym), timeout=10)
    except Exception:
        return True, ""
    m = client.market_info(symbol, "futures")
    info = m.get("info", {}) or {}
    if info.get("contractType") != "PERPETUAL":
        return False, "非永续合约"
    if info.get("status") != "TRADING":
        return False, f"状态异常({info.get('status')})"
    qvol = float(t.get("quoteVolume") or 0)
    min_vol = float(cfg.get("min_quote_volume", 100_000_000) or 0)
    if min_vol > 0 and qvol < min_vol:
        return False, f"成交额 {qvol/1e8:.2f}亿 < {min_vol/1e8:.0f}亿"
    close = float(t.get("close") or t.get("last") or 0)
    high = float(t.get("high") or 0)
    low = float(t.get("low") or 0)
    if close > 0 and high > 0 and low > 0:
        rng = (high - low) / close
        lo = float(cfg.get("min_daily_range", 0.02) or 0)
        hi = float(cfg.get("max_daily_range", 0.20) or 1)
        if rng < lo:
            return False, f"振幅 {rng*100:.1f}% 低于 {lo*100:.0f}%"
        if rng > hi:
            return False, f"振幅 {rng*100:.1f}% 高于 {hi*100:.0f}%"
    return True, ""
