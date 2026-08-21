import asyncio
import json
import logging
import os
import shutil
import time
from aiohttp import web

log = logging.getLogger(__name__)

# ---- Static file cache (loaded once, never changes while running) ----
_static_cache = {}


def _get_static(path):
    """Read a static file once and cache it in memory."""
    if path not in _static_cache:
        with open(path, "r", encoding="utf-8") as f:
            _static_cache[path] = f.read()
    return _static_cache[path]


class WebServer:
    def __init__(self, exchange_client, database, trader=None):
        self.client = exchange_client
        self.db = database
        self.trader = trader
        self.app = web.Application()
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/intro.html", self.handle_intro)
        self.app.router.add_get("/api/config", self.api_config_get)
        self.app.router.add_post("/api/config", self.api_config_set)
        self.app.router.add_get("/api/account", self.api_account_cached)
        self.app.router.add_get("/api/positions", self.api_positions_cached)
        self.app.router.add_get("/api/positions-map", self.api_positions_map_cached)
        self.app.router.add_get("/api/stats", self.api_stats_cached)
        self.app.router.add_get("/api/trades", self.api_trades)
        self.app.router.add_get("/api/daily-trades", self.api_daily_trades)
        self.app.router.add_get("/api/symbol-summary", self.api_symbol_summary)
        self.app.router.add_get("/api/system", self.api_system)
        self.app.router.add_get("/api/profit-trend", self.api_profit_trend_cached)
        self.app.router.add_get("/api/liquidations", self.api_liquidations)
        self.app.router.add_post("/api/refresh-symbols", self.api_refresh_symbols)
        self.app.router.add_get("/web/config.json", self.handle_web_config)
        self.app.router.add_get("/api/web-config", self.api_web_config_get)
        self.app.router.add_post("/api/web-config", self.api_web_config_set)
        self._last_cpu = None
        self._last_net = None
        self._last_system_time = None
        # Response cache: stores only pure dict data, never Response objects
        self._api_cache: dict[str, tuple[float, dict]] = {}
        self._api_cache_ttl = 15
        self._api_cache_max = 20
        self._balance_cache = (0, 0.0)
        self._system_cache = (0, None)
        self._start_time = time.time()

    def _load_config(self):
        """Load config from database (source of truth for ALL settings including exchange_kwargs)."""
        try:
            return self.db.load_config()
        except Exception:
            return {}

    async def _cached_response(self, key, handler, request):
        """Return cached response if within TTL, otherwise call handler and cache.
        Caches only pure dict data to prevent memory leaks from Response objects."""
        now = time.monotonic()
        cached = self._api_cache.get(key)
        if cached and now - cached[0] < self._api_cache_ttl:
            return web.json_response(cached[1])

        try:
            resp = await asyncio.wait_for(handler(request), timeout=15)
        except asyncio.TimeoutError:
            log.warning(f"Handler timeout for {key}")
            return web.json_response({"status": "error", "message": "Request timeout"}, status=504)

        # Extract pure dict data from response to avoid caching Response objects
        try:
            if hasattr(resp, 'body') and resp.body:
                data = json.loads(resp.body)
            else:
                data = resp
        except Exception:
            data = resp

        # Only cache successful dict data (not error/500 responses)
        if isinstance(data, dict) and data.get("status") != "error":
            if len(self._api_cache) >= self._api_cache_max:
                oldest = min(self._api_cache, key=lambda k: self._api_cache[k][0])
                del self._api_cache[oldest]
            self._api_cache[key] = (now, data)

        return resp

    async def _db_sync(self, fn, *args, **kwargs):
        """Run a synchronous DB call in a thread pool to avoid blocking the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def api_account_cached(self, request):
        return await self._cached_response("account", self.api_account, request)

    async def api_positions_cached(self, request):
        return await self._cached_response("positions", self.api_positions, request)

    async def api_positions_map_cached(self, request):
        return await self._cached_response("positions_map", self.api_positions_map, request)

    async def api_stats_cached(self, request):
        return await self._cached_response("stats", self.api_stats, request)

    async def api_profit_trend_cached(self, request):
        period = request.query.get("period", "hour")
        return await self._cached_response(f"profit_trend_{period}", self.api_profit_trend, request)

    async def handle_index(self, request):
        web_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
        content = _get_static(web_path)
        return web.Response(text=content, content_type="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        })

    async def handle_intro(self, request):
        web_path = os.path.join(os.path.dirname(__file__), "web", "intro.html")
        content = _get_static(web_path)
        return web.Response(text=content, content_type="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        })

    async def handle_web_config(self, request):
        """Legacy endpoint — reads from DB (file was migrated on startup)."""
        cfg = await self._db_sync(self.db.load_web_config)
        return web.json_response(cfg)

    async def api_web_config_get(self, request):
        try:
            cfg = await self._db_sync(self.db.load_web_config)
            return web.json_response(cfg)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_web_config_set(self, request):
        try:
            data = await request.json()
            await self._db_sync(self.db.save_web_config, data)
            return web.json_response({"status": "ok", "message": "Frontend config saved to database"})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=400)

    async def api_config_get(self, request):
        cfg = self._load_config()
        safe = cfg.copy()
        kwargs = safe.get("exchange_kwargs", {})
        safe["exchange_kwargs"] = {
            "apiKey": kwargs.get("apiKey", "")[:6] + "..." if kwargs.get("apiKey") else "",
            "secret": "***",
        }
        return web.json_response(safe)

    async def api_config_set(self, request):
        try:
            body = await request.json()
            existing = self._load_config()
            kwargs = existing.get("exchange_kwargs", {})

            # Phase 4: Handle exchange_kwargs updates via DB
            if "exchange_kwargs" in body:
                if body["exchange_kwargs"].get("apiKey"):
                    kwargs["apiKey"] = body["exchange_kwargs"]["apiKey"]
                if body["exchange_kwargs"].get("secret"):
                    kwargs["secret"] = body["exchange_kwargs"]["secret"]

            # Build the full config to save
            for k, v in body.items():
                if k == "exchange_kwargs":
                    continue
                existing[k] = v
            existing["exchange_kwargs"] = kwargs

            # Phase 4: Save everything to DB (including exchange_kwargs)
            await self._db_sync(self.db.save_config, existing)

            return web.json_response({"status": "ok", "message": "Config saved to database"})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=400)

    async def api_account(self, request):
        try:
            balance = await self.client.get_balance()
            all_positions = await self.client.get_positions()

            unrealized = 0.0
            long_pos_count = 0
            short_pos_count = 0
            total_pnl = 0.0
            total_value = 0.0
            worst_pnl = 0.0
            worst_rate = 0.0
            current_max_loss_rate = 0.0
            current_max_loss_pnl = 0.0
            sym_set = set()
            sym_total_pnl = 0.0
            sym_total_value = 0.0

            if self.trader and self.trader._candidate_symbols:
                symbols = self.trader._candidate_symbols
                sym_set = set(symbols)
            else:
                cfg = self._load_config()
                volume_threshold = cfg.get("volume_threshold", 0)
                price_threshold = cfg.get("price_threshold", 0)
                if volume_threshold == 0 and price_threshold == 0:
                    symbols = cfg.get("symbols", [])
                    sym_set = set(symbols)
                else:
                    symbols = await self.client.get_candidate_symbols(volume_threshold, price_threshold)
                    sym_set = set(symbols)

            for p in all_positions:
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                side = p.get("side")
                market = self.client.get_market_info(p["symbol"])
                cs = market.get("contractSize", 1) or 1
                val = entry * contracts * cs
                rate = pnl / val if val > 0 else 0

                unrealized += pnl
                if side == "long":
                    long_pos_count += 1
                elif side == "short":
                    short_pos_count += 1
                total_pnl += pnl
                total_value += val
                if rate < current_max_loss_rate:
                    current_max_loss_rate = rate
                if pnl < current_max_loss_pnl:
                    current_max_loss_pnl = pnl
                if pnl < worst_pnl:
                    worst_pnl = pnl
                if rate < worst_rate:
                    worst_rate = rate

                if sym_set:
                    user_sym = self.client.user_symbol(p["symbol"])
                    if user_sym in sym_set:
                        sym_total_pnl += pnl
                        sym_total_value += val

            total_pnl_rate = total_pnl / total_value if total_value > 0 else 0
            realtime_rate = sym_total_pnl / sym_total_value if sym_total_value > 0 else 0

            hist_max_loss_rate = await self._db_sync(self.db.get_runtime_stat, "max_position_loss_rate", 0)
            if current_max_loss_rate < hist_max_loss_rate:
                hist_max_loss_rate = current_max_loss_rate
                await self._db_sync(self.db.set_runtime_stat, "max_position_loss_rate", hist_max_loss_rate)
            hist_max_loss_pnl = await self._db_sync(self.db.get_runtime_stat, "max_position_loss_pnl", 0)
            if current_max_loss_pnl < hist_max_loss_pnl:
                hist_max_loss_pnl = current_max_loss_pnl
                await self._db_sync(self.db.set_runtime_stat, "max_position_loss_pnl", hist_max_loss_pnl)
            hist_total_loss_pnl = await self._db_sync(self.db.get_runtime_stat, "hist_total_loss_pnl", 0)
            hist_total_loss_rate = await self._db_sync(self.db.get_runtime_stat, "hist_total_loss_rate", 0)
            if total_pnl < hist_total_loss_pnl:
                hist_total_loss_pnl = total_pnl
                await self._db_sync(self.db.set_runtime_stat, "hist_total_loss_pnl", hist_total_loss_pnl)
            if total_pnl_rate < hist_total_loss_rate:
                hist_total_loss_rate = total_pnl_rate
                await self._db_sync(self.db.set_runtime_stat, "hist_total_loss_rate", hist_total_loss_rate)

            margin_call_count = int(await self._db_sync(self.db.get_runtime_stat, "margin_call_count", 0))
            liq_stats = await self._db_sync(self.db.get_liquidation_stats)

            self._balance_cache = (time.monotonic(), float(balance.get("balance", 0) or 0))
            return web.json_response({
                "balance": balance.get("balance", 0),
                "available_balance": round(balance.get("available_balance", 0), 2),
                "unrealized_pnl": round(unrealized, 4),
                "position_count": len(all_positions),
                "long_position_count": long_pos_count,
                "short_position_count": short_pos_count,
                "total_position_pnl": round(total_pnl, 4),
                "total_position_pnl_rate": round(total_pnl_rate, 6),
                "worst_position_pnl": round(worst_pnl, 4),
                "worst_position_rate": round(worst_rate, 6),
                "hist_worst_trade_pnl": round(hist_max_loss_pnl, 4),
                "hist_worst_trade_rate": round(hist_max_loss_rate, 6),
                "hist_total_loss_pnl": round(hist_total_loss_pnl, 4),
                "hist_total_loss_rate": round(hist_total_loss_rate, 6),
                "margin_call_count": margin_call_count,
                "realtime_profit_rate": round(realtime_rate, 6),
                "active_symbols": symbols,
                "held_symbols": sorted({self.client.user_symbol(p["symbol"]) for p in all_positions}),
                "liquidation_event_count": liq_stats["event_count"],
                "liquidation_total_pnl": liq_stats["total_pnl"],
                "liquidation_pairs_count": liq_stats["pairs_count"],
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_positions(self, request):
        try:
            cfg = self._load_config()
            symbols = cfg.get("symbols", [])
            positions = await self.client.get_positions(symbols if symbols else None)
            result = []
            for p in positions:
                entry_price = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                unrealized_pnl = float(p.get("unrealizedPnl", 0) or 0)
                market = self.client.get_market_info(p["symbol"])
                contract_size = market.get("contractSize", 1) or 1
                value = entry_price * contracts * contract_size
                pnl_rate = unrealized_pnl / value if value > 0 else 0
                result.append({
                    "symbol": p["symbol"],
                    "side": p.get("side", ""),
                    "contracts": contracts,
                    "entry_price": entry_price,
                    "unrealized_pnl": round(unrealized_pnl, 4),
                    "pnl_rate": round(pnl_rate, 6),
                    "position_value": round(value, 2),
                })
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_positions_map(self, request):
        try:
            all_positions = await self.client.get_positions()

            position_items = []
            for p in all_positions:
                sym = self.client.user_symbol(p["symbol"])
                side = p.get("side")
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                market = self.client.get_market_info(p["symbol"])
                cs = market.get("contractSize", 1) or 1
                val = entry * contracts * cs
                rate = pnl / val if val > 0 else 0
                position_items.append({
                    "symbol": sym,
                    "side": side,
                    "contracts": contracts,
                    "entry_price": entry,
                    "position_value": round(val, 2),
                    "pnl": round(pnl, 4),
                    "pnl_rate": round(rate, 6),
                })

            position_items.sort(key=lambda x: x["pnl_rate"])

            result = []
            for i, item in enumerate(position_items):
                result.append({"index": i, **item, "occupied": True})

            return web.json_response({"slots": result, "total_positions": len(all_positions), "max_slots": len(position_items)})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_stats(self, request):
        try:
            balance_data = await self.client.get_balance()
            balance = balance_data.get("balance", 0)
            stats = await self._db_sync(self.db.get_stats, balance)
            return web.json_response(stats)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_system(self, request):
        try:
            now = time.time()
            if self._system_cache[1] is not None and now - self._system_cache[0] < 5:
                return web.json_response(self._system_cache[1])

            cpu_percent = 0.0
            with open("/proc/stat") as f:
                line = f.readline()
            fields = list(map(int, line.split()[1:]))
            idle = fields[3]
            total = sum(fields)
            if self._last_cpu and self._last_system_time:
                dt_total = total - self._last_cpu[0]
                dt_idle = idle - self._last_cpu[1]
                if dt_total > 0:
                    cpu_percent = (dt_total - dt_idle) / dt_total * 100
            self._last_cpu = (total, idle)

            mem_total = mem_free = 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        mem_free = int(line.split()[1]) * 1024
            mem_used = mem_total - mem_free if mem_total else 0

            du = shutil.disk_usage("/")

            rx_bytes = tx_bytes = 0
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    parts = line.split()
                    if parts[0].endswith(":"):
                        iface = parts[0][:-1]
                        if iface != "lo":
                            rx_bytes += int(parts[1])
                            tx_bytes += int(parts[9])

            rx_speed = tx_speed = 0
            if self._last_net and self._last_system_time:
                dt = now - self._last_system_time
                if dt > 0:
                    rx_speed = (rx_bytes - self._last_net[0]) / dt
                    tx_speed = (tx_bytes - self._last_net[1]) / dt
            self._last_net = (rx_bytes, tx_bytes)
            self._last_system_time = now

            data = {
                "uptime_seconds": round(time.time() - self._start_time),
                "cpu_percent": round(cpu_percent, 1),
                "mem_total": mem_total,
                "mem_used": mem_used,
                "mem_percent": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                "disk_total": du.total,
                "disk_used": du.used,
                "disk_percent": round(du.used / du.total * 100, 1) if du.total else 0,
                "net_rx_speed": round(rx_speed, 0),
                "net_tx_speed": round(tx_speed, 0),
            }
            self._system_cache = (now, data)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_refresh_symbols(self, request):
        try:
            symbols = await self.trader.refresh_symbols_now()
            return web.json_response({"status": "ok", "count": len(symbols), "symbols": symbols})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_profit_trend(self, request):
        try:
            from datetime import datetime, timezone, timedelta
            from collections import defaultdict

            period = request.query.get("period", "hour")
            cached_ts, cached_bal = self._balance_cache
            if cached_bal > 0 and time.monotonic() - cached_ts < 300:
                balance = cached_bal
            else:
                balance_data = await self.client.get_balance()
                balance = balance_data.get("balance", 0)
                self._balance_cache = (time.monotonic(), float(balance) if balance else 0.0)
            if balance <= 0:
                balance = 1

            now = datetime.now(timezone.utc)

            if period == "hour":
                buckets = []
                for i in range(24):
                    t = now - timedelta(hours=23 - i)
                    buckets.append((t.strftime("%Y-%m-%dT%H"), t.strftime("%H:00")))
                start_iso = (now - timedelta(hours=24)).isoformat()
            elif period == "day":
                buckets = []
                for i in range(30):
                    t = now - timedelta(days=29 - i)
                    buckets.append((t.strftime("%Y-%m-%d"), t.strftime("%m-%d")))
                start_iso = (now - timedelta(days=30)).isoformat()
            elif period == "week":
                buckets = []
                for i in range(12):
                    t = now - timedelta(weeks=11 - i)
                    week_start = t - timedelta(days=t.weekday())  # Monday of that week
                    buckets.append((week_start.strftime("%Y-%m-%d"), week_start.strftime("%m-%d")))
                start_iso = (now - timedelta(weeks=12)).isoformat()
            else:  # month
                buckets = []
                for i in range(12):
                    t = now - timedelta(days=365)
                    # Build year-month buckets stepping back month by month
                    y = (now.year * 12 + now.month - 1 - (11 - i)) // 12
                    m = (now.year * 12 + now.month - 1 - (11 - i)) % 12 + 1
                    buckets.append((f"{y:04d}-{m:02d}", f"{y:02d}-{m:02d}"))
                start_iso = (now - timedelta(days=400)).isoformat()

            def _fetch_trades():
                return self.db.conn.execute(
                    "SELECT close_time, pnl FROM trades WHERE close_time >= ? ORDER BY close_time",
                    (start_iso,),
                ).fetchall()
            rows = await self._db_sync(_fetch_trades)

            bucket_pnls = defaultdict(float)
            for close_time, pnl in rows:
                if period == "hour":
                    bucket_key = close_time[:13]
                elif period == "month":
                    bucket_key = close_time[:7]
                elif period == "week":
                    # Map close_time to its week-start (Monday) key
                    try:
                        dt = datetime.fromisoformat(close_time[:10])
                        ws = dt - timedelta(days=dt.weekday())
                        bucket_key = ws.strftime("%Y-%m-%d")
                    except Exception:
                        bucket_key = close_time[:10]
                else:  # day
                    bucket_key = close_time[:10]
                bucket_pnls[bucket_key] += float(pnl)

            cumulative = 0.0
            result = []
            for bucket_key, label in buckets:
                cumulative += bucket_pnls.get(bucket_key, 0.0)
                rate = cumulative / balance
                result.append({
                    "label": label,
                    "rate": round(rate, 6),
                    "cumulative_pnl": round(cumulative, 4),
                })

            return web.json_response({"period": period, "data": result})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_trades(self, request):
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
            total = await self._db_sync(self.db.get_total_trades)
            trades = await self._db_sync(self.db.get_recent_trades, limit, offset)
            return web.json_response({
                "trades": trades,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_daily_trades(self, request):
        """Today's open/margin/close counts by side + realized PnL from closes."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            day_prefix = now.strftime("%Y-%m-%d")

            open_stats = await self._db_sync(self.db.get_daily_open_stats, day_prefix)

            def _close_stats():
                return self.db.conn.execute(
                    """SELECT side, COUNT(*), COALESCE(SUM(pnl),0)
                       FROM trades WHERE close_time LIKE ?
                       GROUP BY side""",
                    (day_prefix + '%',),
                ).fetchall()
            rows = await self._db_sync(_close_stats)
            close = {"long": 0, "short": 0}
            pnl = {"long": 0.0, "short": 0.0}
            for side, cnt, p in rows:
                if side in close:
                    close[side] = cnt
                    pnl[side] = round(float(p), 4)

            return web.json_response({
                "date": day_prefix,
                "open": open_stats["open"],
                "margin": open_stats["margin"],
                "close": close,
                "pnl": pnl,
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_symbol_summary(self, request):
        """Per-symbol+side aggregation: opens, closes, remaining, pnl, fees."""
        try:
            open_rows = await self._db_sync(self.db.get_symbol_open_summary)
            opens = {f"{r['symbol']}:{r['side']}": r for r in open_rows}

            def _close_rows():
                return self.db.conn.execute(
                    """SELECT symbol, side, COUNT(*) as close_cnt,
                              COALESCE(SUM(amount),0) as close_qty,
                              COALESCE(SUM(amount*exit_price),0) as close_value,
                              COALESCE(SUM(pnl),0) as total_pnl,
                              COALESCE(SUM(fee),0) as close_fee,
                              ROUND(AVG(pnl_rate),6) as avg_rate
                       FROM trades GROUP BY symbol, side
                       ORDER BY total_pnl DESC""",
                ).fetchall()
            close_rows = await self._db_sync(_close_rows)

            def _open_remaining():
                return self.db.conn.execute(
                    """SELECT symbol, side, amount FROM open_positions""",
                ).fetchall()
            remaining = await self._db_sync(_open_remaining)
            remaining_map = {}
            for symbol, side, amt in remaining:
                remaining_map[f"{symbol}:{side}"] = float(amt)

            result = []
            seen = set()
            # Include current open positions so symbols with no close history yet
            # (e.g. opened before the opens table existed) still appear with remaining qty.
            all_keys = (set(opens.keys())
                        | {f"{r[0]}:{r[1]}" for r in close_rows}
                        | set(remaining_map.keys()))
            for key in all_keys:
                symbol, side = key.split(":", 1)
                if key in seen:
                    continue
                seen.add(key)
                op = opens.get(key, {})
                cr = next((r for r in close_rows if f"{r[0]}:{r[1]}" == key), None)

                open_cnt = op.get("open_count", 0) if op else 0
                open_qty = round(op.get("open_qty", 0), 4) if op else 0
                open_val = round(op.get("open_value", 0), 4) if op else 0
                margin_cnt = op.get("margin_count", 0) if op else 0
                rem_qty = round(remaining_map.get(key, 0), 4)

                close_cnt = cr[2] if cr else 0
                close_qty = round(cr[3], 4) if cr else 0
                close_val = round(cr[4], 4) if cr else 0
                total_pnl = round(cr[5], 4) if cr else 0
                close_fee = round(cr[6], 4) if cr else 0
                avg_rate = round(cr[7] * 100, 4) if cr else 0

                result.append({
                    "symbol": symbol,
                    "side": side,
                    "open_count": open_cnt,
                    "open_qty": open_qty,
                    "open_value": open_val,
                    "margin_count": margin_cnt,
                    "close_count": close_cnt,
                    "close_qty": close_qty,
                    "close_value": close_val,
                    "remaining_qty": rem_qty,
                    "total_pnl": total_pnl,
                    "close_fee": close_fee,
                    "avg_close_rate_pct": avg_rate,
                })

            return web.json_response({"data": result, "total": len(result)})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_liquidations(self, request):
        try:
            limit = int(request.query.get("limit", "20"))
            offset = int(request.query.get("offset", "0"))
            stats = await self._db_sync(self.db.get_liquidation_stats)
            top10 = await self._db_sync(self.db.get_liquidation_top10)
            events, total = await self._db_sync(self.db.get_liquidation_events, limit, offset)

            # For each event, get top 3 worst PnL symbols
            for evt in events:
                def _get_top3(batch_id):
                    return self.db.conn.execute(
                        """SELECT symbol, side, pnl FROM liquidations
                           WHERE batch_id = ? ORDER BY pnl ASC LIMIT 3""",
                        (batch_id,),
                    ).fetchall()

                top3_rows = await self._db_sync(_get_top3, evt["batch_id"])
                evt["top3"] = [f"{r[0]} {r[1]} {round(r[2], 2)}" for r in top3_rows]

            return web.json_response({
                "events": events,
                "total_events": total,
                "event_count": stats["event_count"],
                "total_pnl": stats["total_pnl"],
                "pairs_count": stats["pairs_count"],
                "total_qty": stats["total_qty"],
                "top10": top10,
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)
