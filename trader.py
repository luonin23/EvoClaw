import asyncio
import logging
import json
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, exchange_client, database, config_path: str = "config.json"):
        self.client = exchange_client
        self.db = database
        self.config_path = config_path
        self.running = False
        self.config = self._load_config()
        self._candidate_symbols = []
        self._last_symbol_refresh = 0
        self._refresh_lock = asyncio.Lock()

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to load config: {e}")
            return getattr(self, "config", {})

    def _get_config(self):
        """Reload config each tick for hot-loading."""
        try:
            with open(self.config_path, "r") as f:
                self.config = json.load(f)
        except Exception as e:
            log.error(f"Config reload failed: {e}")
        return self.config

    def _get_sides(self) -> list[str]:
        side = self.config.get("side", "both")
        if side == "both":
            return ["long", "short"]
        return [side]

    async def run(self):
        self.running = True
        log.info("Trader started")
        while self.running:
            try:
                await self.tick()
            except Exception as e:
                log.error(f"Tick error: {e}")
            await asyncio.sleep(self._get_config().get("position_check_interval", 1))
        log.info("Trader stopped")

    def stop(self):
        self.running = False

    async def _ensure_symbols(self):
        """Refresh candidate symbols if interval has passed."""
        cfg = self._get_config()
        interval = cfg.get("symbol_refresh_interval", 86400)
        now = datetime.now(timezone.utc).timestamp()
        async with self._refresh_lock:
            if not self._candidate_symbols or (now - self._last_symbol_refresh) >= interval:
                volume_threshold = cfg.get("volume_threshold", 0)
                price_threshold = cfg.get("price_threshold", 0)
                self._candidate_symbols = await self.client.get_candidate_symbols(volume_threshold, price_threshold)
                self._last_symbol_refresh = now
                log.info(f"Symbols refreshed: {len(self._candidate_symbols)} (interval={interval}s)")

    async def refresh_symbols_now(self):
        """Manual refresh of candidate symbols."""
        async with self._refresh_lock:
            cfg = self._get_config()
            volume_threshold = cfg.get("volume_threshold", 0)
            price_threshold = cfg.get("price_threshold", 0)
            self._candidate_symbols = await self.client.get_candidate_symbols(volume_threshold, price_threshold)
            self._last_symbol_refresh = datetime.now(timezone.utc).timestamp()
            log.info(f"Symbols manually refreshed: {len(self._candidate_symbols)}")
        return self._candidate_symbols

    async def tick(self):
        cfg = self._get_config()
        sides = self._get_sides()
        skip = set(cfg.get("skip_symbols", []))

        # Dynamic symbol selection based on volume & price (cached)
        await self._ensure_symbols()
        candidate_symbols = self._candidate_symbols
        if not candidate_symbols:
            return

        # Refresh prices every tick so calc_min_contracts uses latest price
        await self.client.refresh_prices(candidate_symbols)

        # STEP 1: Get exchange positions for candidate symbols
        exchange_positions = await self.client.get_positions(candidate_symbols)

        # STEP 2: All-close check — only on system-tracked positions
        if cfg.get("enable_all_close", False):
            await self.check_all_close(candidate_symbols, sides)

        # Re-fetch after potential all-close
        exchange_positions = await self.client.get_positions(candidate_symbols)

        # STEP 3: Single-symbol profit close — ALL positions (not limited to candidates)
        all_positions = await self.client.get_positions()
        for p in list(all_positions):
            symbol = self.client.user_symbol(p["symbol"])
            pos_side = p.get("side")
            if symbol in skip:
                continue
            await self.check_single_close(p, symbol, pos_side)

        # Re-fetch after single closes
        exchange_positions = await self.client.get_positions(candidate_symbols)
        all_positions = await self.client.get_positions()

        # STEP 3.5: Single pair close — all positions with both long+short
        if cfg.get("enable_single_pair_close", False):
            await self.check_single_pair_close(all_positions, skip)

        # Re-fetch after pair closes
        exchange_positions = await self.client.get_positions(candidate_symbols)

        # STEP 4: Margin call — add position if loss exceeds threshold (all positions, skip whitelist)
        if cfg.get("enable_margin_call", False):
            all_positions_for_margin = await self.client.get_positions()
            await self.check_margin_call(all_positions_for_margin, skip)

        # Re-fetch after margin calls
        exchange_positions = await self.client.get_positions(candidate_symbols)

        # STEP 5: Replenish only if system has no tracked open for that side
        await self.replenish_missing(exchange_positions, candidate_symbols, sides)

    # ========== All-close: all positions excluding skip whitelist ==========

    async def check_all_close(self, symbols, sides):
        cfg = self.config
        threshold = cfg.get("all_close_threshold", 0.002)
        skip = set(cfg.get("skip_symbols", []))

        # Get ALL exchange positions (not limited to system-tracked)
        all_positions = await self.client.get_positions()
        if not all_positions:
            return

        # Build system position map for open_fee lookup
        system_positions = self.db.get_open_positions()
        system_map = {}
        for sp in system_positions:
            system_map[f"{sp['symbol']}:{sp['side']}"] = sp

        total_pnl = 0.0
        total_value = 0.0
        targets = []  # positions to close

        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            pos_side = p.get("side")
            if sym in skip:
                continue

            pnl = float(p.get("unrealizedPnl", 0) or 0)
            entry_price = float(p.get("entryPrice", 0) or 0)
            contracts = float(p.get("contracts", 0) or 0)
            market = self.client.get_market_info(sym)
            contract_size = market.get("contractSize", 1) or 1
            value = entry_price * contracts * contract_size

            if value > 0:
                total_pnl += pnl
                total_value += value
                targets.append({
                    "symbol": sym,
                    "side": pos_side,
                    "entry_price": entry_price,
                    "contracts": contracts,
                })

        if total_value <= 0:
            return
        if total_pnl / total_value >= threshold:
            log.info(f"ALL CLOSE: pnl={total_pnl:.4f} value={total_value:.2f} rate={total_pnl/total_value:.4f} targets={len(targets)}")
            # Close all targeted positions (system + manual)
            for t in targets:
                sym = t["symbol"]
                pos_side = t["side"]
                result = await self.client.close_position(sym, pos_side)
                if result:
                    # Get open_fee from system tracking if available
                    open_fee = 0
                    sp = system_map.get(f"{sym}:{pos_side}")
                    if sp:
                        open_fee = sp.get("open_fee", 0)
                        self.db.remove_open(sym, pos_side)
                    if open_fee <= 0:
                        # Estimate for manual positions
                        market = self.client.get_market_info(sym)
                        cs = market.get("contractSize", 1) or 1
                        open_fee = t["entry_price"] * t["contracts"] * cs * 0.0005

                    await self._record_trade(
                        symbol=sym,
                        side=pos_side,
                        entry_price=t["entry_price"],
                        contracts=t["contracts"],
                        close_result=result,
                        trade_type="all_close",
                        open_fee=open_fee,
                    )
            # Replenish all
            await self.replenish_all(symbols, sides)

    # ========== Single close ==========

    async def check_single_close(self, position, symbol, pos_side):
        """Close any position (system or manual) if profit rate exceeds threshold."""
        cfg = self.config
        threshold = cfg.get("profit_threshold", 0.002)

        unrealized_pnl = float(position.get("unrealizedPnl", 0) or 0)
        entry_price = float(position.get("entryPrice", 0) or 0)
        contracts = float(position.get("contracts", 0) or 0)
        market = self.client.get_market_info(symbol)
        contract_size = market.get("contractSize", 1) or 1
        position_value = entry_price * contracts * contract_size

        if position_value <= 0:
            return

        profit_rate = unrealized_pnl / position_value
        if profit_rate >= threshold:
            log.info(f"SINGLE CLOSE {symbol} {pos_side}: pnl={unrealized_pnl:.4f} rate={profit_rate:.4%}")
            result = await self.client.close_position(symbol, pos_side)
            if result:
                # Get open_fee before removing
                open_fee = 0
                if self.db.has_open(symbol, pos_side):
                    for sp in self.db.get_open_positions():
                        if sp["symbol"] == symbol and sp["side"] == pos_side:
                            open_fee = sp.get("open_fee", 0)
                            break
                # Remove from tracking if tracked
                self.db.remove_open(symbol, pos_side)
                # Record trade
                await self._record_trade(
                    symbol=symbol,
                    side=pos_side,
                    entry_price=entry_price,
                    contracts=contracts,
                    close_result=result,
                    trade_type="single",
                    open_fee=open_fee,
                )

    # ========== Single pair close (多空对平) ==========

    async def check_single_pair_close(self, all_positions, skip):
        """When both long+short exist for same symbol and average profit rate >= threshold,
        close both sides simultaneously. Uses all exchange positions, not limited to candidates."""
        cfg = self.config
        threshold = cfg.get("pair_close_threshold", cfg.get("profit_threshold", 0.002))

        # Build position map by symbol from exchange
        by_symbol = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            if sym in skip:
                continue
            by_symbol.setdefault(sym, {})[p.get("side")] = p

        # Only process symbols with both long and short
        for sym, pair in by_symbol.items():
            if not pair or "long" not in pair or "short" not in pair:
                continue

            rates = []
            entry_map = {}
            contracts_map = {}
            for side in ("long", "short"):
                p = pair[side]
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                market = self.client.get_market_info(sym)
                cs = market.get("contractSize", 1) or 1
                val = entry * contracts * cs
                if val > 0:
                    rates.append(pnl / val)
                    entry_map[side] = entry
                    contracts_map[side] = contracts

            if len(rates) < 2:
                continue

            avg_rate = sum(rates) / len(rates)
            if avg_rate >= threshold:
                log.info(f"SINGLE PAIR CLOSE {sym}: avg_rate={avg_rate:.4%} rates={[f'{r:.4%}' for r in rates]}")
                for side in ("long", "short"):
                    result = await self.client.close_position(sym, side)
                    if result:
                        open_fee = 0
                        if self.db.has_open(sym, side):
                            for sp in self.db.get_open_positions():
                                if sp["symbol"] == sym and sp["side"] == side:
                                    open_fee = sp.get("open_fee", 0)
                                    break
                        self.db.remove_open(sym, side)
                        await self._record_trade(
                            symbol=sym, side=side,
                            entry_price=entry_map[side],
                            contracts=contracts_map[side],
                            close_result=result,
                            trade_type="pair_close",
                            open_fee=open_fee,
                        )

    # ========== Margin call (亏损加仓) ==========

    async def check_margin_call(self, all_positions, skip):
        """When position loss rate >= side-specific threshold, add position by multiplier.
        Repeatable: triggers every tick if condition still holds.
        Checks ALL exchange positions (skip whitelist only)."""
        cfg = self.config
        # Backward-compatible: fall back to legacy margin_call_threshold if new keys missing
        threshold_long = cfg.get("margin_call_threshold_long", cfg.get("margin_call_threshold", 0.01))
        threshold_short = cfg.get("margin_call_threshold_short", cfg.get("margin_call_threshold", 0.01))
        multiplier = cfg.get("margin_call_multiplier", 2)

        # Check all exchange positions (skip whitelist)
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            if sym in skip:
                continue
            side = p.get("side")

            pnl = float(p.get("unrealizedPnl", 0) or 0)
            if pnl >= 0:
                continue  # Only add on loss

            entry = float(p.get("entryPrice", 0) or 0)
            contracts = float(p.get("contracts", 0) or 0)
            market = self.client.get_market_info(sym)
            cs = market.get("contractSize", 1) or 1
            val = entry * contracts * cs
            if val <= 0:
                continue

            loss_rate = abs(pnl) / val
            threshold = threshold_long if side == "long" else threshold_short
            if loss_rate >= threshold:
                # v1.4-fix: add based on current position size, not min contracts
                add_amount = contracts * multiplier
                log.info(
                    f"MARGIN CALL {sym} {side}: loss={loss_rate:.4%} threshold={threshold:.4%} "
                    f"adding {add_amount} contracts (current={contracts} x {multiplier})"
                )
                result = await self.client.add_position(sym, side, add_amount)
                if result:
                    self.db.increment_margin_call_count()
                    new_total = contracts + add_amount
                    added_fee = result["average"] * add_amount * cs * 0.0005
                    if self.db.has_open(sym, side):
                        self.db.mark_margin_called(sym, side, new_total, added_fee)
                    else:
                        open_fee = entry * contracts * cs * 0.0005 + added_fee
                        self.db.record_open(sym, side, "margin_call", entry, new_total, open_fee)

    # ========== Replenish ==========

    async def replenish_missing(self, positions, symbols, sides):
        cfg = self._get_config()
        stop_threshold = cfg.get("replenish_stop_threshold", 0)

        # Fetch ALL positions to inspect opposite-side entry prices
        all_positions = await self.client.get_positions()
        position_map = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        current = set()
        for p in positions:
            sym = self.client.user_symbol(p["symbol"])
            key = f"{sym}:{p.get('side', '')}"
            current.add(key)

        tasks = []
        for sym in symbols:
            for side in sides:
                key = f"{sym}:{side}"
                # Only open if NOT already on exchange AND NOT already tracked by system
                if key not in current and not self.db.has_open(sym, side):
                    if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                        continue
                    open_side = "buy" if side == "long" else "sell"
                    tasks.append(self._do_open(sym, open_side, side))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def replenish_all(self, symbols, sides):
        # Fetch all positions for stop-check
        all_positions = await self.client.get_positions()
        position_map = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        cfg = self._get_config()
        stop_threshold = cfg.get("replenish_stop_threshold", 0)

        tasks = []
        for sym in symbols:
            for side in sides:
                if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                    continue
                open_side = "buy" if side == "long" else "sell"
                tasks.append(self._do_open(sym, open_side, side))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info(f"Replenished {len(tasks)} positions")

    async def _do_open(self, symbol: str, open_side: str, side: str):
        """Open position and track it in database."""
        result = await self.client.safe_open(symbol, open_side)
        if result:
            market = self.client.get_market_info(symbol)
            contract_size = market.get("contractSize", 1) or 1
            open_fee = result["average"] * result["amount"] * contract_size * 0.0005
            self.db.record_open(
                symbol=symbol,
                side=side,
                order_id=result["order_id"],
                entry_price=result["average"],
                amount=result["amount"],
                open_fee=open_fee,
            )
        else:
            log.warning(f"_do_open failed: {symbol} {side} ({open_side})")

    # ========== Record trade ==========

    async def _record_trade(self, symbol, side, entry_price, contracts, close_result, trade_type, open_fee=0):
        try:
            market = self.client.get_market_info(symbol)
            contract_size = market.get("contractSize", 1) or 1

            exit_price = float(close_result.get("average", 0) or 0)
            # ccxt may not return closedPnL, compute manually
            raw_pnl = float(close_result.get("closedPnL", 0) or 0)
            if raw_pnl == 0 and exit_price > 0:
                if side == "long":
                    raw_pnl = (exit_price - entry_price) * contracts * contract_size
                else:
                    raw_pnl = (entry_price - exit_price) * contracts * contract_size
            pnl = raw_pnl
            position_value = entry_price * contracts * contract_size
            pnl_rate = pnl / position_value if position_value > 0 else 0

            close_fee = exit_price * contracts * contract_size * 0.0005
            if open_fee <= 0:
                open_fee = entry_price * contracts * contract_size * 0.0005
            fee = open_fee + close_fee

            now = datetime.now(timezone.utc).isoformat()
            self.db.insert_trade({
                "symbol": symbol,
                "side": side,
                "type": trade_type,
                "open_time": "",
                "close_time": now,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "amount": contracts,
                "pnl": pnl,
                "pnl_rate": pnl_rate,
                "fee": fee,
            })
        except Exception as e:
            log.error(f"Record trade failed: {e}")
