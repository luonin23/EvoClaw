"""EvoClaw v2.0 — Web/API 服务。

鉴权：所有写接口需要 X-Web-Password 头（密码存 DB，首次启动随机生成）。
只读接口公开，方便手机随时查看。
"""

import asyncio
import json
import logging
import os
import secrets
import time

from aiohttp import web

log = logging.getLogger(__name__)

_static_cache: dict[str, str] = {}


def _static(path: str) -> str:
    if path not in _static_cache:
        with open(path, "r", encoding="utf-8") as f:
            _static_cache[path] = f.read()
    return _static_cache[path]


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


class WebServer:
    def __init__(self, client, db, engine=None):
        self.client = client
        self.db = db
        self.engine = engine
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self.page_index)
        r.add_get("/intro.html", self.page_intro)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/grid", self.api_grid)
        r.add_get("/api/dca", self.api_dca)
        r.add_get("/api/orders", self.api_orders)
        r.add_get("/api/config", self.api_config_get)
        r.add_post("/api/config", self.api_config_set)
        r.add_post("/api/auth-check", self.api_auth_check)
        r.add_post("/api/pause", self.api_pause)
        self._start = time.time()

    # ===== 鉴权 =====
    def _password(self) -> str:
        try:
            return str(self.db.load_config().get("web_password") or "")
        except Exception:
            return ""

    def _auth_ok(self, request) -> bool:
        expected = self._password()
        if not expected:
            return False
        supplied = request.headers.get("X-Web-Password") or request.query.get("password") or ""
        try:
            return secrets.compare_digest(supplied.encode(), expected.encode())
        except (TypeError, UnicodeEncodeError):
            return False

    async def _deny(self, request):
        log.warning(f"未授权访问尝试 来自 {request.remote}")
        return web.json_response({"status": "error", "message": "unauthorized"}, status=401)

    async def api_auth_check(self, request):
        if self._auth_ok(request):
            return web.json_response({"status": "ok"})
        return await self._deny(request)

    # ===== 页面 =====
    async def page_index(self, request):
        return web.Response(text=_static(os.path.join(os.path.dirname(__file__), "web", "index.html")),
                            content_type="text/html", headers=_NO_CACHE)

    async def page_intro(self, request):
        p = os.path.join(os.path.dirname(__file__), "web", "intro.html")
        if not os.path.exists(p):
            return web.Response(status=404, text="intro.html 尚未生成")
        return web.Response(text=_static(p), content_type="text/html", headers=_NO_CACHE)

    # ===== API =====
    async def api_state(self, request):
        cfg = self.db.load_config()
        health = await self.engine.health() if self.engine else {}
        try:
            fut = await self.client.get_futures_balance()
        except Exception as e:
            fut = {"balance": 0, "available": 0, "error": str(e)}
        spot = {"free": 0, "total": 0}
        if cfg.get("mode") == "dca":
            try:
                spot = await self.client.get_spot_balance("USDT")
            except Exception as e:
                spot = {"free": 0, "total": 0, "error": str(e)}
        return web.json_response({
            "mode": cfg.get("mode", "off"),
            "dry_run": bool(cfg.get("dry_run", True)),
            "paused": health.get("paused", False),
            "futures_balance": fut,
            "spot_balance": spot,
            "health": health,
            "uptime": round(time.time() - self._start),
        })

    async def api_grid(self, request):
        from strategies.grid import GridStrategy
        cfg = self.db.load_config()
        s = GridStrategy(self.client, self.db, cfg)
        try:
            return web.json_response(await s.status())
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_dca(self, request):
        from strategies.dca import DCAStrategy
        cfg = self.db.load_config()
        s = DCAStrategy(self.client, self.db, cfg)
        try:
            data = await s.status()
            data["records"] = self.db.get_dca_records(50)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_orders(self, request):
        try:
            limit = min(int(request.query.get("limit", "100")), 500)
            return web.json_response({"orders": self.db.get_orders(limit)})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def api_config_get(self, request):
        cfg = self.db.load_config()
        safe = dict(cfg)
        kw = dict(safe.get("exchange_kwargs", {}))
        safe["exchange_kwargs"] = {
            "apiKey": (kw.get("apiKey", "")[:6] + "...") if kw.get("apiKey") else "",
            "secret": "***" if kw.get("secret") else "",
        }
        safe.pop("web_password", None)   # 绝不外泄
        return web.json_response(safe)

    _WRITABLE = {"mode", "dry_run", "loop_interval", "grid", "dca",
                 "min_order_notional", "order_notional_safety", "exchange_kwargs"}

    async def api_config_set(self, request):
        if not self._auth_ok(request):
            return await self._deny(request)
        try:
            body = await request.json()
            body = {k: v for k, v in body.items() if k in self._WRITABLE}
            cfg = self.db.load_config()
            kw = cfg.get("exchange_kwargs", {})
            if "exchange_kwargs" in body:
                nk = body.pop("exchange_kwargs")
                if nk.get("apiKey"):
                    kw["apiKey"] = nk["apiKey"]
                if nk.get("secret"):
                    kw["secret"] = nk["secret"]
            cfg.update(body)
            cfg["exchange_kwargs"] = kw
            self.db.save_config(cfg)
            if self.engine:
                self.engine._reload()
            return web.json_response({"status": "ok"})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=400)

    async def api_pause(self, request):
        if not self._auth_ok(request):
            return await self._deny(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not self.engine:
            return web.json_response({"status": "error", "message": "engine not ready"}, status=503)
        self.engine.set_paused(bool(body.get("paused")))
        return web.json_response({"status": "ok", "paused": self.engine.paused})
