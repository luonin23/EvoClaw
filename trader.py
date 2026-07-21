import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from exchange_client import _throttle_warn

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, exchange_client, database):
        self.client = exchange_client
        self.db = database
        self.running = False
        self.config = self._load_config()
        self._candidate_symbols = []
        self._last_symbol_refresh = 0
        self._fail2027_counts: dict[str, int] = {}
        self._fail2027_max = 5
        self._fail2027_skipped_at: dict[str, float] = {}
        self._fail2027_retry_after = 600
        self._system_pos_map: dict[str, dict] = {}
        self._tier_executed: dict[str, int] = {}  # loaded from DB below

        # === Phase 1: Margin call rate limiting ===
        self._mc_last_success: dict[str, float] = {}     # symbol -> timestamp of last SUCCESSFUL margin call
        self._mc_fail_streak: dict[str, int] = {}         # symbol -> consecutive failures
        self._mc_max_fail_streak = 5
        self._mc_cooldown_success = 3600                   # 1 hour after any success
        self._mc_cooldown_fail = 3600                      # 1 hour after max consecutive failures

        # === Phase 2: Price refresh health ===
        self._last_price_ok: float = 0                     # timestamp of last successful refresh_prices
        self._price_fail_streak: int = 0
        self._price_max_fail_streak = 3
        self._price_stale_seconds = 300                    # prices considered stale after 5 min

        # === Phase 2: Heartbeat ===
        self._last_heartbeat_tick: int = 0

        # Load persisted tier states from DB (survives restarts)
        try:
            self._tier_executed = self.db.load_tier_states()
            if self._tier_executed:
                import logging as _log
                _log.getLogger(__name__).info(f"Loaded {len(self._tier_executed)} tier states from DB")
        except Exception:
            pass

    def _load_config(self):
        """Load config from database (source of truth for ALL settings including exchange_kwargs)."""
        try:
            return self.db.load_config()
        except Exception as e:
            log.error(f"Failed to load config: {e}")
            return getattr(self, "config", {})

    def _get_config(self):
        """Reload latest config from database (always fresh)."""
        try:
            self.config = self._load_config()
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
        tick_count = 0
        interval = max(3, self._get_config().get("position_check_interval", 3))  # minimum 3s
        log.info("Trader started")

        # Track tick duration for health monitoring
        tick_start = time.monotonic()

        while self.running:
            try:
                # === Phase 1: Per-tick timeout (30s) to prevent event-loop hangs ===
                await asyncio.wait_for(self.tick(), timeout=30)
                tick_count += 1

                # === Phase 2: Heartbeat every 60 ticks (~1 minute) ===
                if tick_count % 60 == 0:
                    self.db.checkpoint_restart()
                    elapsed = time.monotonic() - tick_start
                    price_age = f"{time.monotonic() - self._last_price_ok:.0f}s" if self._last_price_ok > 0 else "N/A"
                    log.info(
                        f"HEARTBEAT: tick={tick_count} "
                        f"pos={len(self._system_pos_map)} "
                        f"skipped_2027={len(self._fail2027_skipped_at)} "
                        f"mc_cooldowns={len(self._mc_last_success)} "
                        f"price_age={price_age}"
                    )
                    tick_start = time.monotonic()

                if tick_count % 300 == 0:
                    self._cleanup_stale_2027()
                    self._cleanup_stale_mc_state()

                # Re-read interval occasionally in case config changed
                if tick_count % 100 == 0:
                    interval = max(3, self._get_config().get("position_check_interval", 3))

            except asyncio.TimeoutError:
                # === Phase 1: Tick timeout — log and continue ===
                tick_count += 1
                log.error(
                    f"TICK TIMEOUT: tick() exceeded 30s (tick={tick_count}). "
                    f"Forcing next cycle. Check Binance API connectivity."
                )
            except Exception as e:
                tick_count += 1
                log.error(f"Tick error: {e}")

            await asyncio.sleep(interval)
        log.info("Trader stopped")

    def stop(self):
        self.running = False

    def _is_skipped_2027(self, symbol: str) -> bool:
        count = self._fail2027_counts.get(symbol, 0)
        if count >= self._fail2027_max:
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
        self._fail2027_counts[symbol] = self._fail2027_counts.get(symbol, 0) + 1
        if self._fail2027_counts[symbol] >= self._fail2027_max:
            if symbol not in self._fail2027_skipped_at:
                self._fail2027_skipped_at[symbol] = datetime.now(timezone.utc).timestamp()
            s = _throttle_warn.emit(f"cb_skip:{symbol}")
            if s is not None:
                log.warning(f"CIRCUIT BREAKER: skipping {symbol} after {self._fail2027_max} consecutive -2027 failures{s}")

    def _cleanup_stale_2027(self):
        for sym in list(self._fail2027_counts.keys()):
            if sym in self._fail2027_skipped_at:
                continue
            if self._fail2027_counts[sym] < self._fail2027_max:
                del self._fail2027_counts[sym]

    def _clear_2027_failure(self, symbol: str):
        if symbol in self._fail2027_counts:
            del self._fail2027_counts[symbol]

    # === Phase 1: Margin call rate limiting helpers ===

    def _is_mc_cooldown(self, symbol: str) -> bool:
        """Check if margin call is in cooldown for this symbol."""
        last_ok = self._mc_last_success.get(symbol)
        if last_ok is not None:
            now = time.monotonic()
            if now - last_ok < self._mc_cooldown_success:
                return True
            else:
                # Cooldown expired, clean up
                del self._mc_last_success[symbol]
        return False

    def _record_mc_success(self, symbol: str):
        """Record a successful margin call — starts cooldown."""
        self._mc_last_success[symbol] = time.monotonic()
        self._mc_fail_streak.pop(symbol, None)

    def _record_mc_failure(self, symbol: str):
        """Record a failed margin call attempt."""
        streak = self._mc_fail_streak.get(symbol, 0) + 1
        self._mc_fail_streak[symbol] = streak
        if streak >= self._mc_max_fail_streak:
            # Cooldown same as success — 1 hour
            self._mc_last_success[symbol] = time.monotonic()
            log.warning(
                f"MARGIN CALL COOLDOWN: {symbol} after {streak} consecutive failures, "
                f"cooling down for {self._mc_cooldown_fail}s"
            )

    def _cleanup_stale_mc_state(self):
        """Periodically clean up stale MC state entries."""
        now = time.monotonic()
        for sym in list(self._mc_last_success.keys()):
            if now - self._mc_last_success[sym] >= max(self._mc_cooldown_success, self._mc_cooldown_fail) * 2:
                del self._mc_last_success[sym]
        for sym in list(self._mc_fail_streak.keys()):
            if sym not in self._mc_last_success:
                del self._mc_fail_streak[sym]

    async def _ensure_symbols(self):
        cfg = self._get_config()
        interval = cfg.get("symbol_refresh_interval", 86400)
        now = datetime.now(timezone.utc).timestamp()
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

    # ====================================================================
    # tick() — main loop. Only re-fetches positions when a step actually
    # executed trades, to minimize exchange API calls.
    # ====================================================================

    async def tick(self):
        cfg = self._get_config()
        sides = self._get_sides()
        skip = set(cfg.get("skip_symbols", []))

        await self._ensure_symbols()
        candidate_symbols = self._candidate_symbols
        if not candidate_symbols:
            return

        # === Phase 2: Price refresh with staleness detection ===
        try:
            await self.client.refresh_prices(candidate_symbols)
            self._last_price_ok = time.monotonic()
            self._price_fail_streak = 0
        except Exception:
            # refresh_prices already logs the warning internally
            self._price_fail_streak += 1
            if self._price_fail_streak >= self._price_max_fail_streak:
                # Multiple consecutive failures — skip this tick entirely
                if self._price_fail_streak == self._price_max_fail_streak:
                    log.error(
                        f"PRICE FEED STALE: refresh_prices failed {self._price_fail_streak} times. "
                        f"Prices are {time.monotonic() - self._last_price_ok:.0f}s old. "
                        f"Continuing with stale prices..."
                    )
            # Don't return — continue with stale prices rather than stopping entirely

        # === Phase 2: Warn if prices are stale ===
        if self._last_price_ok > 0 and (time.monotonic() - self._last_price_ok) > self._price_stale_seconds:
            tick_count = getattr(self, '_stale_warn_count', 0)
            if tick_count == 0:
                log.warning(
                    f"STALE PRICES: last successful refresh was "
                    f"{time.monotonic() - self._last_price_ok:.0f}s ago. "
                    f"Trading decisions may be unreliable."
                )
            self._stale_warn_count = (tick_count + 1) % 60

        # STEP 1: Fetch positions ONCE (timeout-safe)
        try:
            all_positions = await self.client.get_positions()
        except Exception as e:
            log.warning(f"get_positions() failed in tick, skipping cycle: {e}")
            return
        exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]
        did_change = False

        # Build system position lookup cache
        self._system_pos_map = {}
        for sp in self.db.get_open_positions():
            self._system_pos_map[f"{sp['symbol']}:{sp['side']}"] = sp

        # STEP 1.5: Liquidation detection — find positions that vanished from exchange
        await self._detect_liquidations(all_positions)

        # STEP 2: All-close
        if cfg.get("enable_all_close", False):
            if await self.check_all_close(candidate_symbols, sides, all_positions):
                did_change = True
                all_positions = await self.client.get_positions()
                exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # Clean up tier tracking for positions that no longer exist
        current_pos_keys = {f"{self.client.user_symbol(p['symbol'])}:{p.get('side')}" for p in all_positions}
        for k in list(self._tier_executed.keys()):
            if k not in current_pos_keys:
                del self._tier_executed[k]

        # STEP 3: Single-symbol profit close
        closed_any = False
        for p in all_positions:
            symbol = self.client.user_symbol(p["symbol"])
            if symbol in skip or self._is_skipped_2027(symbol):
                continue
            if await self.check_single_close(p, symbol, p.get("side")):
                closed_any = True

        if closed_any:
            did_change = True
            all_positions = await self.client.get_positions()
            exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 3.5: Single pair close
        if cfg.get("enable_single_pair_close", False):
            if await self.check_single_pair_close(all_positions, skip):
                did_change = True
                all_positions = await self.client.get_positions()
                exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 4: Margin call
        if cfg.get("enable_margin_call", False):
            if await self.check_margin_call(all_positions, skip):
                did_change = True
                all_positions = await self.client.get_positions()
                exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]

        # STEP 5: Replenish
        await self.replenish_missing(exchange_positions, candidate_symbols, sides, all_positions)

    # ========== Helpers ==========

    @staticmethod
    def _position_value(entry: float, contracts: float, contract_size: float) -> float:
        return entry * contracts * contract_size

    @staticmethod
    def _pnl_rate(pnl: float, position_value: float) -> float:
        return pnl / position_value if position_value > 0 else 0

    # ========== All-close ==========

    async def check_all_close(self, symbols, sides, all_positions) -> bool:
        """Return True if any positions were closed."""
        cfg = self.config
        threshold = cfg.get("all_close_threshold", 0.002)
        skip = set(cfg.get("skip_symbols", []))

        if not all_positions:
            return False

        total_pnl = 0.0
        total_value = 0.0
        targets = []

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
            value = self._position_value(entry_price, contracts, contract_size)

            if value > 0:
                total_pnl += pnl
                total_value += value
                targets.append({
                    "symbol": sym,
                    "side": pos_side,
                    "entry_price": entry_price,
                    "contracts": contracts,
                })

        if total_value <= 0 or total_pnl / total_value < threshold:
            return False

        log.info(f"ALL CLOSE: pnl={total_pnl:.4f} value={total_value:.2f} rate={total_pnl/total_value:.4f} targets={len(targets)}")
        for t in targets:
            sym = t["symbol"]
            pos_side = t["side"]
            result = await self.client.close_position(sym, pos_side, t["contracts"])
            if result:
                open_fee = 0
                sp = self._system_pos_map.get(f"{sym}:{pos_side}")
                if sp:
                    open_fee = sp.get("open_fee", 0)
                    self.db.remove_open(sym, pos_side)
                    self._system_pos_map.pop(f"{sym}:{pos_side}", None)
                if open_fee <= 0:
                    market = self.client.get_market_info(sym)
                    cs = market.get("contractSize", 1) or 1
                    open_fee = t["entry_price"] * t["contracts"] * cs * 0.0005

                open_time = sp.get("entry_time", "") if sp else ""
                await self._record_trade(
                    symbol=sym, side=pos_side,
                    entry_price=t["entry_price"], contracts=t["contracts"],
                    close_result=result, trade_type="all_close", open_fee=open_fee,
                    open_time=open_time,
                )
        await self.replenish_all(symbols, sides)
        return True

    # ========== Single close (5-tier profit taking) ==========

    async def check_single_close(self, position, symbol, pos_side) -> bool:
        """Return True if a close was executed."""
        cfg = self.config
        tiers = cfg.get("profit_tiers")
        if not tiers:
            threshold = cfg.get("profit_threshold", 0.002)
            tiers = [{"threshold": threshold, "close_pct": 1.0}]

        unrealized_pnl = float(position.get("unrealizedPnl", 0) or 0)
        entry_price = float(position.get("entryPrice", 0) or 0)
        contracts = float(position.get("contracts", 0) or 0)
        market = self.client.get_market_info(symbol)
        contract_size = market.get("contractSize", 1) or 1
        position_value = self._position_value(entry_price, contracts, contract_size)

        if position_value <= 0:
            return False

        profit_rate = self._pnl_rate(unrealized_pnl, position_value)
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
                    # Position too small for this tier — mark as attempted
                    # and move to next tier. Persist so restart doesn't loop.
                    self._tier_executed[pos_key] = i
                    try:
                        self.db.set_tier_executed(symbol, pos_side, i)
                    except Exception:
                        pass
                    continue
                log.info(f"TIER CLOSE {symbol} {pos_side}: tier={i+1} pnl={unrealized_pnl:.4f} rate={profit_rate:.4%} close={close_contracts}/{contracts}")
                result = await self.client.close_position(symbol, pos_side, close_contracts)
                if result:
                    open_fee = 0
                    sp = self._system_pos_map.get(pos_key)
                    if sp:
                        open_fee = sp.get("open_fee", 0)
                    remaining = contracts - close_contracts
                    if remaining <= 0:
                        self.db.remove_open(symbol, pos_side)
                        self._system_pos_map.pop(pos_key, None)
                    else:
                        try:
                            self.db.update_open_amount(symbol, pos_side, remaining)
                            if sp:
                                sp["amount"] = remaining
                        except Exception:
                            pass
                    open_time = sp.get("entry_time", "") if sp else ""
                    await self._record_trade(
                        symbol=symbol, side=pos_side,
                        entry_price=entry_price, contracts=close_contracts,
                        close_result=result, trade_type="single", open_fee=open_fee,
                        open_time=open_time,
                    )
                    self._tier_executed[pos_key] = i
                    # Persist tier state to DB (survives restarts)
                    try:
                        self.db.set_tier_executed(symbol, pos_side, i)
                    except Exception:
                        pass
                return True
        return False

    # ========== Single pair close ==========

    async def check_single_pair_close(self, all_positions, skip) -> bool:
        """Return True if any pair was closed."""
        cfg = self.config
        threshold = cfg.get("pair_close_threshold", cfg.get("profit_threshold", 0.002))

        by_symbol = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            if sym in skip:
                continue
            by_symbol.setdefault(sym, {})[p.get("side")] = p

        closed_any = False
        for sym, pair in by_symbol.items():
            if not pair or "long" not in pair or "short" not in pair:
                continue

            rates = []
            entry_map = {}
            contracts_map = {}
            market = self.client.get_market_info(sym)
            cs = market.get("contractSize", 1) or 1
            total_pnl = 0
            total_val = 0
            for side in ("long", "short"):
                p = pair[side]
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                val = self._position_value(entry, contracts, cs)
                if val > 0:
                    rates.append(pnl / val)
                    total_pnl += pnl
                    total_val += val
                    entry_map[side] = entry
                    contracts_map[side] = contracts

            if len(rates) < 2 or total_val <= 0:
                continue

            avg_rate = total_pnl / total_val
            if avg_rate >= threshold:
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
                        open_time = sp.get("entry_time", "") if sp else ""
                        await self._record_trade(
                            symbol=sym, side=side,
                            entry_price=entry_map[side], contracts=contracts_map[side],
                            close_result=result, trade_type="pair_close", open_fee=open_fee,
                            open_time=open_time,
                        )
                closed_any = True
        return closed_any

    # ========== Margin call (Phase 1: with rate limiting) ==========

    async def check_margin_call(self, all_positions, skip) -> bool:
        """Return True if any margin call was executed.

        Phase 1 protections:
        - Per-symbol cooldown: max 1 margin call per hour per symbol
        - Circuit breaker: after 5 consecutive failures, cooldown 1 hour
        - -2027 circuit breaker integration: checks _is_skipped_2027
        """
        cfg = self.config
        threshold_long = cfg.get("margin_call_threshold_long", cfg.get("margin_call_threshold", 0.01))
        threshold_short = cfg.get("margin_call_threshold_short", cfg.get("margin_call_threshold", 0.01))
        multiplier = cfg.get("margin_call_multiplier", 2)

        executed = False
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            if sym in skip:
                continue

            # === Phase 1: Check -2027 circuit breaker ===
            if self._is_skipped_2027(sym):
                continue

            # === Phase 1: Check margin call cooldown ===
            if self._is_mc_cooldown(sym):
                continue

            side = p.get("side")

            pnl = float(p.get("unrealizedPnl", 0) or 0)
            if pnl >= 0:
                # Position is profitable — clear failure streak
                self._mc_fail_streak.pop(sym, None)
                continue

            entry = float(p.get("entryPrice", 0) or 0)
            contracts = float(p.get("contracts", 0) or 0)
            market = self.client.get_market_info(sym)
            cs = market.get("contractSize", 1) or 1
            val = self._position_value(entry, contracts, cs)
            if val <= 0:
                continue

            loss_rate = abs(pnl) / val
            threshold = threshold_long if side == "long" else threshold_short
            if loss_rate >= threshold:
                add_amount = contracts * multiplier
                min_amount = self.client.calc_min_contracts(sym)
                if add_amount < min_amount:
                    add_amount = min_amount
                log.info(
                    f"MARGIN CALL {sym} {side}: loss={loss_rate:.4%} threshold={threshold:.4%} "
                    f"adding {add_amount} contracts (current={contracts} x {multiplier})"
                )
                result = await self.client.add_position(sym, side, add_amount)
                if result:
                    # === Phase 1: Record success → start cooldown ===
                    self._record_mc_success(sym)
                    self._clear_2027_failure(sym)
                    self.db.increment_margin_call_count()
                    new_total = contracts + add_amount
                    added_fee = result["average"] * add_amount * cs * 0.0005
                    if self.db.has_open(sym, side):
                        self.db.mark_margin_called(sym, side, new_total, added_fee)
                    else:
                        open_fee = entry * contracts * cs * 0.0005 + added_fee
                        self.db.record_open(sym, side, "margin_call", entry, new_total, open_fee)
                    executed = True
                else:
                    # === Phase 1: Record failure ===
                    self._record_mc_failure(sym)
                    # Also feed into -2027 circuit breaker (add_position already logs if -2027)
                    self._record_2027_failure(sym)
        return executed

    # ========== Replenish ==========

    async def replenish_missing(self, positions, symbols, sides, all_positions):
        cfg = self._get_config()
        stop_threshold = cfg.get("replenish_stop_threshold", 0)
        max_count = cfg.get("max_position_count", 0)

        if max_count > 0 and len(all_positions) >= max_count:
            log.debug(f"REPLENISH SKIP: total positions {len(all_positions)} >= limit {max_count}")
            return

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
        skip = set(cfg.get('skip_symbols', []))
        for sym in symbols:
            if sym in skip:
                continue
            for side in sides:
                if max_new is not None and len(tasks) >= max_new:
                    break
                key = f"{sym}:{side}"
                if key not in current and not self.db.has_open(sym, side) and not self._is_skipped_2027(sym):
                    if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                        continue
                    open_side = "buy" if side == "long" else "sell"
                    tasks.append(self._do_open(sym, open_side, side))
            if max_new is not None and len(tasks) >= max_new:
                break
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def replenish_all(self, symbols, sides):
        cfg = self._get_config()
        max_count = cfg.get("max_position_count", 0)

        all_positions = await self.client.get_positions()
        if max_count > 0 and len(all_positions) >= max_count:
            log.debug(f"REPLENISH ALL SKIP: total positions {len(all_positions)} >= limit {max_count}")
            return

        position_map = {}
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        stop_threshold = cfg.get("replenish_stop_threshold", 0)

        max_new = max_count - len(all_positions) if max_count > 0 else None
        tasks = []
        skip = set(cfg.get('skip_symbols', []))
        for sym in symbols:
            if sym in skip:
                continue
            for side in sides:
                if max_new is not None and len(tasks) >= max_new:
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

    async def _detect_liquidations(self, all_positions: list[dict]):
        """Detect positions that vanished from exchange (likely liquidated).

        Compares DB open_positions against live exchange positions.  Any position
        in DB but not on exchange was closed externally — check Binance force
        orders to confirm it was a liquidation.
        """
        exchange_keys = {f"{self.client.user_symbol(p['symbol'])}:{p.get('side')}" for p in all_positions}
        db_positions = self.db.get_open_positions()
        db_keys = {f"{sp['symbol']}:{sp['side']}" for sp in db_positions}
        vanished = db_keys - exchange_keys

        if not vanished:
            return

        # Cache: track last fetch time to avoid hammering API within same tick/batch
        now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if not hasattr(self, '_last_liq_fetch_ts'):
            self._last_liq_fetch_ts = 0
            self._liq_cache: list[dict] = []

        # Only re-fetch if last fetch was > 30 seconds ago
        if now_ts - self._last_liq_fetch_ts > 30000:
            self._liq_cache = await self.client.fetch_liquidations(since_minutes=10)
            self._last_liq_fetch_ts = now_ts

        # Build lookup from force orders: (symbol, side) -> liquidation info
        force_map = {}
        for fo in self._liq_cache:
            key = f"{fo['symbol']}:{fo['side']}"
            # Keep the earliest (or most recent) match
            if key not in force_map:
                force_map[key] = fo

        for key in vanished:
            sym, side = key.split(":", 1)
            db_pos = next((sp for sp in db_positions if sp['symbol'] == sym and sp['side'] == side), None)
            if not db_pos:
                self.db.remove_open(sym, side)
                continue

            entry_price = float(db_pos.get('entry_price', 0) or 0)
            amount = float(db_pos.get('amount', 0) or 0)

            fo = force_map.get(key)
            if fo:
                # Calculate PnL from entry vs liquidation price
                market = self.client.get_market_info(sym)
                cs = float(market.get("contractSize", 1) or 1)
                liq_price = fo.get("avgPrice", 0)
                if liq_price > 0 and entry_price > 0:
                    if side == "long":
                        pnl = (liq_price - entry_price) * amount * cs
                    else:
                        pnl = (entry_price - liq_price) * amount * cs
                else:
                    pnl = fo.get("pnl", 0)

                batch_id = fo.get("time", "")[:16]  # group by minute
                self.db.record_liquidation(
                    batch_id=batch_id,
                    symbol=sym,
                    side=side.upper(),
                    orig_qty=amount,
                    avg_price=liq_price,
                    executed_qty=fo.get("executedQty", amount),
                    pnl=pnl,
                    time_str=fo.get("time", ""),
                )
                self.db.remove_open(sym, side)
                self._system_pos_map.pop(key, None)
                log.warning(
                    f"LIQUIDATION DETECTED: {sym} {side} qty={amount} "
                    f"entry={entry_price:.6f} liq_price={liq_price:.6f} pnl={pnl:.4f}"
                )
            else:
                # Vanished from exchange but no force order found — could be
                # a stale DB entry or exchange API inconsistency. Clean up.
                log.warning(f"VANISHED POSITION (not in force orders): {sym} {side}, removing from DB")
                self.db.remove_open(sym, side)
                self._system_pos_map.pop(key, None)

    async def _do_open(self, symbol: str, open_side: str, side: str):
        if self._is_skipped_2027(symbol):
            return
        result = await self.client.safe_open(symbol, open_side)
        if result:
            self._clear_2027_failure(symbol)
            market = self.client.get_market_info(symbol)
            contract_size = market.get("contractSize", 1) or 1
            open_fee = result["average"] * result["amount"] * contract_size * 0.0005
            self.db.record_open(
                symbol=symbol, side=side,
                order_id=result["order_id"], entry_price=result["average"],
                amount=result["amount"], open_fee=open_fee,
            )
        else:
            self._record_2027_failure(symbol)
            s = _throttle_warn.emit(f"do_open_fail:{symbol}:{side}")
            if s is not None:
                log.warning(f"_do_open failed: {symbol} {side} ({open_side}){s}")

    # ========== Record trade ==========

    async def _record_trade(self, symbol, side, entry_price, contracts, close_result, trade_type, open_fee=0, open_time=""):
        try:
            market = self.client.get_market_info(symbol)
            contract_size = market.get("contractSize", 1) or 1

            exit_price = float(close_result.get("average", 0) or 0)
            raw_pnl = float(close_result.get("closedPnL", 0) or 0)
            if raw_pnl == 0 and exit_price > 0:
                if side == "long":
                    raw_pnl = (exit_price - entry_price) * contracts * contract_size
                else:
                    raw_pnl = (entry_price - exit_price) * contracts * contract_size
            pnl = raw_pnl
            position_value = self._position_value(entry_price, contracts, contract_size)
            pnl_rate = self._pnl_rate(pnl, position_value)

            close_fee = exit_price * contracts * contract_size * 0.0005
            if open_fee <= 0:
                open_fee = entry_price * contracts * contract_size * 0.0005
            fee = open_fee + close_fee

            now = datetime.now(timezone.utc).isoformat()
            self.db.insert_trade({
                "symbol": symbol,
                "side": side,
                "type": trade_type,
                "open_time": open_time,
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
