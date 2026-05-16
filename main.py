import asyncio
import logging
import signal
import os
import json
import logging.handlers
from aiohttp import web

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("data", exist_ok=True)

from exchange_client import ExchangeClient
from database import Database
from trader import Trader
from web_server import WebServer


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.handlers.RotatingFileHandler(
        "data/trader.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


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
    setup_logging()
    log = logging.getLogger(__name__)
    cfg = load_config()

    db = Database("data/evoclaw.db")
    client = ExchangeClient(cfg)
    await client.load_markets()

    trader = Trader(client, db, "config.json")
    server = WebServer(client, db, "config.json", trader=trader)

    # Dynamic symbol selection based on volume & price thresholds
    volume_threshold = cfg.get("volume_threshold", 0)
    price_threshold = cfg.get("price_threshold", 0)
    # Fallback to legacy symbols if thresholds not configured
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
        # Get current exchange positions
        positions = await client.get_positions(symbols)

        # Track existing exchange positions in DB so system can manage them
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

        # Get DB-tracked positions to avoid duplicates
        tracked = set()
        for sp in db.get_open_positions():
            tracked.add(f"{sp['symbol']}:{sp['side']}")

        # Build position map for replenish stop-check
        position_map = {}
        for p in positions:
            sym = client.user_symbol(p["symbol"])
            side = p.get("side")
            position_map[f"{sym}:{side}"] = p

        stop_threshold = cfg.get("replenish_stop_threshold", 0)
        max_count = cfg.get("max_position_count", 0)

        # Check position count limit before any open
        if max_count > 0 and len(positions) >= max_count:
            log.info(f"STARTUP OPEN SKIP: total positions {len(positions)} >= limit {max_count}")
            open_tasks = []
        else:
            # Open only missing positions and record them
            open_tasks = []
            for sym in symbols:
                for side in sides:
                    key = f"{sym}:{side}"
                    if key not in tracked:
                        if client.should_stop_replenish(sym, side, stop_threshold, position_map):
                            continue
                        open_side = "buy" if side == "long" else "sell"
                        open_tasks.append((sym, open_side, side))

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
    db.close()
    log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
