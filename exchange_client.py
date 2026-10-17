"""EvoClaw v2.0 — 交易所客户端。

借鉴 v1 的成熟做法：价格缓存 + 精度处理 + 最小名义值 + 错误码分类 + 取消安全。
v2 同时支持两个市场：
  - futures（binanceusdm）：合约网格（低杠杆）
  - spot（binance）      ：现货交易（马丁格尔）/ 利润定投 / 现货筛选

dry_run=True 时，策略层不会调用下单方法；客户端仍可读行情/余额用于模拟。
"""

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt

log = logging.getLogger(__name__)


def _extract_code(err: str) -> str:
    m = re.search(r'"code":(-?\d+)', err)
    return m.group(1) if m else ""


class LogThrottle:
    """同一 key 在冷却窗口内只打一条日志。"""

    def __init__(self, cooldown: float = 300.0):
        self._cooldown = cooldown
        self._entries: dict[str, tuple[float, int]] = {}

    def emit(self, key: str) -> str | None:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = (now, 0)
            return ""
        last, suppressed = entry
        if now - last >= self._cooldown:
            self._entries[key] = (now, 0)
            return f" (suppressed {suppressed} in {self._cooldown:.0f}s)" if suppressed else ""
        self._entries[key] = (last, suppressed + 1)
        return None


_throttle = LogThrottle(cooldown=300)

# 现货下单下限：**全自动跟随交易所规则**（NOTIONAL filter，一般 5U、DOGE 等部分
# 币对 1U），不设人工配置。下面两个只是技术保障，不是门槛：
#   · SAFETY —— min_amount 向上取整时的保险，保证按步长截断后的数量仍够得着
#     最小名义额，不被交易所拒单
#   · FALLBACK —— 市场表拿不到时的保守兜底（币安现货典型值）
SPOT_NOTIONAL_SAFETY = 1.02
SPOT_NOTIONAL_FALLBACK = 5.0

_oid_seq = 0


def _client_oid() -> str:
    """给本程序下的普通单打一个可识别的 clientOrderId 前缀。

    币安成交明细里带 clientOrderId —— 有了它，对账时就能区分
    「我们挂的单成交了」和「用户在 App 里手工平的」，不会再把两者搞混。
    """
    global _oid_seq
    _oid_seq += 1
    return f"ec{int(time.time() * 1000) % 10**9}{_oid_seq % 1000:03d}"


