import asyncio
import json
import logging
import math
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
        self._fail2027_counts: dict[str, int] = {}
        self._fail2027_max = 5
        self._fail2027_skipped_at: dict[str, float] = {}
        self._fail2027_retry_after = 600
        self._system_pos_map: dict[str, dict] = {}
        self._tier_executed: dict[str, int] = {}

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
        tick_count = 0
        interval = self._get_config().get("position_check_interval", 1)
        log.info("Trader started")
        while self.running:
            try:
                await self.tick()
                tick_count += 1
                if tick_count % 60 == 0:
                    self.db.checkpoint_restart()
                if tick_count % 300 == 0:
                    self._cleanup_stale_2027()
                # Re-read interval occasionally in case config changed
                if tick_count % 100 == 0:
                    interval = self._get_config().get("position_check_interval", 1)
            except Exception as e:
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
            log.warning(f"CIRCUIT BREAKER: skipping {symbol} after {self._fail2027_max} consecutive -2027 failures")

    def _cleanup_stale_2027(self):
        for sym in list(self._fail2027_counts.keys()):
            if sym in self._fail2027_skipped_at:
                continue
            if self._fail2027_counts[sym] < self._fail2027_max:
                del self._fail2027_counts[sym]

    def _clear_2027_failure(self, symbol: str):
        if symbol in self._fail2027_counts:
            del self._fail2027_counts[symbol]

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

        await self.client.refresh_prices(candidate_symbols)

        # STEP 1: Fetch positions ONCE
        all_positions = await self.client.get_positions()
        exchange_positions = [p for p in all_positions if self.client.user_symbol(p["symbol"]) in candidate_symbols]
        did_change = False

        # Build system position lookup cache
        self._system_pos_map = {}
        for sp in self.db.get_open_positions():
            self._system_pos_map[f"{sp['symbol']}:{sp['side']}"] = sp

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

        self.db.checkpoint()

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

                await self._record_trade(
                    symbol=sym, side=pos_side,
                    entry_price=t["entry_price"], contracts=t["contracts"],
                    close_result=result, trade_type="all_close", open_fee=open_fee,
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
                    continue
                log.info(f"TIER CLOSE {symbol} {pos_side}: tier={i+1} pnl={unrealized_pnl:.4f} rate={profit_rate:.4%} close={close_contracts}/{contracts}")
                result = await self.client.close_position(symbol, pos_side, close_contracts)
                if result:
                    open_fee = 0
                    sp = self._system_pos_map.get(pos_key)
                    if sp:
                        open_fee = sp.get("open_fee", 0)
                    if close_contracts >= contracts:
                        self.db.remove_open(symbol, pos_side)
                        self._system_pos_map.pop(pos_key, None)
                    await self._record_trade(
                        symbol=symbol, side=pos_side,
                        entry_price=entry_price, contracts=close_contracts,
                        close_result=result, trade_type="single", open_fee=open_fee,
                    )
                    self._tier_executed[pos_key] = i
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
                        await self._record_trade(
                            symbol=sym, side=side,
                            entry_price=entry_map[side], contracts=contracts_map[side],
                            close_result=result, trade_type="pair_close", open_fee=open_fee,
                        )
                closed_any = True
        return closed_any

    # ========== Margin call ==========

    async def check_margin_call(self, all_positions, skip) -> bool:
        """Return True if any margin call was executed."""
        cfg = self.config
        threshold_long = cfg.get("margin_call_threshold_long", cfg.get("margin_call_threshold", 0.01))
        threshold_short = cfg.get("margin_call_threshold_short", cfg.get("margin_call_threshold", 0.01))
        multiplier = cfg.get("margin_call_multiplier", 2)

        executed = False
        for p in all_positions:
            sym = self.client.user_symbol(p["symbol"])
            if sym in skip:
                continue
            side = p.get("side")

            pnl = float(p.get("unrealizedPnl", 0) or 0)
            if pnl >= 0:
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
                    self.db.increment_margin_call_count()
                    new_total = contracts + add_amount
                    added_fee = result["average"] * add_amount * cs * 0.0005
                    if self.db.has_open(sym, side):
                        self.db.mark_margin_called(sym, side, new_total, added_fee)
                    else:
                        open_fee = entry * contracts * cs * 0.0005 + added_fee
                        self.db.record_open(sym, side, "margin_call", entry, new_total, open_fee)
                    executed = True
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
            log.warning(f"_do_open failed: {symbol} {side} ({open_side})")

    # ========== Record trade ==========

    async def _record_trade(self, symbol, side, entry_price, contracts, close_result, trade_type, open_fee=0):
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
