import json
import logging
import os
import shutil
import time
from aiohttp import web

log = logging.getLogger(__name__)


class WebServer:
    def __init__(self, exchange_client, database, config_path: str = "config.json", trader=None):
        self.client = exchange_client
        self.db = database
        self.config_path = config_path
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
        self.app.router.add_get("/api/system", self.api_system)
        self.app.router.add_get("/api/profit-trend", self.api_profit_trend_cached)
        self.app.router.add_post("/api/refresh-symbols", self.api_refresh_symbols)
        self.app.router.add_get("/web/config.json", self.handle_web_config)
        self.app.router.add_get("/api/web-config", self.api_web_config_get)
        self.app.router.add_post("/api/web-config", self.api_web_config_set)
        # System metrics cache for rate calculations
        self._last_cpu = None
        self._last_net = None
        self._last_system_time = None
        # Response cache to reduce Binance API calls
        self._api_cache = {}
        self._api_cache_ttl = 15  # seconds: reduce request frequency
        self._balance_cache = (0, 0.0)  # (timestamp, balance)
        self._cache_lock = __import__('asyncio').Lock()
        self._system_cache = (0, None)  # (timestamp, data)

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    async def _cached_response(self, key, handler, request):
        """Return cached response if within TTL, otherwise call handler and cache.
        Uses a lock to prevent thundering herd on cache miss.
        Caches parsed JSON data instead of Response objects to avoid aiohttp reuse issues."""
        now = time.monotonic()
        cached = self._api_cache.get(key)
        if cached and now - cached[0] < self._api_cache_ttl:
            data = cached[1]
            if isinstance(data, dict):
                return web.json_response(data)
            return data
        async with self._cache_lock:
            # Double-check after acquiring lock
            cached = self._api_cache.get(key)
            if cached and now - cached[0] < self._api_cache_ttl:
                data = cached[1]
                if isinstance(data, dict):
                    return web.json_response(data)
                return data
            resp = await handler(request)
            # Extract JSON data from response body to avoid caching Response objects
            try:
                if hasattr(resp, 'body') and resp.body:
                    cached_data = json.loads(resp.body)
                else:
                    cached_data = resp
            except Exception:
                cached_data = resp
            self._api_cache[key] = (now, cached_data)
            return resp


    async def _db_sync(self, fn, *args, **kwargs):
        """Run a synchronous DB call in a thread pool to avoid blocking the event loop."""
        import asyncio
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
        return web.FileResponse(web_path)

    async def handle_intro(self, request):
        web_path = os.path.join(os.path.dirname(__file__), "web", "intro.html")
        return web.FileResponse(web_path)

    async def handle_web_config(self, request):
        config_path = os.path.join(os.path.dirname(__file__), "web", "config.json")
        if os.path.exists(config_path):
            return web.FileResponse(config_path)
        return web.json_response({"matrix_slots": 100, "matrix_columns": 10})

    async def api_web_config_get(self, request):
        try:
            config_path = os.path.join(os.path.dirname(__file__), "web", "config.json")
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    return web.json_response(json.load(f))
            return web.json_response({"matrix_slots": 100, "matrix_columns": 10})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_web_config_set(self, request):
        try:
            data = await request.json()
            config_path = os.path.join(os.path.dirname(__file__), "web", "config.json")
            existing = {}
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    existing = json.load(f)
            existing.update(data)
            with open(config_path, "w") as f:
                json.dump(existing, f, indent=4)
            return web.json_response({"status": "ok", "message": "Frontend config saved"})
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
            if "exchange_kwargs" in body:
                if body["exchange_kwargs"].get("apiKey"):
                    kwargs["apiKey"] = body["exchange_kwargs"]["apiKey"]
                if body["exchange_kwargs"].get("secret"):
                    kwargs["secret"] = body["exchange_kwargs"]["secret"]
            for k, v in body.items():
                if k == "exchange_kwargs":
                    continue
                existing[k] = v
            existing["exchange_kwargs"] = kwargs
            with open(self.config_path, "w") as f:
                json.dump(existing, f, indent=4)
            return web.json_response({"status": "ok", "message": "Config saved"})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=400)

    async def api_account(self, request):
        try:
            balance = await self.client.get_balance()
            all_positions = await self.client.get_positions()

            # Single pass: compute all metrics together
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

            # Use trader cached symbols; fallback to config
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

                # Filter for active symbol subset (reuse same loop, no extra API call)
                if sym_set:
                    user_sym = self.client.user_symbol(p["symbol"])
                    if user_sym in sym_set:
                        sym_total_pnl += pnl
                        sym_total_value += val

            total_pnl_rate = total_pnl / total_value if total_value > 0 else 0
            realtime_rate = sym_total_pnl / sym_total_value if sym_total_value > 0 else 0

            # Update historical stats (offload to thread pool)
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
            cfg = self._load_config()

            all_positions = await self.client.get_positions()

            # Compute pnl_rate for each position
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
                    "pnl": round(pnl, 4),
                    "pnl_rate": round(rate, 6),
                })

            # Sort by pnl_rate ascending (worst loss first, profit last)
            position_items.sort(key=lambda x: x["pnl_rate"])

            # Only send occupied slots; client fills empty ones
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
            # Use in-memory cache for 5s to avoid repeated file reads
            if self._system_cache[1] and now - self._system_cache[0] < 5:
                return web.json_response(self._system_cache[1])

            # CPU usage from /proc/stat
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

            # Memory from /proc/meminfo
            mem_total = mem_free = 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        mem_free = int(line.split()[1]) * 1024
            mem_used = mem_total - mem_free if mem_total else 0

            # Disk usage
            du = shutil.disk_usage("/")

            # Network from /proc/net/dev
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
            t0 = time.monotonic()
            period = request.query.get("period", "hour")  # 'hour' or 'day'
            # Use cached balance from api_account to avoid extra network request
            cached_ts, cached_bal = self._balance_cache
            if cached_bal > 0 and time.monotonic() - cached_ts < 300:
                balance = cached_bal
            else:
                balance_data = await self.client.get_balance()
                balance = balance_data.get("balance", 0)
                self._balance_cache = (time.monotonic(), float(balance) if balance else 0.0)
            t1 = time.monotonic()
            if balance <= 0:
                balance = 1  # avoid division by zero

            now = datetime.now(timezone.utc)

            # Build bucket list and query recent trades
            if period == "hour":
                buckets = []
                for i in range(24):
                    t = now - timedelta(hours=23 - i)
                    buckets.append((t.strftime("%Y-%m-%dT%H"), t.strftime("%H:00")))
                start_iso = (now - timedelta(hours=24)).isoformat()
            else:
                buckets = []
                for i in range(30):
                    t = now - timedelta(days=29 - i)
                    buckets.append((t.strftime("%Y-%m-%d"), t.strftime("%m-%d")))
                start_iso = (now - timedelta(days=30)).isoformat()

            def _fetch_trades():
                with self.db.lock:
                    return self.db.conn.execute(
                        "SELECT close_time, pnl FROM trades WHERE close_time >= ? ORDER BY close_time",
                        (start_iso,),
                    ).fetchall()
            rows = await self._db_sync(_fetch_trades)
            t2 = time.monotonic()

            # Aggregate pnl by bucket
            from collections import defaultdict
            bucket_pnls = defaultdict(float)
            for close_time, pnl in rows:
                if period == "hour":
                    bucket_key = close_time[:13]  # '2026-05-16T22'
                else:
                    bucket_key = close_time[:10]  # '2026-05-16'
                bucket_pnls[bucket_key] += float(pnl)

            # Build cumulative series
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
