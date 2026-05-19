import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, exchange_client, database, config_path: str = "config.json"):
        self.client = exchange_client
        self.db = database
        self.config_path = config_path
        self.running = False
        self.config = self._load_config()
        self._config_mtime = os.path.getmtime(self.config_path) if os.path.exists(self.config_path) else 0
        self._candidate_symbols = []
        self._last_symbol_refresh = 0
        self._refresh_lock = asyncio.Lock()
        # Circuit breaker: skip symbols that fail with -2027 (max position)
        self._fail2027_counts: dict[str, int] = {}
        self._fail2027_max = 5  # Skip after 5 consecutive failures
        self._fail2027_skipped_at: dict[str, float] = {}  # timestamp when circuit broke
        self._fail2027_retry_after = 600  # retry after 10 minutes (was 5, too aggressive)
        # System position lookup cache: built once per tick, O(1) lookup for open_fee
        self._system_pos_map: dict[str, dict] = {}  # "symbol:side" -> open_position row
        # Track highest profit tier executed per position to avoid repeat closes
        self._tier_executed: dict[str, int] = {}  # "symbol:side" -> highest tier index executed

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to load config: {e}")
            return getattr(self, "config", {})

    def _get_config(self):
        """Reload config only when file changed (mtime-based)."""
        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime != self._config_mtime:
                with open(self.config_path, "r") as f:
                    self.config = json.load(f)
                self._config_mtime = mtime
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

    def _is_skipped_2027(self, symbol: str) -> bool:
        """Check if symbol is circuit-broken due to consecutive -2027 errors.
        Auto-resets after _fail2027_retry_after seconds."""
        count = self._fail2027_counts.get(symbol, 0)
        if count >= self._fail2027_max:
            # Check if retry window has passed
            skipped_at = self._fail2027_skipped_at.get(symbol)
            if skipped_at:
                now = datetime.now(timezone.utc).timestamp()
                if now - skipped_at >= self._fail2027_retry_after:
                    log.info(f"CIRCUIT BREAKER: retrying {symbol} after {self._fail2027_retry_after}s")
                    del self._fail2027_counts[symbol]
                    del self._fail2027_skipped_at[symbol]
                    return False
            return True
        return False

    def _record_2027_failure(self, symbol: str):
        """Record a -2027 failure for circuit breaker."""
        self._fail2027_counts[symbol] = self._fail2027_counts.get(symbol, 0) + 1
        if self._fail2027_counts[symbol] >= self._fail2027_max:
            if symbol not in self._fail2027_skipped_at:
                self._fail2027_skipped_at[symbol] = datetime.now(timezone.utc).timestamp()
            log.warning(f"CIRCUIT BREAKER: skipping {symbol} after {self._fail2027_max} consecutive -2027 failures")

    def _clear_2027_failure(self, symbol: str):
        """Clear -2027 failure counter on success."""
        if symbol in self._fail2027_counts:
            del self._fail2027_counts[symbol]

    async def _ensure_symbols(self):
        """Refresh candidate symbols if interval has passed."""
        cfg = self._get_config()
        interval = cfg.get("symbol_refresh_interval", 86400)
        now = datetime.now(timezone.utc).timestamp()
        async with self._refresh_lock:
            if not self._candidate_symbols or (now - self._last_symbol_refresh) >= interval:
                volume_threshold = cfg.get("volume_threshold", 0)
                price_threshold = cfg.get("price_threshold", 0)
                if volume_threshold == 0 and price_threshold == 0:
                    self._candidate_symbols = cfg.get("symbols", [])
                else:
                    self._candidate_symbols = await self.client.get_candidate_symbols(volume_threshold, price_threshold)
                self._last_symbol_refresh = now
                log.info(f"Symbols refreshed: {len(self._candidate_symbols)} (interval={interval}s)")

    async def refresh_symbols_now(self):
        """Manual refresh of candidate symbols."""
        async with self._refresh_lock:
            cfg = self._get_config()
            volume_threshold = cfg.get("volume_threshold", 0)
            price_threshold = cfg.get("price_threshold", 0)
            if volume_threshold == 0 and price_threshold == 0:
                self._candidate_symbols = cfg.get("symbols", [])
            else:
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

        # STEP 1: Fetch all positions ONCE at start of tick
        all_positions = await self.client.get_positions()
        exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # Build system position lookup cache (O(1) instead of O(n) per lookup)
        self._system_pos_map = {}
        for sp in self.db.get_open_positions():
            self._system_pos_map[f"{sp['symbol']}:{sp['side']}"] = sp

        # STEP 2: All-close check — passes positions directly, no re-fetch
        if cfg.get("enable_all_close", False):
            await self.check_all_close(candidate_symbols, sides, all_positions)
            # Re-fetch after all-close (positions changed)
            all_positions = await self.client.get_positions()
            exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # Clean up tier tracking for positions that no longer exist
        current_pos_keys = {f"{self.client.user_symbol(p['symbol'])}:{p.get('side')}" for p in all_positions}
        for k in list(self._tier_executed.keys()):
            if k not in current_pos_keys:
                del self._tier_executed[k]

        # STEP 3: Single-symbol profit close — pass positions directly
        for p in list(all_positions):
            symbol = self.client.user_symbol(p["symbol"])
            if symbol in skip or self._is_skipped_2027(symbol):
                continue
            await self.check_single_close(p, symbol, p.get("side"))

        # Re-fetch after single closes
        all_positions = await self.client.get_positions()
        exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 3.5: Single pair close — pass positions directly
        if cfg.get("enable_single_pair_close", False):
            await self.check_single_pair_close(all_positions, skip)
            # Re-fetch after pair closes
            all_positions = await self.client.get_positions()
            exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 4: Margin call — pass positions directly
        if cfg.get("enable_margin_call", False):
            await self.check_margin_call(all_positions, skip)
            # Re-fetch after margin calls
            all_positions = await self.client.get_positions()
            exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 5: Replenish — pass positions directly, no internal re-fetch
        await self.replenish_missing(exchange_positions, candidate_symbols, sides, all_positions)

        # Database WAL checkpoint to prevent WAL file growth
        self.db.checkpoint()

    # ========== All-close: all positions excluding skip whitelist ==========

    async def check_all_close(self, symbols, sides, all_positions):
        cfg = self.config
        threshold = cfg.get("all_close_threshold", 0.002)
        skip = set(cfg.get("skip_symbols", []))

        if not all_positions:
            return

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
                if pnl > 0:
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
            # Close only profitable positions (never close losing ones)
            for t in targets:
                sym = t["symbol"]
                pos_side = t["side"]
                result = await self.client.close_position(sym, pos_side, t["contracts"])
                if result:
                    # Get open_fee from system tracking if available
                    open_fee = 0
                    sp = self._system_pos_map.get(f"{sym}:{pos_side}")
                    if sp:
                        open_fee = sp.get("open_fee", 0)
                        self.db.remove_open(sym, pos_side)
                        self._system_pos_map.pop(f"{sym}:{pos_side}", None)
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

    # ========== Single close (5-tier profit taking) ==========

    async def check_single_close(self, position, symbol, pos_side):
        """5-tier partial close based on profit rate.
        Tier 1: e.g. 0.2% -> close 30% of current position
        Tier 2: e.g. 1.0% -> close 50% of current position
        Tier 3: e.g. 5.0% -> close 100% of current position
        All close amounts are floored to integers. Close pct is based on CURRENT remaining contracts.
        """
        import math
        cfg = self.config
        tiers = cfg.get("profit_tiers")
        if not tiers:
            # backward compatibility: old single threshold closes 100%
            threshold = cfg.get("profit_threshold", 0.002)
            tiers = [{"threshold": threshold, "close_pct": 1.0}]

        unrealized_pnl = float(position.get("unrealizedPnl", 0) or 0)
        entry_price = float(position.get("entryPrice", 0) or 0)
        contracts = float(position.get("contracts", 0) or 0)
        market = self.client.get_market_info(symbol)
        contract_size = market.get("contractSize", 1) or 1
        position_value = entry_price * contracts * contract_size

        if position_value <= 0:
            return

        profit_rate = unrealized_pnl / position_value
        pos_key = f"{symbol}:{pos_side}"
        executed = self._tier_executed.get(pos_key, -1)

        for i, tier in enumerate(tiers):
            if i <= executed:
                continue
            threshold = tier.get("threshold", 0)
            if profit_rate >= threshold:
                close_pct = tier.get("close_pct", 1.0)
                close_contracts = math.floor(contracts * close_pct)
                if close_contracts < 1:
                    continue
                log.info(f"TIER CLOSE {symbol} {pos_side}: tier={i+1} pnl={unrealized_pnl:.4f} rate={profit_rate:.4%} close={close_contracts}/{contracts}")
                result = await self.client.close_position(symbol, pos_side, close_contracts)
                if result:
                    open_fee = 0
                    sp = self._system_pos_map.get(pos_key)
                    if sp:
                        open_fee = sp.get("open_fee", 0)
                    # Only remove tracking if fully closed
                    if close_contracts >= contracts:
                        self.db.remove_open(symbol, pos_side)
                        self._system_pos_map.pop(pos_key, None)
                    await self._record_trade(
                        symbol=symbol,
                        side=pos_side,
                        entry_price=entry_price,
                        contracts=close_contracts,
                        close_result=result,
                        trade_type="single",
                        open_fee=open_fee,
                    )
                    self._tier_executed[pos_key] = i
                break

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
            # Only close pair when BOTH sides are profitable (never close a losing side)
            if avg_rate >= threshold and min(rates) > 0:
                log.info(f"SINGLE PAIR CLOSE {sym}: avg_rate={avg_rate:.4%} rates={[f'{r:.4%}' for r in rates]}")
                for side in ("long", "short"):
                    result = await self.client.close_position(sym, side, contracts_map[side])
                    if result:
                        open_fee = 0
                        sp = self._system_pos_map.get(f"{sym}:{side}")
                        if sp:
                            open_fee = sp.get("open_fee", 0)
                        self.db.remove_open(sym, side)
                        self._system_pos_map.pop(f"{sym}:{side}", None)
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

    async def replenish_missing(self, positions, symbols, sides, all_positions):
        cfg = self._get_config()
        stop_threshold = cfg.get("replenish_stop_threshold", 0)
        max_count = cfg.get("max_position_count", 0)

        # Check position count limit before any open
        if max_count > 0:
            if len(all_positions) >= max_count:
                log.info(f"REPLENISH SKIP: total positions {len(all_positions)} >= limit {max_count}")
                return

        # Use passed positions to inspect opposite-side entry prices
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

        max_new = max_count - len(all_positions) if max_count > 0 else None
        tasks = []
        for sym in symbols:
            for side in sides:
                if max_new is not None and len(tasks) >= max_new:
                    log.info(f"REPLENISH LIMIT: stop at {max_count} positions")
                    break
                key = f"{sym}:{side}"
                # Only open if NOT already on exchange AND NOT already tracked by system AND not circuit-broken
                if key not in current and not self.db.has_open(sym, side) and not self._is_skipped_2027(sym):
                    if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                        continue
                    open_side = "buy" if side == "long" else "sell"
                    tasks.append(self._do_open(sym, open_side, side))
            if max_new is not None and len(tasks) >= max_new:
                break
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def replenish_all(self, symbols, sides, all_positions):
        cfg = self._get_config()
        max_count = cfg.get("max_position_count", 0)
        if max_count > 0:
            if len(all_positions) >= max_count:
                log.info(f"REPLENISH ALL SKIP: total positions {len(all_positions)} >= limit {max_count}")
                return

        # Use passed positions for stop-check
        position_map = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        stop_threshold = cfg.get("replenish_stop_threshold", 0)

        max_new = max_count - len(all_positions) if max_count > 0 else None
        tasks = []
        for sym in symbols:
            for side in sides:
                if max_new is not None and len(tasks) >= max_new:
                    log.info(f"REPLENISH ALL LIMIT: stop at {max_count} positions")
                    break
                if self._is_skipped_2027(sym):
                    continue
                if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                    continue
                open_side = "buy" if side == "long" else "sell"
                tasks.append(self._do_open(sym, open_side, side))
            if max_new is not None and len(tasks) >= max_new:
                break
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info(f"Replenished {len(tasks)} positions")

    async def _do_open(self, symbol: str, open_side: str, side: str):
        """Open position and track it in database."""
        # Circuit breaker: skip if already at max consecutive failures
        if self._is_skipped_2027(symbol):
            return
        result = await self.client.safe_open(symbol, open_side)
        if result:
            self._clear_2027_failure(symbol)
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
            self._record_2027_failure(symbol)
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
