import asyncio
import gc
import logging
import signal
import os
import sys
import glob
import time
import logging.handlers
import fcntl
from datetime import datetime, timezone, timedelta
from aiohttp import web

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("data", exist_ok=True)

from exchange_client import ExchangeClient
from database import Database
from trader import Trader
from web_server import WebServer


# Default runtime configuration. DB is the source of truth; this is only used
# to seed an empty database on first run.
DEFAULT_CONFIG = {
    "exchange": "binanceusdm",
    "side": "both",
    "symbols": [
        "1000PEPEUSDT", "ADAUSDT", "AEROUSDT", "AGTUSDT", "AIOUSDT", "ALLOUSDT",
        "ARBUSDT", "ASTERUSDT", "BABYUSDT", "BLESSUSDT", "BRUSDT", "BSBUSDT",
        "CHIPUSDT", "CHZUSDT", "CLOUSDT", "CRVUSDT", "DOGEUSDT", "DOTUSDT",
        "ENAUSDT", "EVAAUSDT", "FETUSDT", "FILUSDT", "IDUSDT", "JCTUSDT",
        "JELLYJELLYUSDT", "JTOUSDT", "LITUSDT", "MAGMAUSDT", "MEGAUSDT",
        "NOTUSDT", "ONDOUSDT", "OPGUSDT", "OPNUSDT", "PENGUUSDT", "PIPPINUSDT",
        "PLAYUSDT", "PUMPUSDT", "RIFUSDT", "SAHARAUSDT", "SENTUSDT", "SIRENUSDT",
        "SKYAIUSDT", "SPACEUSDT", "SPXUSDT", "STGUSDT", "SUIUSDT", "TIAUSDT",
        "TONUSDT", "TRADOORUSDT", "TRIAUSDT", "TRUMPUSDT", "TRXUSDT",
        "VELVETUSDT", "WLDUSDT", "WLFIUSDT", "XLMUSDT", "XPLUSDT", "XRPUSDT",
        "ZKCUSDT", "ZROUSDT"
    ],
    "volume_threshold": 0,
    "price_threshold": 0,
    "symbol_refresh_interval": 86400,
    "position_check_interval": 3,
    "profit_tiers": [
        {"threshold": 0.002, "close_pct": 0.3},
        {"threshold": 0.01, "close_pct": 0.5},
        {"threshold": 0.05, "close_pct": 1.0},
        {"threshold": 0.4, "close_pct": 0.5},
        {"threshold": 0.5, "close_pct": 1.0}
    ],
    "replenish_stop_threshold": 0.3,
    "max_position_count": 150,
    "enable_all_close": True,
    "all_close_threshold": 0.0015,
    "enable_margin_call": True,
    "margin_call_threshold_long": 0.25,
    "margin_call_threshold_short": 0.25,
    "margin_call_multiplier": 2,
    "enable_single_pair_close": True,
    "pair_close_threshold": 0.002,
    "skip_symbols": []
}


PID_FILE = "data/evoclaw.pid"


def _acquire_pid_lock():
    """Prevent multiple EvoClaw processes from running simultaneously."""
    try:
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        return fd
    except (OSError, IOError):
        print("ERROR: Another EvoClaw instance is already running. Exiting.", file=sys.stderr)
        sys.exit(1)


def _release_pid_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.remove(PID_FILE)
    except Exception:
        pass


