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
        self.app.router.add_get("/api/config", self.api_config_get)
        self.app.router.add_post("/api/config", self.api_config_set)
        self.app.router.add_get("/api/account", self.api_account)
        self.app.router.add_get("/api/positions", self.api_positions)
        self.app.router.add_get("/api/positions-map", self.api_positions_map)
        self.app.router.add_get("/api/stats", self.api_stats)
        self.app.router.add_get("/api/trades", self.api_trades)
        self.app.router.add_get("/api/system", self.api_system)
        self.app.router.add_post("/api/refresh-symbols", self.api_refresh_symbols)
        # System metrics cache for rate calculations
        self._last_cpu = None
        self._last_net = None
        self._last_system_time = None

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    async def handle_index(self, request):
        web_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
        return web.FileResponse(web_path)

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
            unrealized = sum(float(p.get("unrealizedPnl", 0) or 0) for p in all_positions)

            # Position breakdown for all positions
            long_pos_count = sum(1 for p in all_positions if p.get("side") == "long")
            short_pos_count = sum(1 for p in all_positions if p.get("side") == "short")

            # Total unrealized PnL rate for ALL positions
            total_pnl = 0.0
            total_value = 0.0
            for p in all_positions:
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                market = self.client.get_market_info(p["symbol"])
                cs = market.get("contractSize", 1) or 1
                val = entry * contracts * cs
                total_pnl += pnl
                total_value += val
            total_pnl_rate = total_pnl / total_value if total_value > 0 else 0

            # Worst position (most negative unrealized PnL)
            worst_pnl = 0.0
            worst_rate = 0.0
            # Also compute the worst rate among ALL positions for historical tracking
            current_max_loss_rate = 0.0
            for p in all_positions:
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                contracts = float(p.get("contracts", 0) or 0)
                market = self.client.get_market_info(p["symbol"])
                cs = market.get("contractSize", 1) or 1
                val = entry * contracts * cs
                rate = pnl / val if val > 0 else 0
                if rate < current_max_loss_rate:
                    current_max_loss_rate = rate
                if pnl < worst_pnl:
                    worst_pnl = pnl
                    worst_rate = rate

            # Update historical max loss rate if current is worse
            hist_max_loss_rate = self.db.get_runtime_stat("max_position_loss_rate", 0)
            if current_max_loss_rate < hist_max_loss_rate:
                hist_max_loss_rate = current_max_loss_rate
                self.db.set_runtime_stat("max_position_loss_rate", hist_max_loss_rate)

            # Use trader cached symbols; fallback to config if trader not available
            if self.trader and self.trader._candidate_symbols:
                symbols = self.trader._candidate_symbols
            else:
                cfg = self._load_config()
                volume_threshold = cfg.get("volume_threshold", 0)
                price_threshold = cfg.get("price_threshold", 0)
                if volume_threshold == 0 and price_threshold == 0:
                    symbols = cfg.get("symbols", [])
                else:
                    symbols = await self.client.get_candidate_symbols(volume_threshold, price_threshold)

            # Calculate real-time profit rate for active symbols only
            if symbols:
                sym_positions = await self.client.get_positions(symbols)
                sym_total_pnl = 0.0
                sym_total_value = 0.0
                for p in sym_positions:
                    pnl = float(p.get("unrealizedPnl", 0) or 0)
                    entry = float(p.get("entryPrice", 0) or 0)
                    contracts = float(p.get("contracts", 0) or 0)
                    market = self.client.get_market_info(p["symbol"])
                    cs = market.get("contractSize", 1) or 1
                    val = entry * contracts * cs
                    sym_total_pnl += pnl
                    sym_total_value += val
                realtime_rate = sym_total_pnl / sym_total_value if sym_total_value > 0 else 0
            else:
                realtime_rate = 0

            margin_call_count = int(self.db.get_runtime_stat("margin_call_count", 0))

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
                "max_position_loss_rate": round(hist_max_loss_rate, 6),
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

            # Build 100 slots: first N filled with positions sorted by loss
            result = []
            for i in range(100):
                if i < len(position_items):
                    result.append({"index": i, **position_items[i], "occupied": True})
                else:
                    result.append({"index": i, "symbol": None, "side": None, "contracts": 0, "entry_price": 0, "pnl": 0, "pnl_rate": 0, "occupied": False})

            return web.json_response({"slots": result, "total_positions": len(all_positions)})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_stats(self, request):
        try:
            balance_data = await self.client.get_balance()
            balance = balance_data.get("balance", 0)
            stats = self.db.get_stats(balance)
            return web.json_response(stats)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_system(self, request):
        try:
            now = time.time()

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

            return web.json_response({
                "cpu_percent": round(cpu_percent, 1),
                "mem_total": mem_total,
                "mem_used": mem_used,
                "mem_percent": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                "disk_total": du.total,
                "disk_used": du.used,
                "disk_percent": round(du.used / du.total * 100, 1) if du.total else 0,
                "net_rx_speed": round(rx_speed, 0),
                "net_tx_speed": round(tx_speed, 0),
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_refresh_symbols(self, request):
        try:
            symbols = await self.trader.refresh_symbols_now()
            return web.json_response({"status": "ok", "count": len(symbols), "symbols": symbols})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_trades(self, request):
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
            total = self.db.get_total_trades()
            trades = self.db.get_recent_trades(limit, offset)
            return web.json_response({
                "trades": trades,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)
