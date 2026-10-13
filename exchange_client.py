"""EvoClaw v2.0 — 交易所客户端。

借鉴 v1 的成熟做法：价格缓存 + 精度处理 + 最小名义值 + 错误码分类 + 取消安全。
v2 同时支持两个市场：
  - futures（binanceusdm）：方案B 低杠杆网格
  - spot（binance）      ：方案C 现货定投

dry_run=True 时，策略层不会调用下单方法；客户端仍可读行情/余额用于模拟。
"""

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt

log = logging.getLogger(__name__)


def _extract_code(err: str) -> str:
    m = re.search(r'"code":(-?\d+)', err)
    return m.group(1) if m else ""


class LogThrottle:
    """同一 key 在冷却窗口内只打一条日志。"""

    def __init__(self, cooldown: float = 300.0):
        self._cooldown = cooldown
        self._entries: dict[str, tuple[float, int]] = {}

    def emit(self, key: str) -> str | None:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = (now, 0)
            return ""
        last, suppressed = entry
        if now - last >= self._cooldown:
            self._entries[key] = (now, 0)
            return f" (suppressed {suppressed} in {self._cooldown:.0f}s)" if suppressed else ""
        self._entries[key] = (last, suppressed + 1)
        return None


_throttle = LogThrottle(cooldown=300)

_oid_seq = 0


def _client_oid() -> str:
    """给本程序下的普通单打一个可识别的 clientOrderId 前缀。

    币安成交明细里带 clientOrderId —— 有了它，对账时就能区分
    「我们挂的单成交了」和「用户在 App 里手工平的」，不会再把两者搞混。
    """
    global _oid_seq
    _oid_seq += 1
    return f"ec{int(time.time() * 1000) % 10**9}{_oid_seq % 1000:03d}"


