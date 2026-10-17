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

import params

from aiohttp import web

log = logging.getLogger(__name__)

_static_cache: dict[str, tuple[tuple, str]] = {}


def _static(path: str) -> str:
    """读静态文件；文件变了就自动重读。

    原来是进程级永久缓存 —— 改一行 HTML 都得重启服务，实测被这个坑过好几次。
    按 (mtime, size) 判断是否重读：命中缓存时只是一次 stat，代价可忽略。
    """
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    hit = _static_cache.get(path)
    if hit and sig is not None and hit[0] == sig:
        return hit[1]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if sig is not None:
        _static_cache[path] = (sig, text)
    return text


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
        r.add_post("/api/grid/cancel", self.api_grid_cancel)
        r.add_get("/api/dca", self.api_dca)
        r.add_get("/api/orders", self.api_orders)
        r.add_get("/api/config", self.api_config_get)
        r.add_post("/api/config", self.api_config_set)
        r.add_get("/api/params", self.api_params_get)
        r.add_get("/api/symbols", self.api_symbols)
        r.add_get("/api/spot/screen", self.api_spot_screen)
        r.add_get("/api/profit-dca", self.api_profit_dca)
        r.add_get("/api/config/history", self.api_config_history)
        r.add_post("/api/config/rollback", self.api_config_rollback)
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

    async def api_grid_cancel(self, request):
        """「取消并重新筛选」：撤掉某标的的全部挂单并强制重选（仅空仓待机可用）。"""
        if not self._auth_ok(request):
            return await self._deny(request)
        from strategies.grid import GridStrategy
        try:
            body = await request.json()
        except Exception:
            body = {}
        sym = str((body or {}).get("symbol") or "").strip().upper()
        if not sym:
            return web.json_response({"status": "error", "message": "缺少 symbol"}, status=400)
        cfg = self.db.load_config()
        s = GridStrategy(self.client, self.db, cfg)
        try:
            ok, msg = await s.cancel_waiting(sym)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)
        return web.json_response({"status": "ok" if ok else "error", "message": msg},
                                 status=200 if ok else 400)

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
        """成交记录，分页 + 按市场过滤。

        market=futures → mode='grid'（合约），market=spot → mode='dca'（现货）；
        缺省返回全部。页码从 1 起，size 上限 200。
        """
        try:
            market = request.query.get("market", "")
            mode = {"futures": "grid", "spot": "dca"}.get(market)
            page = max(1, int(request.query.get("page", "1")))
            size = min(max(10, int(request.query.get("size", "50"))), 200)
            total = self.db.orders_count(mode)
            return web.json_response({
                "orders": self.db.get_orders(size, mode=mode, offset=(page - 1) * size),
                "summary": self.db.get_order_summary(mode),
                "total": total, "page": page, "size": size,
                "pages": max(1, -(-total // size)),
            })
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
                 "min_order_notional", "order_notional_safety", "exchange_kwargs",
                 "web_password", "profit_dca"}

    def _check(self, section: str, key: str, value):
        """按注册表校验并转换一个值。返回 (ok, 转换后的值, 错误消息)。"""
        ok, val, msg = params.validate(f"{section}.{key}" if section else key, value)
        if not ok:
            return False, value, f"{section + '.' if section else ''}{key}：{msg}"
        return True, val, ""

    async def api_config_set(self, request):
        if not self._auth_ok(request):
            return await self._deny(request)
        try:
            body = await request.json()
            body = {k: v for k, v in body.items() if k in self._WRITABLE}
            cfg = self.db.load_config()
            errors: list[str] = []

            # ---- 先全校验，一处不合法就整体拒绝（不做半截生效）----
            # ⚠ 段名必须**从注册表推导**，不能写死 ("grid","dca")。
            # 写死时新增的段（如 profit_dca）会掉进下面那个「顶层键」分支，
            # 而 validate("profit_dca", {...}) 查不到这个段名 → 直接放行 →
            # 类型/范围校验被整体绕过（实测 POST {"profit_dca":{"ratio":100}}
            # 把 10000% 写进了库）。_merge_sections 用的是注册表，这里必须一致。
            sections = {p.section for p in params.PARAMS if p.section}
            for key in sections:
                if isinstance(body.get(key), dict):
                    for k, v in list(body[key].items()):
                        ok, val, msg = self._check(key, k, v)
                        if ok:
                            body[key][k] = val
                        else:
                            errors.append(msg)
            for k in list(body.keys()):
                if k in sections or k == "exchange_kwargs":
                    continue
                ok, val, msg = self._check("", k, body[k])
                if ok:
                    body[k] = val
                else:
                    errors.append(msg)
            if errors:
                return web.json_response(
                    {"status": "error", "message": "；".join(errors), "errors": errors},
                    status=400)

            kw = dict(cfg.get("exchange_kwargs", {}))
            if "exchange_kwargs" in body:
                nk = body.pop("exchange_kwargs")
                if nk.get("apiKey"):
                    kw["apiKey"] = nk["apiKey"]
                if nk.get("secret"):
                    kw["secret"] = nk["secret"]
            # 各配置段做**字段级合并**，不做整体替换。
            # 网页上只暴露了其中几个参数，若照单全收地整体替换，其余参数
            # 会被"页面上没显示"这一点顺手抹掉（含自动选币维护的字段）。
            # ⚠ 段名从**注册表**推导，别再写死 ("grid","dca") —— 新增一段
            # （比如 profit_dca）时忘了加，一次保存就会把该段其余键全冲掉
            # （实测：POST {profit_dca:{step_usdt:20}} 把 ratio/enabled 冲没了）。
            self._merge_sections(cfg, body)
            cfg.update(body)
            cfg["exchange_kwargs"] = kw

            # ---- 通用防线：记下改动前的完整配置，供审计与一键回滚 ----
            # 没有任何实际变化就不写历史 —— 否则「点一次保存」就多一条，
            # 真正有用的那几条会被冲掉。
            changed = self._diff_changed(self.db.load_config(), cfg)
            if changed:
                self.db.save_config_audited(cfg, changed, source=request.remote or "web")
            else:
                self.db.save_config(cfg)
            if self.engine:
                self.engine._reload()
            return web.json_response({"status": "ok", "changed": changed})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=400)

    @staticmethod
    def _merge_sections(cfg: dict, body: dict) -> dict:
        """把 body 里各配置段**按字段合并**进 cfg 的对应段（就地改 body）。

        段名从注册表推导，不写死 —— 新增一段时忘了加，一次保存就会把该段
        其余键全冲掉（实测：POST {profit_dca:{step_usdt:20}} 把 ratio/enabled
        冲没了，因为合并列表里只有 grid/dca）。
        """
        for key in {p.section for p in params.PARAMS if p.section}:
            if isinstance(body.get(key), dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(body[key])
                body[key] = merged
        return body

    @staticmethod
    def _diff_changed(old: dict, new: dict) -> dict:
        """对比出真正变化的键（含各配置段的子键），用于审计与回滚展示。

        exchange_kwargs / web_password 是凭据：只记「改过没有」，不记值。
        """
        out = {}
        for k, v in new.items():
            if k == "exchange_kwargs":
                if v != old.get(k):
                    out["exchange_kwargs"] = ["***", "***"]
                continue                      # 密钥不进审计明文
            if k == "web_password":
                if v != old.get(k):
                    out["web_password"] = ["***", "***"]
                continue
            if isinstance(v, dict) and isinstance(old.get(k), dict):
                for kk, vv in v.items():
                    if vv != old[k].get(kk):
                        out[f"{k}.{kk}"] = [old[k].get(kk), vv]
            elif v != old.get(k):
                out[k] = [old.get(k), v]
        return out

    async def api_params_get(self, request):
        """参数表元数据 + 当前值。前端表格完全由它驱动。"""
        cfg = self.db.load_config()
        kw = dict(cfg.get("exchange_kwargs") or {})
        masked = {
            "exchange_kwargs": {"apiKey": (kw.get("apiKey", "")[:6] + "…")
                                if kw.get("apiKey") else "", "secret": ""},
        }
        items = []
        for p in params.PARAMS:
            if p.hidden:
                continue
            if p.sensitive:
                # 敏感项也进表（否则用户在页面上根本改不了 API key / 密码），
                # 但**绝不回传明文** —— 前端只显示掩码，留空即表示不修改。
                items.append({
                    "key": p.key, "group": p.group, "label": p.label, "desc": p.desc,
                    "type": p.type, "widget": p.widget, "unit": "", "scale": 1,
                    "min": None, "max": None, "step": None,
                    "sentinel": None, "sentinelHint": "", "choices": None,
                    "hot": p.hot, "value": masked.get(p.key, ""), "default": None,
                    "changed": False, "enabledWhen": None, "sensitive": True,
                })
                continue
            cur = params.dotted_get(cfg, p.key, p.default)
            if p.type == "kwargs":
                continue
            items.append({
                "key": p.key, "group": p.group, "label": p.label, "desc": p.desc,
                "type": p.type, "widget": p.widget, "unit": p.unit, "scale": p.scale,
                "min": p.min, "max": p.max, "step": p.step,
                "sentinel": p.sentinel,
                "sentinelHint": params.SENTINEL_HINT.get(p.sentinel or "", ""),
                "choices": list(p.choices) if p.choices else None,
                "hot": p.hot, "value": cur, "default": p.default,
                "changed": cur != p.default,
                "enabledWhen": list(p.enabled_when) if p.enabled_when else None,
            })
        return web.json_response({"groups": params.groups(), "params": items})

    async def api_symbols(self, request):
        """供「黑名单 / 手写标的 / 定投标的」的多选弹窗用。"""
        from selector import DEFAULT_BLACKLIST

        def _list(markets):
            return sorted({self.client.user_symbol(s) for s in (markets or {})})

        fut = _list(getattr(self.client, "_fut_markets", {}))
        spot = _list(getattr(self.client, "_spot_markets", {}))
        if not spot and request.query.get("kind") == "spot":
            # 现货市场默认不加载（只有跑定投时才用），按需拉一次，之后有缓存。
            try:
                await self.client.ensure_spot()
                spot = _list(getattr(self.client, "_spot_markets", {}))
            except Exception as e:
                log.warning(f"加载现货市场失败: {e}")
        return web.json_response({"futures": fut, "spot": spot,
                                  "forced": sorted(DEFAULT_BLACKLIST)})

    async def api_spot_screen(self, request):
        """现货筛选榜：跌幅 / 涨幅 / 振幅（按 N 日），各取前 10。

        ?mode=drop|gain|range|all&days=7&limit=10
          · drop  —— 只保留**真正下跌**的币（change < 0），跌最多在前
          · gain  —— 涨最多在前
          · range —— N 日振幅最大在前
          · all   —— 一次返回全部候选（含 change 与 range），前端据此本地出三个榜
        """
        mode = str(request.query.get("mode", "drop"))
        try:
            days = max(1, min(90, int(request.query.get("days", 7))))
            limit = max(1, min(200, int(request.query.get("limit", 10))))
        except ValueError:
            return web.json_response({"status": "error", "message": "参数不合法"}, status=400)
        if mode not in ("drop", "gain", "range", "all"):
            return web.json_response(
                {"status": "error", "message": "mode 只能是 drop/gain/range/all"}, status=400)

        # **不做服务端缓存**：用户要的是「每次点开弹窗重新计算」。
        # 一次调用就把涨跌幅和振幅都算出来了，前端按天数缓存、
        # 同一次会话里切榜不再请求；改了天数或重开弹窗才重算。
        try:
            res = await self.client.spot_screen(days=days)
        except Exception as e:
            log.warning(f"现货筛选失败: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)
        rows = res["items"]
        if not rows:
            return web.json_response(
                {"status": "error", "message": "没取到候选标的（现货市场可能还在加载），请稍后重试"},
                status=503)

        if mode == "drop":
            # 盈利的直接排除 —— 行情普涨时不会出现「跌幅榜第一名是 +2%」这种怪事
            rows = [r for r in rows if r["change"] < 0]
            rows.sort(key=lambda r: r["change"])                 # 跌最多在前
        elif mode == "gain":
            rows.sort(key=lambda r: -r["change"])                # 涨最多在前
        elif mode == "range":
            rows.sort(key=lambda r: -r["range"])                 # 振幅最大在前
        # mode == "all"：不排序，原样返回（前端自己排）
        return web.json_response({
            "mode": mode, "days": days, "total": len(rows),
            "items": rows[:limit] if mode != "all" else rows,
            "failed": res["failed"],
        })

    async def api_profit_dca(self, request):
        from strategies.profit_dca import ProfitDCA
        p = ProfitDCA(self.client, self.db, self.db.load_config())
        return web.json_response(p.status())

    async def api_config_history(self, request):
        if not self._auth_ok(request):
            return await self._deny(request)
        return web.json_response({"items": self.db.config_history(20)})

    async def api_config_rollback(self, request):
        if not self._auth_ok(request):
            return await self._deny(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        target = self.db.config_rollback(int(body.get("id") or 0))
        if target is None:
            return web.json_response({"status": "error", "message": "没有可回滚的历史"},
                                     status=400)
        if self.engine:
            self.engine._reload()
        return web.json_response({"status": "ok"})

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
