import asyncio
import logging
import math
import ccxt.async_support as ccxt

log = logging.getLogger(__name__)


class ExchangeClient:
    def __init__(self, config: dict):
        exchange_class = getattr(ccxt, "binanceusdm")
        kwargs = config.get("exchange_kwargs", {}).copy()
        kwargs.setdefault("enableRateLimit", True)
        kwargs.setdefault("timeout", 10000)
        self.exchange = exchange_class(kwargs)
        self.market_info = {}
        self.symbol_map = {}
        self._reverse_map = {}
        self._prices = {}

    async def _safe_call(self, coro, timeout=10):
        """Wrap ccxt call with hard timeout to prevent event loop blocking."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("CCXT call timed out")
            raise
        except Exception:
            raise

    async def load_markets(self):
        markets = await self._safe_call(self.exchange.load_markets(), timeout=20)
        for symbol in self.exchange.symbols:
            m = markets.get(symbol)
            if m and m.get("swap") and m.get("quote") == "USDT":
                self.market_info[symbol] = m
                exchange_id = m.get("id", "")
                if exchange_id and exchange_id not in self.market_info:
                    self.symbol_map[exchange_id] = symbol
                base = symbol.replace(":", "").replace("/", "")
                if base and base not in self.market_info:
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
        amount = self.calc_min_contracts(resolved)
        if amount <= 0:
            return None
        for attempt in range(3):
            try:
                order = await self._safe_call(self.exchange.create_order(
                    symbol=resolved, type="market", side=side, amount=amount,
                    params={"positionSide": "LONG" if side == "buy" else "SHORT"},
                ), timeout=15)
                log.info(f"Open {side} {resolved} {amount} -> {order.get('id')}")
                return {
                    "order_id": str(order.get("id", "")),
                    "average": float(order.get("average", 0) or 0),
                    "amount": amount,
                }
            except Exception as e:
                err = str(e)
                if "-4164" in err and attempt < 2:
                    old = amount
                    amount = max(amount + 1, int(amount * 1.3))
                    log.warning(f"Notional too small for {resolved} {side}, retry {old} -> {amount}")
                    await asyncio.sleep(0.2)
                    continue
                if "-2027" in err or "exceeded" in err.lower():
                    log.warning(f"Open position blocked (max position) {resolved} {side}: {e}")
                else:
                    log.error(f"Open position failed {resolved} {side}: {e}")
                return None

    async def safe_open(self, symbol: str, side: str, retries: int = 1) -> dict | None:
        for i in range(retries + 1):
            result = await self.open_position(symbol, side)
            if result:
                return result
            if i < retries:
                await asyncio.sleep(0.5)
        log.warning(f"safe_open gave up after {retries + 1} attempts: {symbol} {side}")
        return None

    async def add_position(self, symbol: str, side: str, amount: float) -> dict | None:
        resolved = self.resolve_symbol(symbol)
        if amount <= 0:
            return None
        try:
            open_side = "buy" if side == "long" else "sell"
            order = await self._safe_call(self.exchange.create_order(
                symbol=resolved, type="market", side=open_side, amount=amount,
                params={"positionSide": side.upper()},
            ), timeout=15)
            log.info(f"Add {side} {resolved} {amount} -> {order.get('id')}")
            return {
                "order_id": str(order.get("id", "")),
                "average": float(order.get("average", 0) or 0),
                "amount": amount,
            }
        except Exception as e:
            err = str(e)
            if "-2027" in err or "exceeded" in err.lower():
                log.warning(f"Add position blocked (max position) {resolved} {side}: {e}")
            else:
                log.error(f"Add position failed {resolved} {side}: {e}")
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
        # Try with positionSide first, fallback to bare params
        for attempt, params in enumerate([
            {"positionSide": side.upper()},
            None,
        ]):
            try:
                kwargs = dict(symbol=resolved, type="market", side=close_side, amount=contracts)
                if params is not None:
                    kwargs["params"] = params
                order = await self._safe_call(self.exchange.create_order(**kwargs), timeout=15)
                tag = "no params" if params is None else side
                log.info(f"Close {tag} {resolved} {contracts} -> {order.get('id')}")
                return {
                    "order_id": str(order.get("id", "")),
                    "average": float(order.get("average", 0) or 0),
                    "closedPnL": order.get("closedPnL", 0),
                    "contracts": contracts,
                }
            except Exception as e:
                if attempt == 0:
                    log.warning(f"Close with positionSide failed {resolved} {side}, trying fallback: {e}")
                    continue
                log.error(f"Close failed {resolved} {side}: {e}")
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
        return [r for r in results if not isinstance(r, BaseException)]
