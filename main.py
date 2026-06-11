import asyncio
import gc
import logging
import signal
import os
import sys
import json
import glob
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


def load_config():
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except Exception:
        return {"exchange": "binance", "exchange_kwargs": {}}


def get_sides(side: str) -> list[str]:
    if side == "both":
        return ["long", "short"]
    return [side]


async def main():
    # --- Startup GC to clean import residue ---
    gc.collect()

    setup_logging()
    log = logging.getLogger(__name__)
    cfg = load_config()

    db = Database("data/evoclaw.db")
    client = ExchangeClient(cfg)
    await client.load_markets()

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

    trader = Trader(client, db, "config.json")
    server = WebServer(client, db, "config.json", trader=trader)

    # Dynamic symbol selection based on volume & price thresholds
    volume_threshold = cfg.get("volume_threshold", 0)
    price_threshold = cfg.get("price_threshold", 0)
    if volume_threshold == 0 and price_threshold == 0:
        symbols = cfg.get("symbols", [])
    else:
        symbols = await client.get_candidate_symbols(volume_threshold, price_threshold)
    log.info(f"Active symbols: {len(symbols)} (volume>={volume_threshold}, price<={price_threshold})")

    if symbols:
        await client.refresh_prices(symbols)

    # Open initial positions - track existing ones, open missing ones
    sides = get_sides(cfg.get("side", "both"))
    if symbols:
        positions = await client.get_positions()

        for p in positions:
            sym = client.user_symbol(p["symbol"])
            pos_side = p.get("side", "")
            if sym in symbols and pos_side in sides:
                entry_price = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                if contracts > 0:
                    open_fee = entry_price * contracts * 0.0005
                    db.record_open(
                        symbol=sym, side=pos_side,
                        order_id="startup",
                        entry_price=entry_price,
                        amount=contracts,
                        open_fee=open_fee,
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

        if max_count > 0 and len(positions) >= max_count:
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
                result = await client.safe_open(sym, open_side)
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

    await trader.run()
    # Graceful shutdown: close exchange connections to prevent Unclosed connector errors
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
