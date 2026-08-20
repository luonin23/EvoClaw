import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone, timedelta
import ccxt.async_support as ccxt

log = logging.getLogger(__name__)


def _extract_code(err: str) -> str:
    """Extract Binance error code from exception string, e.g. '-2019'."""
    m = re.search(r'"code":(-?\d+)', err)
    return m.group(1) if m else ""


class LogThrottle:
    """Suppress duplicate log messages within a cooldown window.

    First occurrence of a key is logged normally.  Subsequent occurrences
    within *cooldown* seconds are silently counted.  When the cooldown
    expires the next occurrence is logged with a suppressed-count suffix.
    """

    def __init__(self, cooldown: float = 30.0):
        self._cooldown = cooldown
        self._entries: dict[str, tuple[float, int]] = {}  # key -> (last_emit, suppressed)

    def emit(self, key: str) -> str | None:
        """Return None to suppress, or a string suffix (``""`` = no suffix)."""
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = (now, 0)
            return ""
        last_emit, suppressed = entry
        if now - last_emit >= self._cooldown:
            self._entries[key] = (now, 0)
            if suppressed > 0:
                return f" (suppressed {suppressed} in {self._cooldown:.0f}s)"
            return ""
        self._entries[key] = (last_emit, suppressed + 1)
        return None


# Module-level throttles — each throttles independently
_throttle_error = LogThrottle(cooldown=600)   # for ERROR-level repeats (10 min)
_throttle_warn = LogThrottle(cooldown=600)    # for WARNING-level repeats (10 min)