class ErrorLogFilter(logging.Filter):
    """Only allow WARNING and above (errors) to pass through."""
    def filter(self, record):
        return record.levelno >= logging.WARNING


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove any pre-existing handlers (avoid duplicates on restart)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # --- stdout (all levels) ---
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # --- Main trader log (INFO+, rotating by size) ---
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trader.log")
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=3 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # --- Error-only log (WARNING+, rotating by size) ---
    err_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "errors.log")
    eh = logging.handlers.RotatingFileHandler(
        err_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    eh.setFormatter(fmt)
    eh.addFilter(ErrorLogFilter())
    root.addHandler(eh)

    _cleanup_stale_logs()


def _cleanup_stale_logs():
    """Remove stale/oversized log files beyond backup limits."""
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    max_bytes = 3 * 1024 * 1024
    max_keep = 5

    # Legacy .bak files
    for f in sorted(glob.glob(os.path.join(data_dir, "trader.log.*.bak"))):
        try:
            os.remove(f)
        except Exception:
            pass

    # Numbered rotation files exceeding backupCount
    numbered = sorted(glob.glob(os.path.join(data_dir, "trader.log.[0-9]*")))
    for f in numbered[:-max_keep] if len(numbered) > max_keep else []:
        try:
            os.remove(f)
        except Exception:
            pass

    # Numbered error logs exceeding backupCount
    err_numbered = sorted(glob.glob(os.path.join(data_dir, "errors.log.[0-9]*")))
    for f in err_numbered[:-3] if len(err_numbered) > 3 else []:
        try:
            os.remove(f)
        except Exception:
            pass

    # Any remaining oversized log files (> 2x maxBytes)
    for f in sorted(glob.glob(os.path.join(data_dir, "*.log*"))):
        try:
            if os.path.getsize(f) > max_bytes * 2:
                os.remove(f)
        except Exception:
            pass


def get_sides(side: str) -> list[str]:
    if side == "both":
        return ["long", "short"]
    return [side]


async def main():
    # --- Startup GC to clean import residue ---
    gc.collect()

    setup_logging()
    log = logging.getLogger(__name__)

    db = Database("data/evoclaw.db")

    # Seed an empty database with built-in defaults on first run.
    # After seeding, the database is the sole source of truth.
    is_new = db.seed_config(DEFAULT_CONFIG)
    if is_new:
        log.info("Database seeded with default config")

    # Load runtime config from DB
    cfg = db.load_config()
    log.info(f"Config loaded from DB: {len(cfg.get('symbols', []))} symbols, exchange_kwargs={'present' if cfg.get('exchange_kwargs') else 'MISSING'}")

    client = ExchangeClient(cfg)
    try:
        await client.load_markets()
    except Exception as e:
        log.warning(f"load_markets() failed (will retry on next API call): {e}")
        # Don't crash — the exchange will lazy-load markets on first use

    # Backfill historical liquidations (last 7 days)
    try:
        existing = db.conn.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0]
        if existing == 0:
            log.info("Backfilling liquidation history (7 days)...")
            liq_records = await client.fetch_liquidations(since_minutes=10080, with_pnl=True)
            if liq_records:
                batch_records = []
                for r in liq_records:
                    batch_id = r.get("time", "")[:16]
                    batch_records.append({
                        "batch_id": batch_id, "symbol": r["symbol"], "side": r["side"],
                        "orig_qty": r["origQty"], "avg_price": r["avgPrice"],
                        "executed_qty": r["executedQty"], "pnl": r["pnl"], "time": r["time"],
                    })
                db.record_liquidations_batch(batch_records)
                stats = db.get_liquidation_stats()
                log.info(f"Backfilled {len(batch_records)} liquidation records from {stats['event_count']} events, total PnL={stats['total_pnl']:.2f}")
        else:
            log.info(f"Liquidation table already has {existing} records, skipping backfill")
    except Exception as e:
        log.warning(f"Liquidation backfill failed (non-fatal): {e}")

    # === Startup DB repair: sync open_positions amounts with exchange ===
    try:
        db_positions = db.get_open_positions()
        if db_positions:
            ex_positions = await client.get_positions()
            ex_map = {}
            for p in ex_positions:
                sym = client.user_symbol(p["symbol"])
                side = p.get("side")
                ex_map[f"{sym}:{side}"] = float(p.get("contracts", 0) or 0)

            repaired = 0
            removed = 0
            for sp in db_positions:
                key = f"{sp['symbol']}:{sp['side']}"
                ex_amt = ex_map.get(key)
                if ex_amt is None or ex_amt <= 0:
                    db.remove_open(sp['symbol'], sp['side'])
                    removed += 1
                elif abs(sp['amount'] - ex_amt) > 1:
                    db.update_open_amount(sp['symbol'], sp['side'], ex_amt)
                    repaired += 1

            if repaired > 0 or removed > 0:
                log.info(f"DB repair: {repaired} amounts synced, {removed} stale entries removed")
    except Exception as e:
        log.warning(f"DB repair failed (non-fatal): {e}")

    trader = Trader(client, db)
    server = WebServer(client, db, trader=trader)

    # === Phase 3: Health check endpoint ===
    async def health_handler(request):
        now = time.monotonic()
        price_age = now - trader._last_price_ok if trader._last_price_ok > 0 else None
        trader_alive = trader.running and (price_age is None or price_age < 300)
        return web.json_response({
            "status": "ok" if trader_alive else "degraded",
            "trader_running": trader.running,
            "last_price_ok_seconds_ago": round(price_age, 1) if price_age else None,
            "price_fail_streak": trader._price_fail_streak,
            "open_positions": len(trader._system_pos_map),
            "skipped_2027_count": len(trader._fail2027_skipped_at),
            "mc_cooldown_count": len(trader._mc_last_success),
            "mc_fail_streaks": dict(trader._mc_fail_streak),
        })

    server.app.router.add_get("/api/health", health_handler)

    # Dynamic symbol selection based on volume & price thresholds
    volume_threshold = cfg.get("volume_threshold", 0)
    price_threshold = cfg.get("price_threshold", 0)
    if volume_threshold == 0 and price_threshold == 0:
        symbols = cfg.get("symbols", [])
    else:
        symbols = await client.get_candidate_symbols(volume_threshold, price_threshold)
    log.info(f"Active symbols: {len(symbols)} (volume>={volume_threshold}, price<={price_threshold})")

    if symbols:
        try:
            await client.refresh_prices(symbols)
        except Exception as e:
            log.warning(f"Initial price refresh failed (non-fatal): {e}")

    # Open initial positions - track existing ones, open missing ones
    sides = get_sides(cfg.get("side", "both"))
    positions_ok = False
    if symbols:
        try:
            positions = await client.get_positions()
            positions_ok = True
        except Exception as e:
            log.warning(f"Initial get_positions() failed (non-fatal): {e}")
            positions = []

        for p in positions:
            sym = client.user_symbol(p["symbol"])
            pos_side = p.get("side", "")
            if sym in symbols and pos_side in sides:
                entry_price = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                if contracts > 0:
                    market = client.get_market_info(sym)
                    cs = market.get("contractSize", 1) or 1
                    open_fee = entry_price * contracts * cs * 0.0005
                    db.record_open(
                        symbol=sym, side=pos_side,
                        order_id="startup",
                        entry_price=entry_price,
                        amount=contracts,
                        open_fee=open_fee,
                        max_slots=int(cfg.get('matrix_slots', 100)),
                    )
                    log.info(f"Tracking existing position: {sym} {pos_side} {contracts} @ {entry_price}")

        tracked = set()
        for sp in db.get_open_positions():
            tracked.add(f"{sp['symbol']}:{sp['side']}")

        position_map = {}
        for p in positions:
            sym = client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        stop_threshold = cfg.get("replenish_stop_threshold", 0)
        max_count = cfg.get("max_position_count", 0)

        if not positions_ok:
            log.warning("STARTUP OPEN SKIP: cannot fetch live positions, refusing to open missing positions to avoid duplicates")
            open_tasks = []
        elif max_count > 0 and len(positions) >= max_count:
            log.info(f"STARTUP OPEN SKIP: total positions {len(positions)} >= limit {max_count}")
            open_tasks = []
        else:
            max_new = max_count - len(positions) if max_count > 0 else None
            open_tasks = []
            for sym in symbols:
                for side in sides:
                    if max_new is not None and len(open_tasks) >= max_new:
                        log.info(f"STARTUP OPEN LIMIT: stop at {max_count} positions")
                        break
                    key = f"{sym}:{side}"
                    if key not in tracked:
                        if client.should_stop_replenish(sym, side, stop_threshold, position_map):
                            continue
                        open_side = "buy" if side == "long" else "sell"
                        open_tasks.append((sym, open_side, side))
                if max_new is not None and len(open_tasks) >= max_new:
                    break

        if open_tasks:
            log.info(f"Opening {len(open_tasks)} missing positions")
            for sym, open_side, side in open_tasks:
                try:
                    result = await client.safe_open(sym, open_side)
                except Exception as e:
                    log.warning(f"safe_open failed for {sym} {side} (non-fatal): {e}")
                    continue
                if result:
                    market = client.get_market_info(sym)
                    contract_size = market.get("contractSize", 1) or 1
                    open_fee = result["average"] * result["amount"] * contract_size * 0.0005
                    db.record_open(
                        symbol=sym, side=side,
                        order_id=result["order_id"],
                        entry_price=result["average"],
                        amount=result["amount"],
                        open_fee=open_fee,
                        max_slots=int(cfg.get('matrix_slots', 100)),
                    )
        else:
            log.info("All positions already exist, skipping open")

    # Start web server
    runner = web.AppRunner(server.app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Web server started on :8080")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, trader.stop)

    try:
        await trader.run()
    except Exception as e:
        log.error(f"Fatal error in trader loop: {e}")
    finally:
        await client.close()
        await runner.cleanup()
        db.close()
        log.info("Shutdown complete")


if __name__ == "__main__":
    pid_fd = _acquire_pid_lock()
    try:
        asyncio.run(main())
    finally:
        _release_pid_lock(pid_fd)