class ExchangeClient:
    def __init__(self, config: dict):
        self.config = config
        self.dry_run = bool(config.get("dry_run", True))

        kwargs = dict(config.get("exchange_kwargs", {}))
        kwargs.setdefault("enableRateLimit", True)
        kwargs.setdefault("timeout", 15000)
        kwargs.setdefault("http_proxy", None)

        self.futures = ccxt.binanceusdm(dict(kwargs))
        self.futures.options["recvWindow"] = 60000
        self.futures.options["adjustForTimeDifference"] = True

        self._spot = None
        self._spot_kwargs = kwargs

        self._fut_markets: dict = {}
        self._spot_markets: dict = {}
        self._reverse: dict = {}          # ccxt unified symbol -> user symbol
        self._prices: dict = {}           # user symbol -> last price
        self._price_ts: dict = {}         # user symbol -> monotonic ts
        self._hl: dict = {}               # user symbol -> (24h high, 24h low)
        self._oo_cache: dict = {}         # (symbol, kind) -> (orders, monotonic ts)
        self._closed = False
        self.min_order_notional = float(config.get("min_order_notional", 5) or 5)
        self.notional_safety = float(config.get("order_notional_safety", 1.02) or 1.02)

    # ===== 基础设施 =====
    async def _safe_call(self, coro, timeout=20):
        if self._closed:
            raise RuntimeError("ExchangeClient already closed")
        return await asyncio.wait_for(coro, timeout=timeout)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for ex in (self.futures, self._spot):
            if ex is None:
                continue
            try:
                session = getattr(ex, "session", None)
                if session and hasattr(session, "close"):
                    await asyncio.wait_for(session.close(), timeout=3)
                await asyncio.wait_for(ex.close(), timeout=5)
            except Exception as e:
                log.warning(f"Exchange close warning: {e}")

    async def load_markets(self):
        markets = await self._safe_call(self.futures.load_markets(), timeout=25)
        for sym, m in markets.items():
            if m.get("swap") and m.get("quote") == "USDT":
                self._fut_markets[sym] = m
                self._register_reverse(sym)
        log.info(f"Futures markets loaded: {len(self._fut_markets)} USDT swaps")

    async def ensure_spot(self):
        """惰性加载现货市场（只有 DCA 模式才需要）。"""
        if self._spot is not None:
            return
        self._spot = ccxt.binance(dict(self._spot_kwargs))
        self._spot.options["recvWindow"] = 60000
        self._spot.options["adjustForTimeDifference"] = True
        markets = await self._safe_call(self._spot.load_markets(), timeout=25)
        for sym, m in markets.items():
            if m.get("spot") and m.get("quote") == "USDT":
                self._spot_markets[sym] = m
                self._register_reverse(sym)
        log.info(f"Spot markets loaded: {len(self._spot_markets)} USDT pairs")

    def _register_reverse(self, ccxt_sym: str):
        if ccxt_sym not in self._reverse:
            self._reverse[ccxt_sym] = ccxt_sym.replace("/", "").replace(":USDT", "")

    def user_symbol(self, ccxt_sym: str) -> str:
        return self._reverse.get(ccxt_sym, ccxt_sym)

    def to_ccxt(self, user_symbol: str, kind: str = "futures") -> str:
        """把 BTCUSDT / BTC/USDT 统一成 ccxt 的 `BTC/USDT:USDT`（合约）或 `BTC/USDT`（现货）。"""
        s = user_symbol.replace("/", "").replace(":USDT", "")
        base = s[:-4] if s.endswith("USDT") else s
        if kind == "futures":
            return f"{base}/USDT:USDT"
        return f"{base}/USDT"

    def market_info(self, user_symbol: str, kind: str = "futures") -> dict:
        table = self._fut_markets if kind == "futures" else self._spot_markets
        return table.get(self.to_ccxt(user_symbol, kind), {})

    # ===== 价格 =====
    async def refresh_prices(self, symbols: list[str]):
        """刷新价格缓存；失败会抛异常（由引擎决定是否降级）。"""
        resolved = [self.to_ccxt(s, "futures") for s in symbols]
        resolved = [s for s in resolved if s in self._fut_markets]
        if not resolved:
            return
        tickers = await self._safe_call(self.futures.fetch_tickers(resolved), timeout=20)
        ts = time.monotonic()
        for sym, t in tickers.items():
            if t and t.get("last"):
                us = self.user_symbol(sym)
                self._prices[us] = float(t["last"])
                self._price_ts[us] = ts
                self._store_hl(us, t)

    def _store_hl(self, user_symbol: str, t: dict):
        try:
            hi = float(t.get("high") or 0)
            lo = float(t.get("low") or 0)
            if hi > 0 and lo > 0 and hi > lo:
                self._hl[user_symbol] = (hi, lo)
        except (TypeError, ValueError):
            pass

    def range_pos(self, user_symbol: str) -> float | None:
        """现价在 24h 区间中的位置：0=最低点，1=最高点；无数据返回 None。

        用来判断「现在是高位还是低位」——选币时振幅已经很大，但程序并不知道
        现价处在当日波动的顶部还是底部，这个指标就是那个代理量。
        """
        hl = self._hl.get(user_symbol)
        p = self._prices.get(user_symbol)
        if not hl or not p or p <= 0:
            return None
        hi, lo = hl
        if hi <= lo:
            return None
        return max(0.0, min(1.0, (p - lo) / (hi - lo)))

    async def ensure_price(self, user_symbol: str, max_age: float = 30.0) -> float:
        now = time.monotonic()
        p = self._prices.get(user_symbol)
        if p and p > 0 and (now - self._price_ts.get(user_symbol, 0)) < max_age:
            return float(p)
        ccxt_sym = self.to_ccxt(user_symbol, "futures")
        try:
            t = await self._safe_call(self.futures.fetch_ticker(ccxt_sym), timeout=10)
            last = float(t.get("last", 0) or 0)
            if last > 0:
                self._prices[user_symbol] = last
                self._price_ts[user_symbol] = now
                self._store_hl(user_symbol, t)
                return last
        except Exception as e:
            log.warning(f"ensure_price {user_symbol} failed: {e}")
        return float(self._prices.get(user_symbol, 0) or 0)

    def last_price(self, user_symbol: str) -> float:
        return float(self._prices.get(user_symbol, 0) or 0)

    # ===== 精度与最小下单量 =====
    def min_notional_of(self, user_symbol: str, kind: str = "futures") -> float:
        """该标的「一手」至少需要多少 USDT（交易所最小名义值 vs 配置最小额的较大者）。"""
        m = self.market_info(user_symbol, kind)
        if not m:
            return self.min_order_notional
        ex_min = float((m.get("limits", {}).get("cost", {}).get("min", 0)) or 0)
        return max(ex_min, self.min_order_notional)

    def min_amount(self, user_symbol: str, price: float, kind: str = "futures") -> float:
        """满足最小名义值与最小数量精度所需的最小可下数量；算不出（资金/精度不足）返回 0。"""
        m = self.market_info(user_symbol, kind)
        if not m or price <= 0:
            return 0.0
        ex = self.futures if kind == "futures" else self._spot
        try:
            cs = float(m.get("contractSize", 1) or 1)
            target = self.min_notional_of(user_symbol, kind) * self.notional_safety
            min_amt = float((m.get("limits", {}).get("amount", {}).get("min", 0)) or 0)
            raw = max(1.0, math.ceil(target / (price * cs)))
            if min_amt > 0:
                raw = max(raw, math.ceil(min_amt / cs))
            return float(ex.amount_to_precision(self.to_ccxt(user_symbol, kind), raw))
        except Exception:
            return 0.0

    def amount_for_notional(self, user_symbol: str, usdt: float, price: float,
                            kind: str = "futures") -> float:
        """按目标名义金额算数量（不低于最小可下量）；该金额买不起「一手」时返回 0。

        返回 0 的典型原因：目标金额低于该标的的最小名义值（如 BTC 一手 ≈ 82U）。
        这种情况由调用方汇总成一条清晰的「资金不足」提示，不在这里逐档刷屏。
        """
        m = self.market_info(user_symbol, kind)
        if not m or price <= 0:
            return 0.0
        ex = self.futures if kind == "futures" else self._spot
        try:
            cs = float(m.get("contractSize", 1) or 1)
            lo = self.min_amount(user_symbol, price, kind)
            if lo <= 0:
                return 0.0
            raw = usdt / (price * cs)
            try:
                amt = float(ex.amount_to_precision(self.to_ccxt(user_symbol, kind), raw))
            except Exception:
                return 0.0
            if amt <= 0:
                return 0.0
            return max(amt, lo)
        except Exception:
            return 0.0

    def round_price(self, user_symbol: str, price: float, kind: str = "futures") -> float:
        ex = self.futures if kind == "futures" else self._spot
        try:
            return float(ex.price_to_precision(self.to_ccxt(user_symbol, kind), price))
        except Exception:
            return price

    # ===== 账户 =====
    async def get_futures_balance(self) -> dict:
        b = await self._safe_call(self.futures.fetch_balance(), timeout=15)
        return {
            "balance": float(b.get("total", {}).get("USDT", 0) or 0),
            "available": float(b.get("free", {}).get("USDT", 0) or 0),
        }

    async def get_spot_balance(self, asset: str = "USDT") -> dict:
        await self.ensure_spot()
        b = await self._safe_call(self._spot.fetch_balance(), timeout=15)
        return {
            "free": float(b.get("free", {}).get(asset, 0) or 0),
            "total": float(b.get("total", {}).get(asset, 0) or 0),
        }

    async def get_positions(self) -> list[dict]:
        """合约持仓（仅数量>0 的）。"""
        rows = await self._safe_call(self.futures.fetch_positions(), timeout=20)
        out = []
        for p in rows:
            c = float(p.get("contracts", 0) or 0)
            if c > 0:
                out.append(p)
        return out

    # ===== 交易 =====
    async def set_margin_mode(self, user_symbol: str, mode: str = "isolated"):
        """逐仓：单仓爆仓只损失该仓保证金，不会牵连整个账户。"""
        if self.dry_run:
            return
        try:
            await self._safe_call(
                self.futures.set_margin_mode(mode, self.to_ccxt(user_symbol, "futures")),
                timeout=10)
        except Exception as e:
            # 已是该模式时交易所会报错，属正常
            if "-4046" not in str(e) and "No need to change" not in str(e):
                log.warning(f"set_margin_mode {user_symbol} {mode} failed: {e}")

    async def set_leverage(self, user_symbol: str, leverage: int):
        if self.dry_run:
            return
        try:
            await self._safe_call(
                self.futures.set_leverage(int(leverage), self.to_ccxt(user_symbol, "futures")),
                timeout=10)
        except Exception as e:
            log.warning(f"set_leverage {user_symbol} x{leverage} failed: {e}")

    async def create_limit_order(self, user_symbol: str, side: str, amount: float,
                                 price: float, kind: str = "futures",
                                 reduce_only: bool = False,
                                 position_side: str = "LONG") -> dict | None:
        """限价单。dry_run 时返回模拟成交结构（不触网）。

        本账户为**对冲模式**，合约单必须带 positionSide，否则报 -4061。
        网格是「只做多」：所有单 positionSide=LONG（SELL 即平多）。
        """
        if self.dry_run:
            return {"order_id": f"dry-{int(time.time()*1000)}", "price": price,
                    "amount": amount, "dry_run": True}
        ex = self.futures if kind == "futures" else self._spot
        params = {}
        if kind == "futures":
            params["positionSide"] = position_side.upper()
            params["newClientOrderId"] = _client_oid()      # 标记「这单是本程序下的」
        if kind == "futures" and reduce_only:
            params["reduceOnly"] = True
        try:
            o = await self._safe_call(ex.create_order(
                self.to_ccxt(user_symbol, kind), "limit", side, amount, price, params), timeout=20)
            return {
                "order_id": str(o.get("id", "")),
                "price": float(o.get("price") or price),
                "amount": float(o.get("amount") or amount),
                "dry_run": False,
            }
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"limit_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Limit order failed {user_symbol} {side} {amount}@{price}: {e}{s}")
            return None

    async def create_market_order(self, user_symbol: str, side: str, amount: float,
                                  kind: str = "futures",
                                  position_side: str = "LONG") -> dict | None:
        if self.dry_run:
            px = self.last_price(user_symbol) or await self.ensure_price(user_symbol)
            return {"order_id": f"dry-{int(time.time()*1000)}", "price": px,
                    "amount": amount, "dry_run": True}
        ex = self.futures if kind == "futures" else self._spot
        params = ({"positionSide": position_side.upper(),
                   "newClientOrderId": _client_oid()} if kind == "futures" else {})
        try:
            # 注意 ccxt 签名：create_order(symbol, type, side, amount, price, params)
            # 市价单 price 必须传 None，否则 params 会被当成 price 丢弃 → -4061
            o = await self._safe_call(ex.create_order(
                self.to_ccxt(user_symbol, kind), "market", side, amount, None, params), timeout=20)
            px = float(o.get("average") or o.get("price") or 0 or 0)
            if px <= 0:
                px = await self.ensure_price(user_symbol)
            return {"order_id": str(o.get("id", "")), "price": px,
                    "amount": float(o.get("amount") or amount), "dry_run": False}
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"market_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Market order failed {user_symbol} {side} {amount}: {e}{s}")
            return None

    async def create_stop_order(self, user_symbol: str, side: str, amount: float,
                                stop_price: float, position_side: str = "SHORT",
                                kind: str = "futures") -> dict | None:
        """条件市价单（止损/止盈触发）。ccxt 会把 stopLossPrice 映射成 STOP_MARKET。

        返回 None 表示不支持或下单失败 —— 调用方必须自行准备软件兜底，不能假设
        交易所一定挂上了止损。
        """
        if self.dry_run:
            return {"order_id": f"dry-stop-{int(time.time()*1000)}", "dry_run": True}
        try:
            o = await self._safe_call(self.futures.create_order(
                self.to_ccxt(user_symbol, "futures"), "market", side, amount, None,
                {"stopLossPrice": stop_price, "positionSide": position_side.upper()}), timeout=20)
            return {"order_id": str(o.get("id", "")), "dry_run": False}
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"stop_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Stop order failed {user_symbol} {side} {amount}@{stop_price}: {e}{s}")
            return None

    async def fetch_my_trades(self, user_symbol: str, since_ms: int = None,
                              limit: int = 100) -> list[dict]:
        """该标的的成交明细（用于还原外部/手工平仓的真实成交价）。"""
        try:
            return await self._safe_call(self.futures.fetch_my_trades(
                self.to_ccxt(user_symbol, "futures"), since_ms, limit), timeout=20)
        except Exception as e:
            log.warning(f"fetch_my_trades {user_symbol} failed: {e}")
            return []

    async def fetch_open_algo_orders(self, user_symbol: str) -> list[dict]:
        """条件单（止损/止盈）走的是币安「算法单」接口，**不在** fetch_open_orders 里。"""
        if self.dry_run:
            return []
        try:
            rows = await self._safe_call(
                self.futures.fapiPrivateGetOpenAlgoOrders({}), timeout=15)
        except Exception as e:
            log.warning(f"fetch open algo orders failed: {e}")
            return []
        return [o for o in (rows or []) if o.get("symbol") == user_symbol]

    async def cancel_algo_orders(self, user_symbol: str) -> int:
        """撤销该标的的全部算法单（条件单）。普通 cancel_order 撤不掉它们。"""
        if self.dry_run:
            return 0
        n = 0
        for o in await self.fetch_open_algo_orders(user_symbol):
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            try:
                await self._safe_call(
                    self.futures.fapiPrivateDeleteAlgoOrder({"algoId": algo_id}), timeout=15)
                n += 1
            except Exception as e:
                log.warning(f"cancel algo order {algo_id} ({user_symbol}) failed: {e}")
        return n

    async def cancel_order(self, order_id: str, user_symbol: str, kind: str = "futures") -> bool:
        if self.dry_run or not order_id or order_id.startswith("dry-"):
            return True
        ex = self.futures if kind == "futures" else self._spot
        try:
            await self._safe_call(ex.cancel_order(order_id, self.to_ccxt(user_symbol, kind)), timeout=15)
            return True
        except Exception as e:
            log.warning(f"Cancel order failed {user_symbol} {order_id}: {e}")
            return False

    async def fetch_order_status(self, order_id: str, user_symbol: str,
                                 kind: str = "futures") -> str:
        """单笔订单状态：open / closed / canceled / expired / unknown。"""
        if not order_id or order_id.startswith("dry-"):
            return "open"
        ex = self.futures if kind == "futures" else self._spot
        try:
            o = await self._safe_call(ex.fetch_order(order_id, self.to_ccxt(user_symbol, kind)), timeout=15)
            return str(o.get("status") or "unknown")
        except Exception:
            return "unknown"

    async def fetch_open_orders(self, user_symbol: str, kind: str = "futures") -> list[dict]:
        # 短 TTL 缓存：引擎 tick 和 Web status() 都会拉挂单，8 秒内共享一次请求，
        # 让「已挂/待挂」的展示以交易所为准而不至于打爆限频。
        key = (user_symbol, kind)
        now = time.monotonic()
        hit = self._oo_cache.get(key)
        if hit and (now - hit[1]) < 8.0:
            return hit[0]
        ex = self.futures if kind == "futures" else self._spot
        try:
            rows = await self._safe_call(ex.fetch_open_orders(self.to_ccxt(user_symbol, kind)), timeout=15)
        except Exception as e:
            log.warning(f"fetch_open_orders {user_symbol} failed: {e}")
            return hit[0] if hit else []
        self._oo_cache[key] = (rows, now)
        return rows