class ExchangeClient:
    def __init__(self, config: dict):
        self.config = config
        self.dry_run = bool(config.get("dry_run", True))

        kwargs = dict(config.get("exchange_kwargs", {}))
        kwargs.setdefault("enableRateLimit", True)
        kwargs.setdefault("timeout", 15000)
        kwargs.setdefault("http_proxy", None)

        self.futures = ccxt.binanceusdm(dict(kwargs))
        self.futures.options["recvWindow"] = 60000
        self.futures.options["adjustForTimeDifference"] = True

        self._spot = None
        self._spot_kwargs = kwargs
        self._spot_lock = asyncio.Lock()      # ensure_spot 可重入（见其 docstring）
        self._spot_ready = False

        self._fut_markets: dict = {}
        self._spot_markets: dict = {}
        self._reverse: dict = {}          # ccxt unified symbol -> user symbol
        self._prices: dict = {}           # user symbol -> last price
        self._price_ts: dict = {}         # user symbol -> monotonic ts
        self._hl: dict = {}               # user symbol -> (24h high, 24h low)
        self._oo_cache: dict = {}         # (symbol, kind) -> (orders, monotonic ts)
        self._closed = False
        self.min_order_notional = float(config.get("min_order_notional", 5) or 5)
        self.notional_safety = float(config.get("order_notional_safety", 1.02) or 1.02)

    # ===== 基础设施 =====
    async def _safe_call(self, coro, timeout=20):
        if self._closed:
            raise RuntimeError("ExchangeClient already closed")
        return await asyncio.wait_for(coro, timeout=timeout)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for ex in (self.futures, self._spot):
            if ex is None:
                continue
            try:
                session = getattr(ex, "session", None)
                if session and hasattr(session, "close"):
                    await asyncio.wait_for(session.close(), timeout=3)
                await asyncio.wait_for(ex.close(), timeout=5)
            except Exception as e:
                log.warning(f"Exchange close warning: {e}")

    async def load_markets(self):
        markets = await self._safe_call(self.futures.load_markets(), timeout=25)
        for sym, m in markets.items():
            if m.get("swap") and m.get("quote") == "USDT":
                self._fut_markets[sym] = m
                self._register_reverse(sym)
        log.info(f"Futures markets loaded: {len(self._fut_markets)} USDT swaps")

    async def ensure_spot(self):
        """惰性加载现货市场（DCA / 现货筛选 / 利润定投才需要）。

        必须可重入：引擎 tick 和 Web 请求随时可能同时调它。以前是
        `if self._spot is not None: return` —— 第二个调用者会在市场**还没加载完**
        时直接返回，于是 _spot_markets 是空的，现货筛选静默返回 0 个候选
        （实测踩过）。现在用锁 + 完成标记：后到的协程会**等**第一次加载完成。
        """
        if self._spot_ready:
            return
        async with self._spot_lock:
            if self._spot_ready:
                return
            if self._spot is None:
                self._spot = ccxt.binance(dict(self._spot_kwargs))
                self._spot.options["recvWindow"] = 60000
                self._spot.options["adjustForTimeDifference"] = True
            markets = await self._safe_call(self._spot.load_markets(), timeout=25)
            for sym, m in markets.items():
                if m.get("spot") and m.get("quote") == "USDT":
                    self._spot_markets[sym] = m
                    self._register_reverse(sym)
            self._spot_ready = True
            log.info(f"Spot markets loaded: {len(self._spot_markets)} USDT pairs")

    def _register_reverse(self, ccxt_sym: str):
        if ccxt_sym not in self._reverse:
            self._reverse[ccxt_sym] = ccxt_sym.replace("/", "").replace(":USDT", "")

    def user_symbol(self, ccxt_sym: str) -> str:
        return self._reverse.get(ccxt_sym, ccxt_sym)

    def to_ccxt(self, user_symbol: str, kind: str = "futures") -> str:
        """把 BTCUSDT / BTC/USDT 统一成 ccxt 的 `BTC/USDT:USDT`（合约）或 `BTC/USDT`（现货）。"""
        s = user_symbol.replace("/", "").replace(":USDT", "")
        base = s[:-4] if s.endswith("USDT") else s
        if kind == "futures":
            return f"{base}/USDT:USDT"
        return f"{base}/USDT"

    def market_info(self, user_symbol: str, kind: str = "futures") -> dict:
        table = self._fut_markets if kind == "futures" else self._spot_markets
        return table.get(self.to_ccxt(user_symbol, kind), {})

    # ===== 价格 =====
    async def refresh_prices(self, symbols: list[str]):
        """刷新价格缓存；失败会抛异常（由引擎决定是否降级）。

        两张市场都要查：只在合约上市的币走 futures，只在上现货的币（部分新币）
        走 spot。以前只查 futures —— 现货策略选了纯现货币时永远拿不到价格，
        买入会静默跳过。
        """
        ts = time.monotonic()
        got = set()
        fut = [s for s in (self.to_ccxt(x, "futures") for x in symbols)
               if s in self._fut_markets]
        if fut:
            tickers = await self._safe_call(self.futures.fetch_tickers(fut), timeout=20)
            for sym, t in (tickers or {}).items():
                if t and t.get("last"):
                    us = self.user_symbol(sym)
                    self._prices[us] = float(t["last"])
                    self._price_ts[us] = ts
                    self._store_hl(us, t)
                    got.add(us)
        rest = [x for x in symbols if x not in got]
        if rest and self._spot is not None:
            spo = [s for s in (self.to_ccxt(x, "spot") for x in rest)
                   if s in self._spot_markets]
            if spo:
                tickers = await self._safe_call(self._spot.fetch_tickers(spo), timeout=20)
                for sym, t in (tickers or {}).items():
                    if t and t.get("last"):
                        us = self.user_symbol(sym)
                        self._prices[us] = float(t["last"])
                        self._price_ts[us] = ts
                        self._store_hl(us, t)

    def _store_hl(self, user_symbol: str, t: dict):
        try:
            hi = float(t.get("high") or 0)
            lo = float(t.get("low") or 0)
            if hi > 0 and lo > 0 and hi > lo:
                self._hl[user_symbol] = (hi, lo)
        except (TypeError, ValueError):
            pass

    def range_pos(self, user_symbol: str) -> float | None:
        """现价在 24h 区间中的位置：0=最低点，1=最高点；无数据返回 None。

        用来判断「现在是高位还是低位」——选币时振幅已经很大，但程序并不知道
        现价处在当日波动的顶部还是底部，这个指标就是那个代理量。
        """
        hl = self._hl.get(user_symbol)
        p = self._prices.get(user_symbol)
        if not hl or not p or p <= 0:
            return None
        hi, lo = hl
        if hi <= lo:
            return None
        return max(0.0, min(1.0, (p - lo) / (hi - lo)))

    async def ensure_price(self, user_symbol: str, max_age: float = 30.0) -> float:
        now = time.monotonic()
        p = self._prices.get(user_symbol)
        if p and p > 0 and (now - self._price_ts.get(user_symbol, 0)) < max_age:
            return float(p)
        # 合约优先，合约没有就试现货（只在上现货的币走这条）
        for kind, ex, table in (("futures", self.futures, self._fut_markets),
                                ("spot", self._spot, self._spot_markets)):
            if ex is None:
                continue
            ccxt_sym = self.to_ccxt(user_symbol, kind)
            if ccxt_sym not in table:
                continue
            try:
                t = await self._safe_call(ex.fetch_ticker(ccxt_sym), timeout=10)
                last = float(t.get("last", 0) or 0)
                if last > 0:
                    self._prices[user_symbol] = last
                    self._price_ts[user_symbol] = now
                    self._store_hl(user_symbol, t)
                    return last
            except Exception as e:
                log.warning(f"ensure_price {user_symbol} ({kind}) failed: {e}")
        return float(self._prices.get(user_symbol, 0) or 0)

    def last_price(self, user_symbol: str) -> float:
        return float(self._prices.get(user_symbol, 0) or 0)

    # ===== 精度与最小下单量 =====
    def min_notional_of(self, user_symbol: str, kind: str = "futures") -> float:
        """该标的「一手」至少需要多少 USDT。

        合约 = max(交易所规则, 配置的 min_order_notional)；
        现货 = **全自动跟随交易所规则**（NOTIONAL filter，DOGE 1U、主流 5U），
        不叠加任何人工配置。
        """
        m = self.market_info(user_symbol, kind)
        if kind == "spot":
            if not m:
                return SPOT_NOTIONAL_FALLBACK
            ex_min = float((m.get("limits", {}).get("cost", {}).get("min", 0)) or 0)
            return ex_min or SPOT_NOTIONAL_FALLBACK
        if not m:
            return self.min_order_notional
        ex_min = float((m.get("limits", {}).get("cost", {}).get("min", 0)) or 0)
        return max(ex_min, self.min_order_notional)

    def _round_amount(self, user_symbol: str, raw: float, kind: str, up: bool) -> float:
        """把数量对齐到交易所的数量步长（ccxt 的 amount_to_precision 是**向下截断**）。

        up=True 向上取整（保证够得着最小名义值，宁可多买一档也不能被拒单）。
        """
        ex = self.futures if kind == "futures" else self._spot
        ccxt_sym = self.to_ccxt(user_symbol, kind)
        m = self.market_info(user_symbol, kind)
        try:
            step = float((m.get("precision") or {}).get("amount") or 0)
        except (TypeError, ValueError):
            step = 0.0
        if step > 0 and raw > 0:
            n = raw / step
            # 留 1e-9 容差：n 恰为整数倍时不要被 IEEE 误差顶到下一档
            raw = (math.ceil(n - 1e-9) if up else round(n)) * step
        return float(ex.amount_to_precision(ccxt_sym, raw))

    def round_amount(self, user_symbol: str, raw: float, kind: str = "futures") -> float:
        """把数量对齐到步长（向下取整）。卖出持仓量必须用它：对齐后不能比实际持有多。

        数量小于最小精度（或为 0）时 ccxt 会抛 InvalidOrder —— 这里转成返回 0，
        让上层走「不卖/不下单」的安全分支。
        """
        try:
            return self._round_amount(user_symbol, float(raw), kind, up=False)
        except Exception:
            return 0.0

    def min_amount(self, user_symbol: str, price: float, kind: str = "futures") -> float:
        """满足最小名义值与最小数量精度所需的最小可下数量；算不出（资金/精度不足）返回 0。

        ⚠ 合约和现货的「数量」是两种东西：
          · 合约 = 张数，**必须是整数**（ceil 到整张）
          · 现货 = 币的数量，**可以是小数**（对齐到步长即可）
        以前两者共用 `max(1.0, math.ceil(...))` —— 现货买 BTC 时算出「1 整枚」
        （≈8 万 U），而调用方只想买 12U。现货下单测试实测「5 个币买了一共
        84889U」。这条路径连着真钱，必须分开算。
        """
        m = self.market_info(user_symbol, kind)
        if not m or price <= 0:
            return 0.0
        try:
            cs = float(m.get("contractSize", 1) or 1)
            safety = self.notional_safety if kind == "futures" else SPOT_NOTIONAL_SAFETY
            target = self.min_notional_of(user_symbol, kind) * safety
            ex_min = float(((m.get("limits") or {}).get("amount") or {}).get("min") or 0)
            if kind == "futures":
                raw = max(1.0, math.ceil(target / (price * cs)))
                if ex_min > 0:
                    raw = max(raw, math.ceil(ex_min / cs))
            else:
                raw = target / (price * cs)
                if ex_min > 0:
                    raw = max(raw, ex_min / cs)
            return self._round_amount(user_symbol, raw, kind, up=True)
        except Exception as e:
            # 返回 0 = 上层会跳过下单（安全方向），但必须留痕，否则排障没线索
            log.warning(f"min_amount {user_symbol} ({kind}) failed: {e}")
            return 0.0

    def amount_for_notional(self, user_symbol: str, usdt: float, price: float,
                            kind: str = "futures") -> float:
        """按目标名义金额算数量（不低于最小可下量）；该金额买不起「一手」时返回 0。

        返回 0 的典型原因：目标金额低于该标的的最小名义值（如 BTC 一手 ≈ 82U）。
        这种情况由调用方汇总成一条清晰的「资金不足」提示，不在这里逐档刷屏。
        """
        m = self.market_info(user_symbol, kind)
        if not m or price <= 0:
            return 0.0
        try:
            cs = float(m.get("contractSize", 1) or 1)
            lo = self.min_amount(user_symbol, price, kind)
            if lo <= 0:
                return 0.0
            amt = self._round_amount(user_symbol, usdt / (price * cs), kind, up=False)
            if amt <= 0:
                return 0.0
            # 就近取整后若够不着最小可下量，抬到最小量（会略微超出目标金额，
            # 但绝不会被交易所拒单）
            return max(amt, lo)
        except Exception:
            return 0.0

    def round_price(self, user_symbol: str, price: float, kind: str = "futures") -> float:
        ex = self.futures if kind == "futures" else self._spot
        try:
            return float(ex.price_to_precision(self.to_ccxt(user_symbol, kind), price))
        except Exception:
            return price

    # ===== 账户 =====
    async def get_futures_balance(self) -> dict:
        b = await self._safe_call(self.futures.fetch_balance(), timeout=15)
        return {
            "balance": float(b.get("total", {}).get("USDT", 0) or 0),
            "available": float(b.get("free", {}).get("USDT", 0) or 0),
        }

    async def transfer_futures_to_spot(self, amount: float) -> dict | None:
        """USDT 从**合约账户**划转到**现货账户**（UMFUTURE_MAIN）。

        利润定投的钱本来躺在合约账户里，不划转就下不了现货单。
        只划「缺口」由调用方决定；这里只负责执行一次划转。
        """
        if amount <= 0:
            return None
        await self.ensure_spot()
        if self.dry_run:
            return {"id": "dry-transfer", "amount": amount, "dry_run": True}
        try:
            r = await self._safe_call(
                self._spot.transfer("USDT", float(amount), "future", "spot"), timeout=20)
            return {"id": str((r or {}).get("id") or ""), "amount": float(amount),
                    "dry_run": False}
        except Exception as e:
            log.error(f"划转 合约→现货 {amount:.2f}U 失败: {e}")
            return None

    async def ensure_spot_funds(self, needed: float) -> tuple[bool, float]:
        """确保现货账户有够用的 USDT；不够就从合约账户补缺口。

        返回 (是否够用, 本次划转了多少)。宁可少划，绝不多划 —— 划多了
        现货里躺着的钱不会自动回合约。
        """
        try:
            spot = await self.get_spot_balance("USDT")
            free = float(spot.get("free", 0) or 0)
        except Exception as e:
            log.warning(f"读取现货余额失败: {e}")
            return False, 0.0
        short = float(needed) - free
        if short <= 1e-9:
            return True, 0.0
        if self.dry_run:
            return True, short
        try:
            fut = await self.get_futures_balance()
            avail = float(fut.get("available", 0) or 0)
        except Exception as e:
            log.warning(f"读取合约可用余额失败: {e}")
            return False, 0.0
        if avail < short:
            log.warning(f"合约可用 {avail:.2f}U < 现货缺口 {short:.2f}U，无法划转")
            return False, 0.0
        r = await self.transfer_futures_to_spot(short)
        if not r:
            return False, 0.0
        log.warning(f"资金划转：合约 → 现货 {short:.2f}U（现货原 {free:.2f}U，"
                    f"需要 {needed:.2f}U）")
        return True, short

    async def fetch_spot_order(self, order_id: str, user_symbol: str) -> dict | None:
        await self.ensure_spot()
        try:
            return await self._safe_call(
                self._spot.fetch_order(str(order_id), self.to_ccxt(user_symbol, "spot")),
                timeout=15)
        except Exception as e:
            log.warning(f"查询现货订单 {order_id} 失败: {e}")
            return None

    async def fetch_spot_open_orders(self, user_symbol: str) -> list[dict]:
        await self.ensure_spot()
        try:
            return await self._safe_call(
                self._spot.fetch_open_orders(self.to_ccxt(user_symbol, "spot")), timeout=15)
        except Exception as e:
            log.warning(f"查询现货挂单失败 {user_symbol}: {e}")
            return []

    async def cancel_spot_order(self, order_id: str, user_symbol: str) -> bool:
        await self.ensure_spot()
        if self.dry_run:
            return True
        try:
            await self._safe_call(
                self._spot.cancel_order(str(order_id), self.to_ccxt(user_symbol, "spot")),
                timeout=15)
            return True
        except Exception as e:
            log.warning(f"撤现货单 {order_id} 失败: {e}")
            return False

    async def get_spot_balance(self, asset: str = "USDT") -> dict:
        await self.ensure_spot()
        b = await self._safe_call(self._spot.fetch_balance(), timeout=15)
        return {
            "free": float(b.get("free", {}).get(asset, 0) or 0),
            "total": float(b.get("total", {}).get(asset, 0) or 0),
        }

    async def get_positions(self) -> list[dict]:
        """合约持仓（仅数量>0 的）。"""
        rows = await self._safe_call(self.futures.fetch_positions(), timeout=20)
        out = []
        for p in rows:
            c = float(p.get("contracts", 0) or 0)
            if c > 0:
                out.append(p)
        return out

    # ===== 交易 =====
    async def set_margin_mode(self, user_symbol: str, mode: str = "isolated"):
        """逐仓：单仓爆仓只损失该仓保证金，不会牵连整个账户。"""
        if self.dry_run:
            return
        try:
            await self._safe_call(
                self.futures.set_margin_mode(mode, self.to_ccxt(user_symbol, "futures")),
                timeout=10)
        except Exception as e:
            # 已是该模式时交易所会报错，属正常
            if "-4046" not in str(e) and "No need to change" not in str(e):
                log.warning(f"set_margin_mode {user_symbol} {mode} failed: {e}")

    async def set_leverage(self, user_symbol: str, leverage: int):
        if self.dry_run:
            return
        try:
            await self._safe_call(
                self.futures.set_leverage(int(leverage), self.to_ccxt(user_symbol, "futures")),
                timeout=10)
        except Exception as e:
            log.warning(f"set_leverage {user_symbol} x{leverage} failed: {e}")

    async def create_limit_order(self, user_symbol: str, side: str, amount: float,
                                 price: float, kind: str = "futures",
                                 reduce_only: bool = False,
                                 position_side: str = "LONG") -> dict | None:
        """限价单。dry_run 时返回模拟成交结构（不触网）。

        本账户为**对冲模式**，合约单必须带 positionSide，否则报 -4061。
        网格是「只做多」：所有单 positionSide=LONG（SELL 即平多）。
        """
        if self.dry_run:
            return {"order_id": f"dry-{int(time.time()*1000)}", "price": price,
                    "amount": amount, "dry_run": True}
        ex = self.futures if kind == "futures" else self._spot
        params = {"newClientOrderId": _client_oid()}        # 现货/合约都打标记，方便对账
        if kind == "futures":
            params["positionSide"] = position_side.upper()
        if kind == "futures" and reduce_only:
            params["reduceOnly"] = True
        try:
            o = await self._safe_call(ex.create_order(
                self.to_ccxt(user_symbol, kind), "limit", side, amount, price, params), timeout=20)
            return {
                "order_id": str(o.get("id", "")),
                "price": float(o.get("price") or price),
                "amount": float(o.get("amount") or amount),
                "dry_run": False,
            }
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"limit_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Limit order failed {user_symbol} {side} {amount}@{price}: {e}{s}")
            return None

    async def create_market_order(self, user_symbol: str, side: str, amount: float,
                                  kind: str = "futures",
                                  position_side: str = "LONG") -> dict | None:
        if self.dry_run:
            px = self.last_price(user_symbol) or await self.ensure_price(user_symbol)
            return {"order_id": f"dry-{int(time.time()*1000)}", "price": px,
                    "amount": amount, "dry_run": True}
        ex = self.futures if kind == "futures" else self._spot
        # 现货也要带客户端单号：对账时用来区分「本程序下的单」和「手工买的」。
        # 现货没有 positionSide，合约才有。
        oid_tag = {"newClientOrderId": _client_oid()}
        params = ({**oid_tag, "positionSide": position_side.upper()}
                  if kind == "futures" else oid_tag)
        try:
            # 注意 ccxt 签名：create_order(symbol, type, side, amount, price, params)
            # 市价单 price 必须传 None，否则 params 会被当成 price 丢弃 → -4061
            o = await self._safe_call(ex.create_order(
                self.to_ccxt(user_symbol, kind), "market", side, amount, None, params), timeout=20)
            px = float(o.get("average") or o.get("price") or 0 or 0)
            if px <= 0:
                px = await self.ensure_price(user_symbol)
            return {"order_id": str(o.get("id", "")), "price": px,
                    "amount": float(o.get("amount") or amount), "dry_run": False}
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"market_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Market order failed {user_symbol} {side} {amount}: {e}{s}")
            return None

    async def create_stop_order(self, user_symbol: str, side: str, amount: float,
                                stop_price: float, position_side: str = "SHORT",
                                kind: str = "futures") -> dict | None:
        """条件市价单（止损/止盈触发）。ccxt 会把 stopLossPrice 映射成 STOP_MARKET。

        返回 None 表示不支持或下单失败 —— 调用方必须自行准备软件兜底，不能假设
        交易所一定挂上了止损。
        """
        if self.dry_run:
            return {"order_id": f"dry-stop-{int(time.time()*1000)}", "dry_run": True}
        try:
            o = await self._safe_call(self.futures.create_order(
                self.to_ccxt(user_symbol, "futures"), "market", side, amount, None,
                {"stopLossPrice": stop_price, "positionSide": position_side.upper()}), timeout=20)
            return {"order_id": str(o.get("id", "")), "dry_run": False}
        except Exception as e:
            code = _extract_code(str(e))
            s = _throttle.emit(f"stop_err:{user_symbol}:{code}")
            if s is not None:
                log.warning(f"Stop order failed {user_symbol} {side} {amount}@{stop_price}: {e}{s}")
            return None

    async def spot_screen(self, days: int = 7, universe: int = 100,
                          min_quote_volume: float = 3_000_000.0) -> dict:
        """现货标的的 N 日排行榜数据（供「跌幅/涨幅/振幅」三种筛选）。

        先用一次 fetch_tickers 拿到现货全市场的 24h 成交额，取成交额最大的
        `universe` 个作为候选（冷门币的 K 线没参考价值，也省掉几百次请求），
        再逐个拉日线算 N 日涨跌幅与振幅。

        口径（实测踩过坑，别改松）：
          · **K 线必须新鲜**：最后一根超过 48 小时 = 已停止交易/退市。
            实测 NFPUSDT 的 K 线停在 3 个多月前，却按「97 天前的开盘 vs 停牌价」
            算出了 -77% 的「30 日跌幅」——完全不是 30 日的数据。
          · **窗口必须完整**：K 线不足 N+1 根 = 上市不满 N 天，「N 日涨跌」口径
            不成立（短窗口的极端值会污染榜单）。
          · **失败要重试**：候选池逐个拉 K 线，任何一次限频超时都会把这个币
            静默挤出榜单——头部几个恰好失败时，跌幅榜第一从 -77% 变成 -17%，
            用户看到的就是一份错误的榜。

        返回 {items: [...], ok: int, failed: int}；failed 占比高时调用方**不得缓存**。
        """
        await self.ensure_spot()
        tickers = await self._safe_call(self._spot.fetch_tickers(), timeout=25)
        cands = []
        for ccxt_sym, t in (tickers or {}).items():
            m = self._spot_markets.get(ccxt_sym)
            if not m:
                continue
            qv = float((t or {}).get("quoteVolume") or 0)
            last = float((t or {}).get("last") or 0)
            if qv < min_quote_volume or last <= 0:
                continue
            cands.append((qv, ccxt_sym, last))
        cands.sort(reverse=True)
        cands = cands[:max(1, universe)]

        items: list[dict] = []
        stats = {"failed": 0}
        n = max(1, int(days))
        sem = asyncio.Semaphore(5)               # 限并发：别和引擎的实时下单抢接口带宽

        async def one(ccxt_sym: str, last: float, qv: float):
            k = None
            # 失败重试一次：限频窗口里的单次超时不该把这个币挤出榜单
            for attempt in range(2):
                async with sem:
                    try:
                        k = await self._safe_call(
                            self._spot.fetch_ohlcv(ccxt_sym, "1d", None, n + 1), timeout=15)
                        break
                    except Exception:
                        k = None
                        if attempt == 0:
                            await asyncio.sleep(1.0)
            if not k or len(k) < 2:
                stats["failed"] += 1
                return
            # 退市/停牌剔除：最后一根 K 线太旧 = 这个币已经不在正常交易
            if self._spot.milliseconds() - int(k[-1][0]) > 48 * 3600 * 1000:
                stats["failed"] += 1
                return
            # 窗口完整性：K 线不足 N+1 根 = 上市不满 N 天，口径不成立
            if len(k) < n + 1:
                stats["failed"] += 1
                return
            base = float(k[0][1])                # N 天前的开盘价
            hi = max(float(x[2]) for x in k)
            lo = min(float(x[3]) for x in k)
            close = float(k[-1][4])
            if base <= 0 or lo <= 0:
                stats["failed"] += 1
                return
            items.append({
                "symbol": self.user_symbol(ccxt_sym),
                "price": close or last,
                "change": (close - base) / base,          # 负=跌，正=涨
                "range": (hi - lo) / close,               # N 日振幅（口径与选币器一致）
                "quote_volume": qv,
                "days": len(k) - 1,
            })

        await asyncio.gather(*(one(s, l, q) for q, s, l in cands))
        return {"items": items, "ok": len(items), "failed": stats["failed"]}

    async def fetch_my_trades(self, user_symbol: str, since_ms: int = None,
                              limit: int = 100) -> list[dict]:
        """该标的的成交明细（用于还原外部/手工平仓的真实成交价）。"""
        try:
            return await self._safe_call(self.futures.fetch_my_trades(
                self.to_ccxt(user_symbol, "futures"), since_ms, limit), timeout=20)
        except Exception as e:
            log.warning(f"fetch_my_trades {user_symbol} failed: {e}")
            return []

    async def fetch_open_algo_orders(self, user_symbol: str) -> list[dict]:
        """条件单（止损/止盈）走的是币安「算法单」接口，**不在** fetch_open_orders 里。"""
        if self.dry_run:
            return []
        try:
            rows = await self._safe_call(
                self.futures.fapiPrivateGetOpenAlgoOrders({}), timeout=15)
        except Exception as e:
            log.warning(f"fetch open algo orders failed: {e}")
            return []
        return [o for o in (rows or []) if o.get("symbol") == user_symbol]

    async def cancel_algo_orders(self, user_symbol: str) -> int:
        """撤销该标的的全部算法单（条件单）。普通 cancel_order 撤不掉它们。"""
        if self.dry_run:
            return 0
        n = 0
        for o in await self.fetch_open_algo_orders(user_symbol):
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            try:
                await self._safe_call(
                    self.futures.fapiPrivateDeleteAlgoOrder({"algoId": algo_id}), timeout=15)
                n += 1
            except Exception as e:
                log.warning(f"cancel algo order {algo_id} ({user_symbol}) failed: {e}")
        return n

    async def cancel_order(self, order_id: str, user_symbol: str, kind: str = "futures") -> bool:
        if self.dry_run or not order_id or order_id.startswith("dry-"):
            return True
        ex = self.futures if kind == "futures" else self._spot
        try:
            await self._safe_call(ex.cancel_order(order_id, self.to_ccxt(user_symbol, kind)), timeout=15)
            return True
        except Exception as e:
            log.warning(f"Cancel order failed {user_symbol} {order_id}: {e}")
            return False

    async def fetch_order_status(self, order_id: str, user_symbol: str,
                                 kind: str = "futures") -> str:
        """单笔订单状态：open / closed / canceled / expired / unknown。"""
        if not order_id or order_id.startswith("dry-"):
            return "open"
        ex = self.futures if kind == "futures" else self._spot
        try:
            o = await self._safe_call(ex.fetch_order(order_id, self.to_ccxt(user_symbol, kind)), timeout=15)
            return str(o.get("status") or "unknown")
        except Exception:
            return "unknown"

    async def fetch_order_fill(self, order_id: str, user_symbol: str,
                               kind: str = "futures") -> float | None:
        """已成交订单的**实际成交均价**；拿不到返回 None。

        为什么需要它：跌破下轨的补仓单可能挂在市价之上（立即成交），此时
        实际成交价远好于挂单价。按挂单价记账会虚增成本，必须取交易所均价。
        """
        if not order_id or order_id.startswith("dry-"):
            return None
        ex = self.futures if kind == "futures" else self._spot
        try:
            o = await self._safe_call(ex.fetch_order(order_id, self.to_ccxt(user_symbol, kind)),
                                      timeout=15)
            avg = float(o.get("average") or 0)
            return avg if avg > 0 else None
        except Exception:
            return None

    async def fetch_open_orders(self, user_symbol: str, kind: str = "futures",
                                strict: bool = False) -> list[dict]:
        # 短 TTL 缓存：引擎 tick 和 Web status() 都会拉挂单，8 秒内共享一次请求，
        # 让「已挂/待挂」的展示以交易所为准而不至于打爆限频。
        # strict=True：接口失败时**抛出**而不是返回空表 —— 对账场景下「查不到」
        # 绝不能被当成「交易所上一条挂单都没有」，否则会误判全部挂单消失。
        key = (user_symbol, kind)
        now = time.monotonic()
        hit = self._oo_cache.get(key)
        if hit and (now - hit[1]) < 8.0:
            return hit[0]
        ex = self.futures if kind == "futures" else self._spot
        try:
            rows = await self._safe_call(ex.fetch_open_orders(self.to_ccxt(user_symbol, kind)), timeout=15)
        except Exception as e:
            log.warning(f"fetch_open_orders {user_symbol} failed: {e}")
            if strict:
                raise
            return hit[0] if hit else []
        self._oo_cache[key] = (rows, now)
        return rows