class ExchangeClient:
    def __init__(self, config: dict):
        exchange_class = getattr(ccxt, "binanceusdm")
        kwargs = config.get("exchange_kwargs", {}).copy()
        kwargs.setdefault("enableRateLimit", True)
        kwargs.setdefault("timeout", 10000)
        # Close any stale aiohttp sessions inside ccxt (prevents Unclosed connector)
        kwargs.setdefault("http_proxy", None)
        self.exchange = exchange_class(kwargs)
        # Binance API clock tolerance:
        # - recvWindow 60000ms: tolerate up to 60s of clock drift per request
        # - adjustForTimeDifference: ccxt auto-syncs local timestamps with exchange server time
        self.exchange.options['recvWindow'] = 60000
        self.exchange.options['adjustForTimeDifference'] = True
        self.market_info = {}
        self.symbol_map = {}
        self._reverse_map = {}
        self._prices = {}
        self._closed = False
        # Track position-limit blocks to avoid wasted API calls
        self._blocked_until: dict[str, float] = {}
        self._block_count: dict[str, int] = {}

    def _mark_blocked(self, key: str) -> None:
        """Mark a symbol+side as blocked due to -2027, with escalating cooldown."""
        self._block_count[key] = self._block_count.get(key, 0) + 1
        count = self._block_count[key]
        durations = [60, 300, 900, 1800, 3600]  # 1min, 5min, 15min, 30min, 1hr
        cooldown = durations[min(count - 1, len(durations) - 1)]
        self._blocked_until[key] = time.monotonic() + cooldown

    def is_position_blocked(self, symbol: str, side: str) -> bool:
        """Check if a symbol+side is in cooldown after -2027 errors."""
        key = f"{symbol}:{side}"
        until = self._blocked_until.get(key, 0)
        if time.monotonic() < until:
            return True
        return False

    async def _safe_call(self, coro, timeout=10):
        """Wrap ccxt call with hard timeout to prevent event loop blocking."""
        if self._closed:
            raise RuntimeError("ExchangeClient already closed")
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("CCXT call timed out")
            raise
        except Exception:
            raise

    async def close(self):
        """Close the ccxt exchange and release all aiohttp connections."""
        if self._closed:
            return
        self._closed = True
        try:
            # Close the underlying aiohttp session first to prevent leaks
            session = getattr(self.exchange, 'session', None)
            if session and hasattr(session, 'close'):
                await asyncio.wait_for(session.close(), timeout=3)
            await asyncio.wait_for(self.exchange.close(), timeout=5)
            log.info("Exchange client closed cleanly")
        except Exception as e:
            log.warning(f"Exchange close warning: {e}")

    async def load_markets(self):
        markets = await self._safe_call(self.exchange.load_markets(), timeout=20)
        for symbol in self.exchange.symbols:
            m = markets.get(symbol)
            if m and m.get("swap") and m.get("quote") == "USDT":
                self.market_info[symbol] = m
                exchange_id = m.get("id", "")
                if exchange_id and exchange_id not in self.symbol_map:
                    self.symbol_map[exchange_id] = symbol
                base = symbol.replace(":", "").replace("/", "")
                if base and base not in self.symbol_map:
                    self.symbol_map[base] = symbol
                eid = m.get("id", "")
                self._reverse_map[symbol] = eid if eid else base
        log.info(f"Loaded {len(self.market_info)} USDT swap markets, {len(self.symbol_map)} aliases")

    async def refresh_prices(self, symbols: list):
        resolved = [self.resolve_symbol(s) for s in symbols]
        try:
            tickers = await self._safe_call(self.exchange.fetch_tickers(resolved), timeout=15)
            for sym, ticker in tickers.items():
                if ticker and ticker.get("last"):
                    self._prices[sym] = float(ticker["last"])
                    if sym in self.market_info:
                        self.market_info[sym]["info"]["lastPrice"] = str(ticker["last"])
        except Exception as e:
            log.warning(f"refresh_prices failed: {e}")

    async def get_candidate_symbols(self, volume_threshold: float, price_threshold: float) -> list[str]:
        try:
            tickers = await self._safe_call(self.exchange.fetch_tickers(), timeout=15)
            candidates = []
            for ccxt_sym, ticker in tickers.items():
                if ccxt_sym not in self.market_info:
                    continue
                last = float(ticker.get("last", 0) or 0)
                volume = float(ticker.get("quoteVolume", 0) or 0)
                price_ok = price_threshold <= 0 or last <= price_threshold
                volume_ok = volume_threshold <= 0 or volume >= volume_threshold
                if price_ok and volume_ok:
                    user_sym = self.user_symbol(ccxt_sym)
                    if user_sym:
                        candidates.append(user_sym)
            return candidates
        except Exception as e:
            log.error(f"get_candidate_symbols failed: {e}")
            return []

    def resolve_symbol(self, symbol: str) -> str:
        if symbol in self.market_info:
            return symbol
        if symbol in self.symbol_map:
            return self.symbol_map[symbol]
        log.warning(f"Symbol {symbol} not found in swap markets")
        return symbol

    def user_symbol(self, ccxt_symbol: str) -> str:
        if ccxt_symbol in self._reverse_map:
            return self._reverse_map[ccxt_symbol]
        return ccxt_symbol

    def get_market_info(self, symbol: str) -> dict:
        resolved = self.resolve_symbol(symbol)
        return self.market_info.get(resolved, {})

    def should_stop_replenish(self, sym: str, side: str, stop_threshold: float, position_map: dict) -> bool:
        if stop_threshold <= 0:
            return False
        opposite_side = "short" if side == "long" else "long"
        opposite_pos = position_map.get(f"{sym}:{opposite_side}")
        if not opposite_pos:
            return False
        entry_price = float(opposite_pos.get("entryPrice", 0) or 0)
        if entry_price <= 0:
            return False
        resolved = self.resolve_symbol(sym)
        price = self._prices.get(resolved)
        if not price or price <= 0:
            market = self.get_market_info(sym)
            price_str = market.get("info", {}).get("lastPrice")
            if price_str:
                price = float(price_str)
        if not price or price <= 0:
            log.warning(f"REPLENISH STOP {sym} {side}: price unavailable, defaulting to STOP")
            return True
        deviation = abs(entry_price - price) / entry_price
        if deviation >= stop_threshold:
            log.info(
                f"REPLENISH STOP {sym} {side}: opposite {opposite_side} "
                f"entry={entry_price:.6f} price={price:.6f} deviation={deviation:.4%}"
            )
            return True
        return False

    async def get_balance(self) -> dict:
        balance = await self._safe_call(self.exchange.fetch_balance(), timeout=10)
        usdt_total = balance.get("total", {}).get("USDT", 0)
        usdt_free = balance.get("free", {}).get("USDT", 0)
        return {
            "balance": float(usdt_total) if usdt_total else 0,
            "available_balance": float(usdt_free) if usdt_free else 0,
        }

    async def fetch_liquidations(self, since_minutes: int = 1440, with_pnl: bool = False) -> list[dict]:
        """Fetch liquidation (force order) history from Binance.

        If with_pnl=True, also fetches all user trades in the window and matches
        realized PnL to each liquidation record.

        Returns list of dicts with: symbol, side, origQty, avgPrice, executedQty, pnl, time.
        """
        try:
            since_ms = int((datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).timestamp() * 1000)
            raw = await self._safe_call(
                self.exchange.fapiPrivateGetForceOrders({"startTime": since_ms, "limit": 100}),
                timeout=20,
            )
            result = []
            for fo in raw:
                sym_id = fo.get("symbol", "")
                sym = self.user_symbol(sym_id)
                side = "LONG" if fo.get("positionSide", "") == "LONG" else "SHORT"
                raw_time = fo.get("time", 0)
                if isinstance(raw_time, str):
                    raw_time = int(raw_time)
                result.append({
                    "symbol": sym,
                    "side": side,
                    "origQty": float(fo.get("origQty", 0)),
                    "avgPrice": float(fo.get("price", 0) or fo.get("averagePrice", 0) or 0),
                    "executedQty": float(fo.get("executedQty", 0)),
                    "pnl": 0.0,
                    "time": datetime.fromtimestamp(raw_time / 1000, tz=timezone.utc).isoformat(),
                    "timestamp_ms": raw_time,
                    "order_id": fo.get("orderId"),
                })

            # Optionally enrich with PnL from all trades in one batch call
            if with_pnl and result:
                try:
                    all_trades = await self._safe_call(
                        self.exchange.fapiPrivateGetUserTrades({"startTime": since_ms, "limit": 1000}),
                        timeout=20,
                    )
                    # Build lookup: (symbol, orderId) -> total realized PnL
                    trade_pnl: dict[tuple, float] = {}
                    for t in all_trades:
                        sym_id = t.get("symbol", "")
                        sym = self.user_symbol(sym_id)
                        oid = str(t.get("orderId", ""))
                        rpnl = float(t.get("realizedPnl", 0) or 0)
                        key = (sym, oid)
                        trade_pnl[key] = trade_pnl.get(key, 0) + rpnl

                    for r in result:
                        matched = trade_pnl.get((r["symbol"], str(r.get("order_id", ""))))
                        if matched is not None:
                            r["pnl"] = matched
                except Exception as e:
                    log.warning(f"PnL enrichment failed: {e}")

            # Clean up internal fields before returning
            for r in result:
                r.pop("order_id", None)
            return result
        except Exception as e:
            log.warning(f"fetch_liquidations failed: {e}")
            return []

    async def get_positions(self, symbols: list = None) -> list[dict]:
        resolved = [self.resolve_symbol(s) for s in symbols] if symbols else None
        positions = await self._safe_call(self.exchange.fetch_positions(resolved or None), timeout=15)
        result = []
        for p in positions:
            contracts = float(p.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            result.append(p)
        return result

    def calc_min_contracts(self, symbol: str) -> float:
        try:
            resolved = self.resolve_symbol(symbol)
            market = self.market_info.get(resolved)
            if not market:
                return 1
            contract_size = float(market.get("contractSize", 1) or 1)
            min_notional = float((market.get("limits", {}).get("cost", {}).get("min", 0)) or 0)
            # Binance USDS-M hard floor: minimum notional is $5.0
            if min_notional <= 0:
                min_notional = 5.0
            min_amount = float((market.get("limits", {}).get("amount", {}).get("min", 0)) or 0)
            amount_precision = int(float(market.get("precision", {}).get("amount", 0) or 0))

            raw = 1
            if min_amount > 0:
                raw = max(raw, int(math.ceil(min_amount / contract_size)))
            if min_notional > 0:
                price = self._prices.get(resolved)
                if not price or price <= 0:
                    price_str = market.get("info", {}).get("lastPrice") or market.get("last", None)
                    if price_str:
                        price = float(price_str)
                if not price or price <= 0:
                    price = min_notional / (contract_size * 10) if contract_size > 0 else 1
                raw = max(raw, int(math.ceil(min_notional / (price * contract_size))))
            return float(round(raw, amount_precision))
        except Exception as e:
            log.error(f"calc_min_contracts failed {symbol}: {e}")
            return 1

    async def open_position(self, symbol: str, side: str) -> dict | None:
        resolved = self.resolve_symbol(symbol)
        if self.is_position_blocked(resolved, side):
            return None
        amount = self.calc_min_contracts(resolved)
        if amount <= 0:
            return None
        # Quick balance check — if available < 5 USDT (minimum notional), skip silently
        try:
            bal = await self.get_balance()
            if bal.get('available_balance', 0) < 5.0:
                return None
        except Exception:
            pass  # if balance check itself fails, try the actual order
        try:
            order = await self._safe_call(self.exchange.create_order(
                symbol=resolved, type="market", side=side, amount=amount,
                params={"positionSide": "LONG" if side == "buy" else "SHORT"},
            ), timeout=15)
            log.info(f"Open {side} {resolved} {amount} -> {order.get('id')}")
            avg = order.get("average") or order.get("price")
            if not avg:
                info = order.get("info", {})
                avg = info.get("avgPrice") or info.get("averagePrice")
            if not avg:
                cost = order.get("cost")
                filled = order.get("filled")
                if cost and filled and filled > 0:
                    avg = cost / filled
            if not avg:
                # Fallback to lastPrice from market info cache
                market = self.market_info.get(resolved, {})
                price_str = market.get("info", {}).get("lastPrice", "0")
                if price_str and float(price_str) > 0:
                    avg = float(price_str)
            if not avg:
                # Last resort: fetch live ticker
                try:
                    ticker = await self._safe_call(self.exchange.fetch_ticker(resolved), timeout=10)
                    last = float(ticker.get("last", 0) or 0)
                    if last > 0:
                        avg = last
                except Exception:
                    pass
            if not avg:
                avg = self._prices.get(resolved, 0)
            return {
                "order_id": str(order.get("id", "")),
                "average": float(avg or 0),
                "amount": amount,
            }
        except Exception as e:
            err = str(e)
            if "-2027" in err or "exceeded" in err.lower():
                self._mark_blocked(f"{resolved}:{side}")
                key = f"open_blocked:{resolved}:{side}"
                s = _throttle_warn.emit(key)
                if s is not None:
                    log.warning(f"Open position blocked (max position) {resolved} {side}: {e}{s}")
            elif "-2019" in err or "insufficient" in err.lower():
                pass
            else:
                key = f"open_err:{resolved}:{side}:{_extract_code(err)}"
                s = _throttle_error.emit(key)
                if s is not None:
                    log.error(f"Open position failed {resolved} {side}: {e}{s}")
            return None

    async def safe_open(self, symbol: str, side: str) -> dict | None:
        """Open position once. No retries — tick loop is the retry mechanism."""
        return await self.open_position(symbol, side)

    async def add_position(self, symbol: str, side: str, amount: float) -> dict | None:
        resolved = self.resolve_symbol(symbol)
        if self.is_position_blocked(resolved, side):
            return None
        if amount <= 0:
            return None
        # Quick balance check
        try:
            bal = await self.get_balance()
            if bal.get('available_balance', 0) < 5.0:
                return None
        except Exception:
            pass
        try:
            open_side = "buy" if side == "long" else "sell"
            order = await self._safe_call(self.exchange.create_order(
                symbol=resolved, type="market", side=open_side, amount=amount,
                params={"positionSide": side.upper()},
            ), timeout=15)
            log.info(f"Add {side} {resolved} {amount} -> {order.get('id')}")
            avg = order.get("average") or order.get("price")
            if not avg:
                info = order.get("info", {})
                avg = info.get("avgPrice") or info.get("averagePrice")
            if not avg:
                cost = order.get("cost")
                filled = order.get("filled")
                if cost and filled and filled > 0:
                    avg = cost / filled
            if not avg:
                # Fallback to lastPrice from market info cache
                market = self.market_info.get(resolved, {})
                price_str = market.get("info", {}).get("lastPrice", "0")
                if price_str and float(price_str) > 0:
                    avg = float(price_str)
            if not avg:
                # Last resort: fetch live ticker
                try:
                    ticker = await self._safe_call(self.exchange.fetch_ticker(resolved), timeout=10)
                    last = float(ticker.get("last", 0) or 0)
                    if last > 0:
                        avg = last
                except Exception:
                    pass
            if not avg:
                avg = self._prices.get(resolved, 0)
            return {
                "order_id": str(order.get("id", "")),
                "average": float(avg or 0),
                "amount": amount,
            }
        except Exception as e:
            err = str(e)
            if "-2027" in err or "exceeded" in err.lower():
                self._mark_blocked(f"{resolved}:{side}")
                key = f"add_blocked:{resolved}:{side}"
                s = _throttle_warn.emit(key)
                if s is not None:
                    log.warning(f"Add position blocked (max position) {resolved} {side}: {e}{s}")
            elif "-2019" in err or "insufficient" in err.lower():
                pass  # silent skip
            else:
                key = f"add_err:{resolved}:{side}:{_extract_code(err)}"
                s = _throttle_error.emit(key)
                if s is not None:
                    log.error(f"Add position failed {resolved} {side}: {e}{s}")
            return None

    async def close_position(self, symbol: str, side: str, contracts: float | None = None) -> dict | None:
        resolved = self.resolve_symbol(symbol)
        if contracts is None:
            positions = await self.get_positions([resolved])
            target = None
            for p in positions:
                pos_side = p.get("side")
                if (side == "long" and pos_side == "long") or (side == "short" and pos_side == "short"):
                    target = p
                    break
            if not target:
                return None
            contracts = float(target.get("contracts", 0) or 0)

        close_side = "sell" if side == "long" else "buy"
        try:
            order = await self._safe_call(self.exchange.create_order(
                symbol=resolved, type="market", side=close_side, amount=contracts,
                params={"positionSide": side.upper()},
            ), timeout=15)
            log.info(f"Close {side} {resolved} {contracts} -> {order.get('id')}")
            avg = order.get("average") or order.get("price")
            if not avg:
                info = order.get("info", {})
                avg = info.get("avgPrice") or info.get("averagePrice")
            if not avg:
                cost = order.get("cost")
                filled = order.get("filled")
                if cost and filled and filled > 0:
                    avg = cost / filled
            if not avg:
                # Fallback to lastPrice from market info cache (updated by refresh_prices)
                market = self.market_info.get(resolved, {})
                price_str = market.get("info", {}).get("lastPrice", "0")
                if price_str and float(price_str) > 0:
                    avg = float(price_str)
            if not avg:
                # Last resort: fetch live ticker so exit price is never 0
                try:
                    ticker = await self._safe_call(self.exchange.fetch_ticker(resolved), timeout=10)
                    last = float(ticker.get("last", 0) or 0)
                    if last > 0:
                        avg = last
                except Exception:
                    pass
            if not avg:
                avg = self._prices.get(resolved, 0)
            return {
                "order_id": str(order.get("id", "")),
                "average": float(avg or 0),
                "closedPnL": order.get("closedPnL", 0),
                "contracts": contracts,
            }
        except Exception as e:
            log.warning(f"Close failed {resolved} {side}: {e}")
            return None

    async def close_all_positions(self, positions: list[dict]) -> list[dict]:
        tasks = []
        for p in positions:
            symbol = p["symbol"]
            pos_side = p.get("side")
            contracts = float(p.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            close_side = "sell" if pos_side == "long" else "buy"

            async def _close(sym=symbol, cs=close_side, amt=contracts, ps=pos_side):
                try:
                    order = await self._safe_call(self.exchange.create_order(
                        symbol=sym, type="market", side=cs, amount=amt,
                        params={"reduceOnly": True, "positionSide": ps.upper()},
                    ), timeout=15)
                    log.info(f"All-close {ps} {sym} {amt} -> {order.get('id')}")
                    return {
                        "symbol": sym, "side": ps, "amount": amt,
                        "order": {
                            "order_id": str(order.get("id", "")),
                            "average": float(order.get("average", 0) or 0),
                            "closedPnL": order.get("closedPnL", 0),
                        },
                        "error": None,
                    }
                except Exception as e:
                    log.error(f"All-close failed {sym} {ps}: {e}")
                    return {"symbol": sym, "side": ps, "amount": amt, "order": None, "error": str(e)}

            tasks.append(_close())

        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        normalized = []
        for r in results:
            if isinstance(r, BaseException):
                log.error(f"All-close task raised exception: {r}")
                normalized.append({"symbol": "", "side": "", "amount": 0, "order": None, "error": str(r)})
            else:
                normalized.append(r)
        return normalized
