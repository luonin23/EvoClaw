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
        self.exchange = exchange_class(kwargs)
        self.market_info = {}
        self.symbol_map = {}
        self._reverse_map = {}  # ccxt symbol -> user symbol (e.g. ENA/USDT:USDT -> ENAUSDT)
        self._prices = {}

    async def load_markets(self):
        markets = await self.exchange.load_markets()
        for symbol in self.exchange.symbols:
            m = markets.get(symbol)
            if m and m.get("swap"):
                self.market_info[symbol] = m
                exchange_id = m.get("id", "")
                if exchange_id and exchange_id not in self.market_info:
                    self.symbol_map[exchange_id] = symbol
                base = symbol.replace(":", "").replace("/", "")
                if base and base not in self.market_info:
                    self.symbol_map[base] = symbol
                # Reverse map: ccxt symbol -> user symbol (prefer exchange_id form)
                eid = m.get("id", "")
                self._reverse_map[symbol] = eid if eid else base
        log.info(f"Loaded {len(self.market_info)} swap markets, {len(self.symbol_map)} aliases")

    async def refresh_prices(self, symbols: list):
        resolved = [self.resolve_symbol(s) for s in symbols]
        try:
            tickers = await self.exchange.fetch_tickers(resolved)
            for sym, ticker in tickers.items():
                if ticker and ticker.get("last"):
                    self._prices[sym] = float(ticker["last"])
                    if sym in self.market_info:
                        self.market_info[sym]["info"]["lastPrice"] = str(ticker["last"])
        except Exception:
            pass

    async def get_candidate_symbols(self, volume_threshold: float, price_threshold: float) -> list[str]:
        """Return user-format symbols (e.g. ENAUSDT) filtered by 24h volume and last price."""
        try:
            tickers = await self.exchange.fetch_tickers()
            candidates = []
            for ccxt_sym, ticker in tickers.items():
                if ccxt_sym not in self.market_info:
                    continue
                last = float(ticker.get("last", 0) or 0)
                volume = float(ticker.get("quoteVolume", 0) or 0)
                if last <= price_threshold and volume >= volume_threshold:
                    user_sym = self.user_symbol(ccxt_sym)
                    if user_sym:
                        candidates.append(user_sym)
            return candidates
        except Exception as e:
            log.error(f"get_candidate_symbols failed: {e}")
            return []

    def resolve_symbol(self, symbol: str) -> str:
        """Resolve user symbol (ENAUSDT) to ccxt swap symbol (ENA/USDT:USDT)."""
        if symbol in self.market_info:
            return symbol
        if symbol in self.symbol_map:
            return self.symbol_map[symbol]
        log.warning(f"Symbol {symbol} not found in swap markets")
        return symbol

    def user_symbol(self, ccxt_symbol: str) -> str:
        """Convert ccxt symbol (ENA/USDT:USDT) back to user form (ENAUSDT)."""
        if ccxt_symbol in self._reverse_map:
            return self._reverse_map[ccxt_symbol]
        return ccxt_symbol

    def get_market_info(self, symbol: str) -> dict:
        resolved = self.resolve_symbol(symbol)
        return self.market_info.get(resolved, {})

    async def get_balance(self) -> dict:
        balance = await self.exchange.fetch_balance()
        usdt = balance.get("total", {}).get("USDT", 0)
        return {"balance": float(usdt) if usdt else 0}

    async def get_positions(self, symbols: list = None) -> list[dict]:
        resolved = [self.resolve_symbol(s) for s in symbols] if symbols else None
        positions = await self.exchange.fetch_positions(resolved or None)
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
                # Try cache first
                price = self._prices.get(resolved)
                if not price or price <= 0:
                    # Fallback: from market_info
                    price_str = market.get("info", {}).get("lastPrice") or market.get("last", None)
                    if price_str:
                        price = float(price_str)
                if not price or price <= 0:
                    # Fallback: use min_notional / contract_size as rough estimate (e.g. 5 USDT at $1)
                    price = min_notional / (contract_size * 10) if contract_size > 0 else 1
                raw = max(raw, int(math.ceil(min_notional / (price * contract_size))))
            return float(round(raw, amount_precision))
        except Exception as e:
            log.error(f"calc_min_contracts failed {symbol}: {e}")
            return 1

    async def open_position(self, symbol: str, side: str) -> dict | None:
        """
        Open a market position.
        Returns dict with: order_id, average (entry price), amount (contracts)
        or None on failure.
        """
        resolved = self.resolve_symbol(symbol)
        amount = self.calc_min_contracts(resolved)
        if amount <= 0:
            return None
        for attempt in range(3):
            try:
                order = await self.exchange.create_order(
                    symbol=resolved,
                    type="market",
                    side=side,
                    amount=amount,
                    params={"positionSide": "LONG" if side == "buy" else "SHORT"},
                )
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
        """Add contracts to existing position (margin call / 加仓).
        side: "long" or "short" (position direction)
        Returns order info or None.
        """
        resolved = self.resolve_symbol(symbol)
        if amount <= 0:
            return None
        try:
            open_side = "buy" if side == "long" else "sell"
            order = await self.exchange.create_order(
                symbol=resolved,
                type="market",
                side=open_side,
                amount=amount,
                params={"positionSide": side.upper()},
            )
            log.info(f"Add {side} {resolved} {amount} -> {order.get('id')}")
            return {
                "order_id": str(order.get("id", "")),
                "average": float(order.get("average", 0) or 0),
                "amount": amount,
            }
        except Exception as e:
            log.error(f"Add position failed {resolved} {side}: {e}")
            return None

    async def close_position(self, symbol: str, side: str) -> dict | None:
        """Close position for symbol+side. Returns order info or None."""
        resolved = self.resolve_symbol(symbol)
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
        try:
            close_side = "sell" if side == "long" else "buy"
            order = await self.exchange.create_order(
                symbol=resolved,
                type="market",
                side=close_side,
                amount=contracts,
                params={"positionSide": side.upper()},
            )
            log.info(f"Close {side} {resolved} {contracts} -> {order.get('id')}")
            return {
                "order_id": str(order.get("id", "")),
                "average": float(order.get("average", 0) or 0),
                "closedPnL": order.get("closedPnL", 0),
                "contracts": contracts,
            }
        except Exception as e:
            error_msg = str(e)
            # Fallback: some accounts need no params at all
            try:
                close_side = "sell" if side == "long" else "buy"
                order = await self.exchange.create_order(
                    symbol=resolved,
                    type="market",
                    side=close_side,
                    amount=contracts,
                )
                log.info(f"Close (no params) {side} {resolved} {contracts} -> {order.get('id')}")
                return {
                    "order_id": str(order.get("id", "")),
                    "average": float(order.get("average", 0) or 0),
                    "closedPnL": order.get("closedPnL", 0),
                    "contracts": contracts,
                }
            except Exception as e2:
                log.error(f"Close fallback failed {resolved} {side}: {e2}")
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
                    order = await self.exchange.create_order(
                        symbol=sym, type="market", side=cs,
                        amount=amt, params={"reduceOnly": True, "positionSide": ps.upper()},
                    )
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
