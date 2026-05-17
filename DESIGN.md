# EvoClaw - 高速微盈利交易系统 设计文档

> 最后更新: 2026-05-17 | 版本: v1.6 (矩阵去 USDT 后缀 + 自适应文字 + 盈利趋势图表 + API 缓存 + USDC 过滤)

## 一、系统设计目标

| 目标 | 说明 |
|------|------|
| **速度优先** | 每秒轮询持仓，盈利0.2%立即平仓，与时间赛跑 |
| **极简功能** | 只干一件事：开仓→盈利平仓，不做复杂策略 |
| **低内存** | 7×24运行，进程常驻内存<100MB |
| **API节省** | 减少无效调用，批量获取数据，避免频繁请求 |
| **双保险盈利** | 单币种盈利平仓 + 账户总盈利全平，二者缺一不可 |
| **动态选币** | 按成交量和价格自动筛选交易对，无需手动维护币种列表 |
| **持仓状态** | 独立展示当前持仓多空分布与总盈亏（v1.3） |
| **多空统计** | 分别统计多单/空单的平仓盈利与盈利率（v1.3） |

## 二、技术架构选型

### 2.1 技术栈

| 层 | 技术 | 理由 |
|----|------|------|
| 后端运行时 | **Python 3.12 + asyncio** | 异步I/O、极低内存、ccxt原生支持 |
| 交易所SDK | **ccxt v4.5** (`binanceusdm`) | USDT-M永续合约专用类，支持Hedge Mode双向持仓 |
| HTTP API | **aiohttp** | 异步Web框架，与ccxt同源 |
| 数据库 | **SQLite** (WAL模式) | 零配置、零依赖、单文件、异步友好 |
| 前端 | **单HTML + 原生JS** | 无构建步骤、手机适配、<50KB |
| 配置 | **JSON文件** | 热加载、无需重启 |

### 2.2 架构图

```
┌─────────────────────────────────────────────────────┐
│                    asyncio Event Loop                │
│                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ Exchange │  │   Trader     │  │ Web Server   │   │
│  │ Client   │  │   (Engine)   │  │  (aiohttp)   │   │
│  │          │  │              │  │              │   │
│  │ • 符号解析│  │ • 动态选币   │  │ • 配置管理   │   │
│  │ • 精度计算│  │ • 开仓+追踪  │  │ • 统计查询   │   │
│  │ • 下单   │  │ • 平仓+记录  │  │ • 静态HTML   │   │
│  │ • 持仓查询│  │ • 补仓       │  │ • 手动刷新   │   │
│  └────┬─────┘  └──────┬───────┘  └──────────────┘   │
│       │               │                              │
│  ┌────▼───────────────▼──────────────────────────┐  │
│  │          Database (SQLite + WAL)               │  │
│  │  • trades 表 (历史成交，含手续费)               │  │
│  │  • open_positions 表 (系统当前管理持仓)         │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**关键设计决策**：
- 使用 `ccxt.binanceusdm` 而非 `ccxt.binance` — 只有 `binanceusdm` 能正确处理 USDT-M 合约的 Hedge Mode
- 无独立的 market.py 模块 — 精度计算集成在 `ExchangeClient` 中，减少模块间依赖
- 双表设计：`trades` 记录历史，`open_positions` 追踪系统当前管理的持仓
- **动态选币**：通过成交量和价格阈值自动筛选候选币种，24小时刷新一次（可手动刷新）

## 三、模块设计

### 3.1 项目目录结构

```
/home/claudeuser/EvoClaw/
├── main.py                  # 入口：启动所有模块
├── restart.sh               # **v1.4** 一键重启脚本（停止→清理→启动）
├── config.json              # 配置文件（前端可修改，热加载）
├── DESIGN.md                # 本文档
├── requirements.txt         # 依赖清单
├── exchange_client.py       # 交易所客户端（符号解析+精度计算+下单+自动选币）
├── trader.py                # 交易引擎核心（动态选币/开仓/平仓/补仓/全平）
├── database.py              # SQLite数据库操作（双表+手续费+统计）
├── web_server.py            # Web服务（API + 前端 + 手动刷新币种）
├── data/
│   ├── evoclaw.db           # SQLite数据库文件
│   └── trader.log           # 旋转日志文件
└── web/
    └── index.html           # 前端单页应用
```

### 3.2 模块详细设计

#### Module 1: `config.json` - 配置

```json
{
    "exchange": "binance",
    "exchange_kwargs": {
        "apiKey": "xxx",
        "secret": "xxx"
    },
    "side": "both",
    "profit_threshold": 0.002,
    "replenish_stop_threshold": 0.10,
    "enable_all_close": false,
    "all_close_threshold": 0.002,
    "skip_symbols": [],
    "enable_margin_call": false,
    "margin_call_threshold_long": 0.25,
    "margin_call_threshold_short": 0.25,
    "margin_call_multiplier": 2,
    "position_check_interval": 1,
    "volume_threshold": 50000000,
    "price_threshold": 0.1,
    "symbol_refresh_interval": 86400
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `exchange` | str | `"binance"` | 交易所标识（固定用 binanceusdm） |
| `exchange_kwargs` | dict | - | 交易所API密钥等参数 |
| ~~`symbols`~~ | ~~list[str]~~ | ~~`[]`~~ | ~~【已废弃】原手动交易对列表，现由 volume_threshold + price_threshold 自动筛选替代~~ |
| `side` | str | `"both"` | `"long"` / `"short"` / `"both"` 多空方向 |
| `profit_threshold` | float | `0.002` | 单币种平仓毛盈利率阈值 |
| `replenish_stop_threshold` | float | `0.10` | **v1.4** 补仓停补阈值：对方持仓开仓价与市价偏离≥此值时，停止补该方向仓位 |
| `enable_all_close` | bool | `false` | 是否启用账户级全平功能 |
| `all_close_threshold` | float | `0.002` | 全平触发毛盈利率阈值 |
| `skip_symbols` | list[str] | `[]` | 跳过盈利平仓的币种白名单（全平、加仓也不处理） |
| `enable_margin_call` | bool | `false` | 是否启用亏损加仓功能 |
| `margin_call_threshold` | float | `0.01` | 【已废弃，向后兼容】原统一亏损加仓阈值 |
| `margin_call_threshold_long` | float | `0.25` | **v1.4** 多单（long）亏损加仓触发阈值 |
| `margin_call_threshold_short` | float | `0.25` | **v1.4** 空单（short）亏损加仓触发阈值 |
| `margin_call_multiplier` | float | `2` | 加仓倍数（相对于最小合约数） |
| `enable_single_pair_close` | bool | `false` | 是否启用单币种多空对平功能 |
| `pair_close_threshold` | float | `0.002` | 单币种对平触发盈利率（可独立于 profit_threshold） |
| `position_check_interval` | int | `1` | 持仓轮询间隔（秒） |
| `volume_threshold` | float | `50000000` | 24h成交量筛选阈值（USDT），满足条件的币才进入候选列表 |
| `price_threshold` | float | `0.1` | 币单价筛选阈值（USDT），**只筛选价格 <= 此值的币种** |
| `symbol_refresh_interval` | int | `86400` | 币种列表自动刷新间隔（秒），默认24小时 |

**热加载机制**：Trader 每次 tick 开头重新读取 config.json，配置修改后即时生效，无需重启。

#### Module 2: `exchange_client.py` - 交易所客户端

**核心职责**：封装 ccxt，提供符号解析、精度计算、下单、持仓查询、**自动选币**。

```python
class ExchangeClient:
    def __init__(self, config: dict):
        exchange_class = getattr(ccxt, "binanceusdm")
        self.exchange = exchange_class({...})
        self.market_info = {}       # ccxt symbol → market dict
        self.symbol_map = {}        # ENAUSDT → ENA/USDT:USDT
        self._reverse_map = {}      # ENA/USDT:USDT → ENAUSDT
        self._prices = {}           # 价格缓存（用于精度计算）
```

**符号解析机制**（重要）：

用户配置使用 `ENAUSDT` 格式，但 ccxt 内部使用 `ENA/USDT:USDT` 格式。需要在两个方向之间做映射。

```python
async def load_markets(self):
    """启动时加载所有swap市场，建立双向映射"""
    markets = await self.exchange.load_markets()
    for symbol in self.exchange.symbols:
        m = markets.get(symbol)
        if m and m.get("swap"):
            self.market_info[symbol] = m
            exchange_id = m.get("id", "")       # "ENAUSDT"
            base = symbol.replace(":", "").replace("/", "")  # "ENAUSDT"
            # 正向映射：用户符号 → ccxt符号
            self.symbol_map[exchange_id] = symbol
            self.symbol_map[base] = symbol
            # 反向映射：ccxt符号 → 用户符号
            self._reverse_map[symbol] = exchange_id if exchange_id else base

def resolve_symbol(self, symbol: str) -> str:
    """用户符号 → ccxt符号 (ENAUSDT → ENA/USDT:USDT)"""
    if symbol in self.market_info:
        return symbol
    if symbol in self.symbol_map:
        return self.symbol_map[symbol]
    return symbol  # 降级：直接返回

def user_symbol(self, ccxt_symbol: str) -> str:
    """ccxt符号 → 用户符号 (ENA/USDT:USDT → ENAUSDT)"""
    return self._reverse_map.get(ccxt_symbol, ccxt_symbol)
```

> **踩坑记录**：ccxt 的 `fetch_positions()` 返回的 `symbol` 字段是 ccxt 格式（`ENA/USDT:USDT`），必须用 `user_symbol()` 转换后才能与筛选出的币种比较。

**自动选币**（v1.2 新增）：

```python
async def get_candidate_symbols(self, volume_threshold: float, price_threshold: float) -> list[str]:
    """按24h成交量和最新价格自动筛选候选币种。
    条件：volume >= volume_threshold AND price <= price_threshold
    返回用户格式符号列表（如 ["ENAUSDT", "DOGEUSDT", ...]）
    """
    tickers = await self.exchange.fetch_tickers()
    candidates = []
    for ccxt_sym, ticker in tickers.items():
        if ccxt_sym not in self.market_info:
            continue
        last = float(ticker.get("last", 0) or 0)
        volume = float(ticker.get("quoteVolume", 0) or 0)
        if last <= price_threshold and volume >= volume_threshold:
            user_sym = self.user_symbol(ccxt_sym)
            if user_sym:
                candidates.append(user_sym)
    return candidates
```

> **设计说明**：
> - `price_threshold` 是 **<=**（小于等于），目的是筛选低价币，降低每仓保证金占用
> - `volume_threshold` 是 **>=**（大于等于），目的是筛选高流动性币，确保能顺利开仓平仓
> - 调用一次 `fetch_tickers()` 即可获取所有币种的成交量和价格，比逐个 `fetch_ticker` 高效

**补仓停补检查**（v1.4-fix 新增统一方法）：

```python
def should_stop_replenish(self, sym: str, side: str, stop_threshold: float, position_map: dict) -> bool:
    """判断是否应该停止补仓。安全默认：价格无法获取时返回 True（停止）。"""
    if stop_threshold <= 0:
        return False
    opposite_side = "short" if side == "long" else "long"
    opposite_pos = position_map.get(f"{sym}:{opposite_side}")
    if not opposite_pos:
        return False
    entry_price = float(opposite_pos.get("entryPrice", 0) or 0)
    if entry_price <= 0:
        return False
    resolved = self.resolve_symbol(sym)
    price = self._prices.get(resolved)
    if not price or price <= 0:
        market = self.get_market_info(sym)
        price_str = market.get("info", {}).get("lastPrice")
        if price_str:
            price = float(price_str)
    if not price or price <= 0:
        # 安全默认：价格无法获取 → 停止补仓
        log.warning(f"REPLENISH STOP {sym} {side}: price unavailable, defaulting to STOP")
        return True
    deviation = abs(entry_price - price) / entry_price
    if deviation >= stop_threshold:
        log.info(f"REPLENISH STOP {sym} {side}: deviation={deviation:.4%}")
        return True
    return False
```

> **v1.4-fix 设计决策**：
> - 统一封装到 `ExchangeClient`，所有开仓路径共用同一套判断逻辑
> - **价格无法获取时默认 STOP**（旧逻辑会跳过检查直接开仓，构成安全漏洞）
> - `replenish_missing`、`replenish_all`、`main.py 启动开仓` 三处全部调用此方法

**价格刷新**（v1.2 优化）：

```python
async def refresh_prices(self, symbols: list):
    """批量刷新价格缓存，供 calc_min_contracts 使用"""
    resolved = [self.resolve_symbol(s) for s in symbols]
    tickers = await self.exchange.fetch_tickers(resolved)
    for sym, ticker in tickers.items():
        if ticker and ticker.get("last"):
            self._prices[sym] = float(ticker["last"])
            if sym in self.market_info:
                self.market_info[sym]["info"]["lastPrice"] = str(ticker["last"])
```

> **v1.2 改动**：从逐个 `fetch_ticker` 改为批量 `fetch_tickers`，大幅减少 API 调用次数。

**精度计算**（`calc_min_contracts`）：

```python
def calc_min_contracts(self, symbol: str) -> float:
    resolved = self.resolve_symbol(symbol)
    market = self.market_info.get(resolved, {})
    if not market:
        return 1

    contract_size = float(market.get("contractSize", 1) or 1)
    min_notional = float((market.get("limits", {}).get("cost", {}).get("min", 0)) or 0)
    min_amount = float((market.get("limits", {}).get("amount", {}).get("min", 0)) or 0)
    amount_precision = int(float(market.get("precision", {}).get("amount", 0) or 0))

    raw = 1
    if min_amount > 0:
        raw = max(raw, int(math.ceil(min_amount / contract_size)))
    if min_notional > 0:
        price = self._prices.get(resolved)
        if not price or price <= 0:
            price_str = self.exchange.markets.get(resolved, {}).get("info", {}).get("lastPrice")
            if price_str:
                price = float(price_str)
        if price and price > 0:
            raw = max(raw, int(math.ceil(min_notional / (price * contract_size))))
    return float(round(raw, amount_precision))
```

> **踩坑记录**：
> - `amount_precision` 在某些交易所返回的是字符串类型（如 `"0"`），直接传给 `round()` 会报错，必须 `int(float(...))` 转换
> - `min_notional` 计算需要价格，如果 `refresh_prices` 还未执行，需要从 `market.info.lastPrice` 降级获取
> - ENAUSDT 实际计算：价格~0.128，contractSize=1，minNotional=5 → ceil(5/0.128)=40 张合约

**下单方法**（v1.2 增强重试）：

```python
async def open_position(self, symbol: str, side: str) -> dict | None:
    """市价开仓。side: "buy"(开多) / "sell"(开空)
    返回: {"order_id": str, "average": float, "amount": float} 或 None
    遇到 -4164（名义价值不足）自动增加合约数重试，最多3次。
    """
    resolved = self.resolve_symbol(symbol)
    amount = self.calc_min_contracts(resolved)
    for attempt in range(3):
        try:
            order = await self.exchange.create_order(
                symbol=resolved, type="market", side=side, amount=amount,
                params={"positionSide": "LONG" if side == "buy" else "SHORT"},
            )
            return {"order_id": str(order.get("id", "")),
                    "average": float(order.get("average", 0) or 0),
                    "amount": amount}
        except Exception as e:
            if "-4164" in str(e) and attempt < 2:
                old = amount
                amount = max(amount + 1, int(amount * 1.3))
                log.warning(f"Notional too small for {resolved} {side}, retry {old} -> {amount}")
                await asyncio.sleep(0.2)
                continue
            log.error(f"Open position failed {resolved} {side}: {e}")
            return None
```

> **v1.2 增强**：遇到币安 `-4164`（Order's notional must be no smaller than 5）错误时，自动把合约数增加 30% 重试，最多 3 次。解决价格下跌后原计算合约数不足的问题。

```python
async def close_position(self, symbol: str, side: str) -> dict | None:
    """市价平仓。side: "long"(平多) / "short"(平空)
    返回: {"order_id": str, "average": float, "closedPnL": float, "contracts": float} 或 None
    """
    resolved = self.resolve_symbol(symbol)
    positions = await self.get_positions([resolved])
    contracts = float(target.get("contracts", 0) or 0)
    close_side = "sell" if side == "long" else "buy"
    order = await self.exchange.create_order(
        symbol=resolved, type="market", side=close_side, amount=contracts,
        params={"positionSide": side.upper()},
    )
    return {
        "order_id": str(order.get("id", "")),
        "average": float(order.get("average", 0) or 0),
        "closedPnL": order.get("closedPnL", 0),
        "contracts": contracts,
    }
```

> **踩坑记录**：
> - 开仓时 `params` 只需要 `positionSide`，不能加 `reduceOnly: False`（多余参数会报错）
> - 平仓时只用 `params={"positionSide": side.upper()}` 即可，**不能加 `reduceOnly`**（`-1106: Parameter 'reduceonly' sent when not required`）
> - 不能使用 `closePosition: True`（`-4136: Target strategy invalid for orderType MARKET,closePosition true`），因为账户绑定了 TP/SL 策略
> - `positionSide` 的值必须是 `"LONG"` 或 `"SHORT"`（大写），对应持仓方向而非下单方向
> - 平仓失败后 fallback：去掉所有 params 再重试

**加仓方法**（v1.1 新增）：

```python
async def add_position(self, symbol: str, side: str, amount: float) -> dict | None:
    """在已有持仓上追加合约数（亏损加仓使用）
    side: "long" / "short"（持仓方向）
    返回: {"order_id": str, "average": float, "amount": float} 或 None
    """
    resolved = self.resolve_symbol(symbol)
    open_side = "buy" if side == "long" else "sell"
    order = await self.exchange.create_order(
        symbol=resolved, type="market", side=open_side, amount=amount,
        params={"positionSide": side.upper()},
    )
```

> **注意**：加仓不加 `reduceOnly`，直接追加同方向市价单。

#### Module 3: `trader.py` - 交易引擎（核心）

**这是整个系统的心脏，管理动态选币、交易逻辑和系统持仓追踪。**

```python
class Trader:
    def __init__(self, exchange_client, database, config_path: str = "config.json"):
        self.client = exchange_client
        self.db = database
        self.config_path = config_path
        self.running = False
        self.config = self._load_config()
        self._candidate_symbols = []          # v1.2: 缓存的候选币种列表
        self._last_symbol_refresh = 0         # v1.2: 上次刷新时间戳
        self._refresh_lock = asyncio.Lock()   # v1.2: 刷新锁
```

**主循环**：

```python
async def run(self):
    self.running = True
    while self.running:
        try:
            await self.tick()
        except Exception as e:
            log.error(f"Tick error: {e}")
        await asyncio.sleep(self._get_config().get("position_check_interval", 1))
```

**币种缓存与刷新机制**（v1.2 新增）：

```python
async def _ensure_symbols(self):
    """刷新候选币种如果间隔已过期。使用缓存避免每秒请求交易所。"""
    cfg = self._get_config()
    interval = cfg.get("symbol_refresh_interval", 86400)
    now = datetime.now(timezone.utc).timestamp()
    async with self._refresh_lock:
        if not self._candidate_symbols or (now - self._last_symbol_refresh) >= interval:
            volume_threshold = cfg.get("volume_threshold", 0)
            price_threshold = cfg.get("price_threshold", 0)
            self._candidate_symbols = await self.client.get_candidate_symbols(
                volume_threshold, price_threshold
            )
            self._last_symbol_refresh = now
            log.info(f"Symbols refreshed: {len(self._candidate_symbols)} (interval={interval}s)")

async def refresh_symbols_now(self):
    """手动刷新候选币种（由前端 /api/refresh-symbols 调用）。"""
    async with self._refresh_lock:
        cfg = self._get_config()
        self._candidate_symbols = await self.client.get_candidate_symbols(
            cfg.get("volume_threshold", 0), cfg.get("price_threshold", 0)
        )
        self._last_symbol_refresh = datetime.now(timezone.utc).timestamp()
        log.info(f"Symbols manually refreshed: {len(self._candidate_symbols)}")
    return self._candidate_symbols
```

> **设计说明**：
> - `_candidate_symbols` 是内存缓存，tick() 直接使用缓存，不再每秒请求交易所
> - 自动刷新间隔由 `symbol_refresh_interval` 控制（默认 86400 秒 = 24小时）
> - 手动刷新通过 `/api/refresh-symbols` POST 接口触发

**tick() 执行阶段**（v1.2 重构）：

```python
async def tick(self):
    cfg = self._get_config()
    sides = self._get_sides()
    skip = set(cfg.get("skip_symbols", []))

    # STEP 0: 动态选币（使用缓存，24小时刷新一次）
    await self._ensure_symbols()
    candidate_symbols = self._candidate_symbols
    if not candidate_symbols:
        return

    # STEP 1: 刷新价格并获取候选币种的交易所持仓
    await self.client.refresh_prices(candidate_symbols)
    exchange_positions = await self.client.get_positions(candidate_symbols)

    # STEP 2: 全平检查（只看系统跟踪的持仓）
    if cfg.get("enable_all_close", False):
        await self.check_all_close(candidate_symbols, sides)
    exchange_positions = await self.client.get_positions(candidate_symbols)

    # STEP 3: 单币种盈利平仓（【所有持仓】，不限于候选列表）
    all_positions = await self.client.get_positions()
    for p in list(all_positions):
        symbol = self.client.user_symbol(p["symbol"])
        if symbol in skip:
            continue
        await self.check_single_close(p, symbol, p.get("side"))
    exchange_positions = await self.client.get_positions(candidate_symbols)
    all_positions = await self.client.get_positions()

    # STEP 3.5: 单币种多空对平（【所有持仓】有 long+short 的币种）
    if cfg.get("enable_single_pair_close", False):
        await self.check_single_pair_close(all_positions, skip)
    exchange_positions = await self.client.get_positions(candidate_symbols)

    # STEP 4: 亏损加仓（【所有持仓】，排除白名单）
    if cfg.get("enable_margin_call", False):
        all_positions_for_margin = await self.client.get_positions()
        await self.check_margin_call(all_positions_for_margin, skip)
    exchange_positions = await self.client.get_positions(candidate_symbols)

    # STEP 5: 补仓缺失的持仓（只在候选列表中补）
    await self.replenish_missing(exchange_positions, candidate_symbols, sides)
```

> **v1.2 关键改动**：
> - 平仓（single/pair/all）和加仓 **不再局限于候选列表**，而是扫描**所有持仓**（排除白名单）
> - 只有**下单/补仓**使用 `candidate_symbols`
> - 这样实现完整闭环：某币被平仓后如果不在候选列表 → 不补仓；但该币持仓亏损 → 仍然会加仓；加仓后盈利 → 仍然会平仓

**系统持仓追踪机制**（核心设计）：

系统通过 `open_positions` 数据库表追踪自己打开的每一笔持仓。平仓和补仓操作**对所有持仓生效**（不管谁开的仓），DB 追踪用于：
- 记录系统开仓的入场价和合约数
- 防止重复开仓
- 统计交易历史
- **记录开仓手续费**（v1.2 新增）

```
开仓成功 → db.record_open(symbol, side, order_id, entry_price, amount, open_fee)
           ↓
      open_positions 表新增记录（含 open_fee）
           ↓
    tick() → db.has_open(symbol, side) → True
           ↓
      盈利率达标 → close_position() → db.remove_open(symbol, side)
           ↓
      open_positions 表删除记录
           ↓
    replenish_missing() → db.has_open()=False + 交易所无持仓 → 重新开仓
```

**开仓与追踪**（v1.2 含手续费）：

```python
async def _do_open(self, symbol: str, open_side: str, side: str):
    """开仓并记录到数据库，同时记录开仓手续费。"""
    result = await self.client.safe_open(symbol, open_side)
    if result:
        market = self.client.get_market_info(symbol)
        contract_size = market.get("contractSize", 1) or 1
        open_fee = result["average"] * result["amount"] * contract_size * 0.0005
        self.db.record_open(
            symbol=symbol, side=side,
            order_id=result["order_id"],
            entry_price=result["average"],
            amount=result["amount"],
            open_fee=open_fee,          # v1.2: 记录开仓手续费
        )
    else:
        log.warning(f"_do_open failed: {symbol} {side} ({open_side})")
```

> **手续费计算**：买入 0.05%，即 `entry_price * contracts * contract_size * 0.0005`

**补仓逻辑**（v1.4-fix 统一停补检查）：

```python
async def replenish_missing(self, positions, symbols, sides):
    cfg = self._get_config()
    stop_threshold = cfg.get("replenish_stop_threshold", 0)

    # v1.4-fix: 获取所有持仓以检查对方方向的 entry_price
    all_positions = await self.client.get_positions()
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

    tasks = []
    for sym in symbols:          # 只在候选列表中遍历
        for side in sides:
            key = f"{sym}:{side}"
            if key not in current and not self.db.has_open(sym, side):
                # v1.4-fix: 统一停补检查（价格未知默认 STOP）
                if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                    continue
                open_side = "buy" if side == "long" else "sell"
                tasks.append(self._do_open(sym, open_side, side))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
```

```python
async def replenish_all(self, symbols, sides):
    # v1.4-fix: 全平后补仓也需要执行停补检查
    all_positions = await self.client.get_positions()
    position_map = {}
    for p in all_positions:
        sym = self.client.user_symbol(p["symbol"])
        side = p.get("side")
        position_map[f"{sym}:{side}"] = p

    cfg = self._get_config()
    stop_threshold = cfg.get("replenish_stop_threshold", 0)

    tasks = []
    for sym in symbols:
        for side in sides:
            if self.client.should_stop_replenish(sym, side, stop_threshold, position_map):
                continue
            open_side = "buy" if side == "long" else "sell"
            tasks.append(self._do_open(sym, open_side, side))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
```

> **v1.4-fix 加固**：
> - 提取 `should_stop_replenish()` 到 `ExchangeClient`，所有开仓路径共用同一逻辑
> - **价格获取失败时默认 STOP**（修复旧逻辑中 price 为 None 导致检查被跳过的漏洞）
> - `replenish_all`（全平后补仓）和 `main.py` 启动开仓 也纳入停补检查，覆盖全部开仓路径

**单币种盈利平仓**（v1.2 含 open_fee 传递）：

```python
async def check_single_close(self, position, symbol, pos_side):
    unrealized_pnl = float(position.get("unrealizedPnl", 0) or 0)
    entry_price = float(position.get("entryPrice", 0) or 0)
    contracts = float(position.get("contracts", 0) or 0)
    market = self.client.get_market_info(symbol)
    contract_size = market.get("contractSize", 1) or 1
    position_value = entry_price * contracts * contract_size

    if position_value <= 0:
        return

    profit_rate = unrealized_pnl / position_value
    if profit_rate >= cfg.get("profit_threshold", 0.002):
        result = await self.client.close_position(symbol, pos_side)
        if result:
            # v1.2: 获取 open_fee 后再移除
            open_fee = 0
            if self.db.has_open(symbol, pos_side):
                for sp in self.db.get_open_positions():
                    if sp["symbol"] == symbol and sp["side"] == pos_side:
                        open_fee = sp.get("open_fee", 0)
                        break
            self.db.remove_open(symbol, pos_side)
            await self._record_trade(
                symbol=symbol, side=pos_side,
                entry_price=entry_price, contracts=contracts,
                close_result=result, trade_type="single",
                open_fee=open_fee,      # v1.2: 传入开仓手续费
            )
```

**全平逻辑**（只计算系统持仓）：

```python
async def check_all_close(self, symbols, sides):
    system_positions = self.db.get_open_positions()
    if not system_positions:
        return

    system_symbols = [sp["symbol"] for sp in system_positions]
    exchange_positions = await self.client.get_positions(system_symbols)

    system_map = {f"{sp['symbol']}:{sp['side']}": sp for sp in system_positions}

    total_pnl = 0.0
    total_value = 0.0
    for p in exchange_positions:
        sym = self.client.user_symbol(p["symbol"])
        pos_side = p.get("side")
        if f"{sym}:{pos_side}" in system_map:
            total_pnl += float(p.get("unrealizedPnl", 0) or 0)
            total_value += entry_price * contracts * contract_size

    if total_value > 0 and total_pnl / total_value >= threshold:
        for sp in system_positions:
            result = await self.client.close_position(sp["symbol"], sp["side"])
            if result:
                self.db.remove_open(sp["symbol"], sp["side"])
                await self._record_trade(
                    symbol=sp["symbol"], side=sp["side"],
                    entry_price=sp["entry_price"], contracts=sp["amount"],
                    close_result=result, trade_type="all_close",
                    open_fee=sp.get("open_fee", 0),   # v1.2
                )
        await self.replenish_all(symbols, sides)
```

**单币种多空对平**（v1.2 改为扫描所有持仓）：

```python
async def check_single_pair_close(self, all_positions, skip):
    """扫描所有持仓（不限于候选列表），排除白名单。"""
    by_symbol = {}
    for p in all_positions:
        sym = self.client.user_symbol(p["symbol"])
        if sym in skip:
            continue
        by_symbol.setdefault(sym, {})[p.get("side")] = p

    for sym, pair in by_symbol.items():
        if not pair or "long" not in pair or "short" not in pair:
            continue
        # 计算 avg_rate = (long_rate + short_rate) / 2
        # if avg_rate >= threshold → 双向平仓
```

**亏损加仓**（v1.4-fix：可重复触发 + 按当前持仓量加仓）：

```python
async def check_margin_call(self, all_positions, skip):
    """扫描所有持仓（不限于候选列表），排除白名单。
    可重复触发：每轮 tick 只要亏损率仍 >= 阈值，就会再次加仓。
    """
    cfg = self.config
    threshold_long = cfg.get("margin_call_threshold_long", cfg.get("margin_call_threshold", 0.01))
    threshold_short = cfg.get("margin_call_threshold_short", cfg.get("margin_call_threshold", 0.01))
    multiplier = cfg.get("margin_call_multiplier", 2)

    for p in all_positions:
        sym = self.client.user_symbol(p["symbol"])
        if sym in skip:
            continue
        side = p.get("side")
        # ... 计算 loss_rate ...
        threshold = threshold_long if side == "long" else threshold_short
        if loss_rate >= threshold:
            # v1.4-fix: 加仓数量 = 当前持仓合约数 × multiplier
            add_amount = contracts * multiplier
            # add_position + 累加 open_fee
```

> **v1.4-fix 改动**：
> 1. 移除 `margin_called` 一次性限制。加仓**可重复触发**：只要亏损率持续 ≥ 阈值，每轮 tick 都会继续加仓。
> 2. 加仓数量从 `calc_min_contracts × multiplier` 改为 `当前持仓合约数 × multiplier`。即加仓量与现有仓位成正比，而非固定最小合约数。
>
> **加仓手续费**：加仓时累加 `added_fee = result["average"] * add_amount * cs * 0.0005` 到 `open_positions.open_fee`

**交易记录**（v1.2 含手续费）：

```python
async def _record_trade(self, symbol, side, entry_price, contracts, close_result, trade_type, open_fee=0):
    market = self.client.get_market_info(symbol)
    contract_size = market.get("contractSize", 1) or 1
    exit_price = float(close_result.get("average", 0) or 0)
    pnl = float(close_result.get("closedPnL", 0) or 0)
    if pnl == 0 and exit_price > 0:
        if side == "long":
            pnl = (exit_price - entry_price) * contracts * contract_size
        else:
            pnl = (entry_price - exit_price) * contracts * contract_size
    position_value = entry_price * contracts * contract_size
    pnl_rate = pnl / position_value if position_value > 0 else 0

    # v1.2: 手续费计算（买入0.05% + 卖出0.05% = 0.1%）
    close_fee = exit_price * contracts * contract_size * 0.0005
    if open_fee <= 0:
        open_fee = entry_price * contracts * contract_size * 0.0005
    fee = open_fee + close_fee

    self.db.insert_trade({
        "symbol": symbol, "side": side, "type": trade_type,
        "entry_price": entry_price, "exit_price": exit_price,
        "amount": contracts, "pnl": pnl, "pnl_rate": pnl_rate,
        "fee": fee,                          # v1.2: 总手续费
        "close_time": datetime.now(timezone.utc).isoformat(),
    })
```

> **手续费规则**：
> - 开仓手续费 = `entry_price * contracts * contract_size * 0.0005`（0.05%）
> - 平仓手续费 = `exit_price * contracts * contract_size * 0.0005`（0.05%）
> - 总手续费 = open_fee + close_fee = 0.1%
> - 如果 `open_fee` 未传入（如手动持仓），按 entry_price 估算

#### Module 4: `database.py` - 数据库层

**双表设计**（v1.2 含手续费字段）：

| 表名 | 用途 | 生命周期 |
|------|------|----------|
| `trades` | 历史记录所有已平仓交易（含手续费） | 只增不减，永久保存 |
| `open_positions` | 追踪系统当前管理的未平仓持仓（含开仓手续费） | 开仓时插入，平仓时删除 |

```python
class Database:
    def __init__(self, db_path: str = "data/evoclaw.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.Lock()
```

**trades 表**（v1.2 新增 `fee` 字段）：

```sql
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,           -- 'long' or 'short'
    type TEXT NOT NULL,           -- 'single' / 'all_close' / 'pair_close'
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    amount REAL NOT NULL,         -- 合约数量
    pnl REAL NOT NULL,            -- 毛盈利（USDT）
    pnl_rate REAL NOT NULL,       -- 毛盈利率
    fee REAL NOT NULL DEFAULT 0,  -- v1.2: 总手续费（开仓+平仓）
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**open_positions 表**（v1.2 新增 `open_fee` 字段）：

```sql
CREATE TABLE IF NOT EXISTS open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    UNIQUE(symbol, side),
    order_id TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    amount REAL NOT NULL,
    margin_called INTEGER DEFAULT 0,
    open_fee REAL DEFAULT 0,      -- v1.2: 累计开仓手续费（含加仓）
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

> **迁移机制**：启动时自动 `ALTER TABLE ADD COLUMN`，已有数据保留，`fee`/`open_fee` 默认为 0。

**open_positions 操作方法**（v1.2 含 open_fee）：

```python
def record_open(self, symbol, side, order_id, entry_price, amount, open_fee=None):
    """记录系统开仓。先删除旧记录再插入，确保唯一性。"""
    if open_fee is None:
        open_fee = entry_price * amount * 0.0005
    with self.lock:
        self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
        self.conn.execute(
            "INSERT INTO open_positions (symbol, side, order_id, entry_time, entry_price, amount, open_fee) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, order_id, now, entry_price, amount, open_fee),
        )
        self.conn.commit()

def mark_margin_called(self, symbol, side, new_amount, added_fee=0):
    """标记为已加仓，同时累加手续费。"""
    with self.lock:
        self.conn.execute(
            "UPDATE open_positions SET margin_called=1, amount=?, open_fee=open_fee+? "
            "WHERE symbol=? AND side=?",
            (new_amount, added_fee, symbol, side),
        )
        self.conn.commit()
```

**统计查询**（v1.3 含多空盈亏统计）：

```python
def get_stats(self, account_balance: float = 0) -> dict:
    row = self.conn.execute("""
        SELECT
            COALESCE(SUM(pnl), 0) AS total_pnl,
            COALESCE(SUM(fee), 0) AS total_fee,
            COALESCE(MAX(pnl_rate), 0) AS max_single_profit_rate,
            CASE WHEN COUNT(*) > 0
                THEN CAST(COUNT(CASE WHEN pnl > 0 THEN 1 END) AS REAL) / COUNT(*)
                ELSE 0 END AS win_rate,
            COUNT(CASE WHEN side = 'long' THEN 1 END) AS long_count,
            COUNT(CASE WHEN side = 'short' THEN 1 END) AS short_count,
            COUNT(*) AS total_count,
            COUNT(CASE WHEN type = 'all_close' THEN 1 END) AS all_close_count,
            COUNT(CASE WHEN type = 'pair_close' THEN 1 END) AS pair_close_count,
            COUNT(CASE WHEN type = 'single' THEN 1 END) AS single_count,
            COALESCE(SUM(CASE WHEN side = 'long' THEN pnl END), 0) AS long_pnl,          -- v1.3
            COALESCE(AVG(CASE WHEN side = 'long' THEN pnl_rate END), 0) AS long_pnl_rate, -- v1.3
            COALESCE(SUM(CASE WHEN side = 'short' THEN pnl END), 0) AS short_pnl,        -- v1.3
            COALESCE(AVG(CASE WHEN side = 'short' THEN pnl_rate END), 0) AS short_pnl_rate -- v1.3
        FROM trades
    """).fetchone()

    return {
        "total_pnl": round(row[0], 4),
        "total_fee": round(row[1], 4),
        "max_single_profit_rate": round(row[2], 6),
        "account_profit_rate": round(row[0] / account_balance, 6) if account_balance > 0 else 0,
        "win_rate": round(row[3], 4),
        "long_count": row[4],
        "short_count": row[5],
        "total_count": row[6],
        "all_close_count": row[7],
        "pair_close_count": row[8],
        "single_count": row[9],
        "long_pnl": round(row[10], 4),              # v1.3
        "long_pnl_rate": round(row[11], 6),         # v1.3
        "short_pnl": round(row[12], 4),             # v1.3
        "short_pnl_rate": round(row[13], 6),        # v1.3
    }
```

#### Module 5: `web_server.py` - Web服务

```python
class WebServer:
    ROUTES:
        GET  /                → 返回 web/index.html
        GET  /api/config      → 返回当前配置
        POST /api/config      → 保存配置（写入config.json，热加载）
        GET  /api/account     → 账户余额 + 候选币种列表
        GET  /api/positions   → 当前持仓（含系统跟踪标记）
        GET  /api/stats       → 统计指标（含手续费、平仓模式）
        GET  /api/trades      → 最近交易记录
        POST /api/refresh-symbols  → v1.2: 手动刷新候选币种
```

**WebServer 构造函数**（v1.2 接收 trader 实例）：

```python
def __init__(self, exchange_client, database, config_path: str = "config.json", trader=None):
    self.client = exchange_client
    self.db = database
    self.config_path = config_path
    self.trader = trader      # v1.2: 用于访问 candidate_symbols 缓存
```

**api_account**（v1.3 增加持仓多空分布）：

```python
async def api_account(self, request):
    all_positions = await self.client.get_positions()
    # v1.3: 持仓多空分布
    long_pos_count = sum(1 for p in all_positions if p.get("side") == "long")
    short_pos_count = sum(1 for p in all_positions if p.get("side") == "short")
    # v1.3: 全部持仓的总盈亏与盈亏率
    total_pnl = 0.0; total_value = 0.0
    for p in all_positions:
        ...
    total_pnl_rate = total_pnl / total_value if total_value > 0 else 0

    # 优先使用 trader 缓存
    if self.trader and self.trader._candidate_symbols:
        symbols = self.trader._candidate_symbols
    else:
        symbols = await self.client.get_candidate_symbols(...)

    return web.json_response({
        "balance": balance,
        "unrealized_pnl": round(unrealized, 4),
        "position_count": len(all_positions),
        "long_position_count": long_pos_count,       # v1.3
        "short_position_count": short_pos_count,     # v1.3
        "total_position_pnl": round(total_pnl, 4),   # v1.3
        "total_position_pnl_rate": round(total_pnl_rate, 6), # v1.3
        "realtime_profit_rate": round(realtime_rate, 6),
        "active_symbols": symbols,
    })
```

**手动刷新币种接口**（v1.2 新增）：

```python
async def api_refresh_symbols(self, request):
    symbols = await self.trader.refresh_symbols_now()
    return web.json_response({
        "status": "ok",
        "count": len(symbols),
        "symbols": symbols,
    })
```

**API响应示例**：

```json
// GET /api/stats
{
    "total_pnl": 7.1508,
    "total_fee": 1.1087,
    "max_single_profit_rate": 0.028301,
    "account_profit_rate": 0.183163,
    "win_rate": 0.9745,
    "long_count": 193,
    "short_count": 238,
    "total_count": 431,
    "all_close_count": 0,
    "pair_close_count": 0,
    "single_count": 431,
    "long_pnl": 3.5214,           // v1.3
    "long_pnl_rate": 0.002134,    // v1.3
    "short_pnl": 3.6294,          // v1.3
    "short_pnl_rate": 0.001987    // v1.3
}

// GET /api/account
{
    "balance": 39.97,
    "available_balance": 15.50,   // v1.4: 可转出余额（availableBalance）
    "unrealized_pnl": -5.43,
    "position_count": 50,
    "long_position_count": 25,    // v1.3
    "short_position_count": 25,   // v1.3
    "total_position_pnl": -5.43,  // v1.3
    "total_position_pnl_rate": -0.021563, // v1.3
    "realtime_profit_rate": -0.021563,
    "active_symbols": ["ENAUSDT", "DOGEUSDT", "WCTUSDT", ...]
}
```

#### Module 6: `web/index.html` - 前端页面

**布局设计**（v1.3 重构）：

```
┌─────────────────────────────┐
│  EvoClaw Trading Dashboard   │
├─────────────────────────────┤
│  [交易配置]                   │
│  ┌─ 币种筛选 ─┐              │   ← v1.3 分组边框
│  │ 交易量筛选 │ 币单价筛选   │
│  │ 刷新间隔   │ [立即刷新]   │
│  ├─ 基础交易 ─┤              │   ← v1.3 分组边框
│  │ 交易方向   │ 平仓利润率   │
│  ├─ 账户级全平 ┤             │   ← v1.3 分组边框
│  │ 启用全平   │ 全平利润率   │
│  ├─ 亏损加仓 ─┤              │   ← v1.3 分组边框
│  │ 启用加仓   │ 加仓阈值     │
│  │            │ 加仓倍数     │
│  ├─ 多空对平 ─┤              │   ← v1.3 分组边框
│  │ 启用对平   │ 对平利润率   │
│  ├─ 白名单 ──┤              │   ← v1.3 分组边框
│  │ 跳过平仓白名单            │
│  └───────────┘              │
│  [保存配置] [停止交易]       │
├─────────────────────────────┤
│  [账户概览]                   │
│  当前筛选币种 (16个):        │   ← v1.3 移到卡片上方
│  ENAUSDT DOGEUSDT ...        │
│  ┌────┬────┬────┬────┬────┬────┐│
│  │余额│可转│未实│实时│全平│对冲│单项│
│  └────┴────┴────┴────┴────┴────┘│
├─────────────────────────────┤
│  [持仓状态]      ← v1.3 新增 │
│  ┌────┬────┬────┬────┬────┐│
│  │持仓│多单│空单│总盈亏│盈亏率│
│  └────┴────┴────┴────┴────┘│
├─────────────────────────────┤
│  [交易统计]                   │
│  累计盈亏  累计手续费  胜率   │
│  最高盈率  累计盈利率         │
│  多单平仓总数  空单平仓总数   │   ← v1.3 改名
│  多单平仓盈利  多单平仓盈利率 │   ← v1.3 新增
│  空单平仓盈利  空单平仓盈利率 │   ← v1.3 新增
└─────────────────────────────┘
```

**v1.3 前端改动汇总**：

| 改动 | 位置 |
|------|------|
| 移除「当前持仓列表」区块 | 整个区块删除 |
| 移除「最近成交」区块 | 整个区块删除 |
| 新增「持仓状态」独立区块 | 账户概览与交易统计之间 |
| 账户概览重新排版 | 筛选币种移到卡片上方，持仓数移出 |
| 配置区分组显示 | 6个分组：币种筛选/基础交易/全平/加仓/对平/白名单 |
| 交易统计新增4张卡片 | 多单平仓盈利、多单平仓盈利率、空单平仓盈利、空单平仓盈利率 |
| 交易统计重命名2张卡片 | 多单次数→多单平仓总数，空单次数→空单平仓总数 |

### 3.3 模块依赖关系

```
main.py
  ├── ExchangeClient (ccxt.binanceusdm)
  ├── Database (SQLite)
  ├── Trader (→ ExchangeClient, Database, Config, _candidate_symbols缓存)
  └── WebServer (→ ExchangeClient, Database, Config, Trader)
```

## 四、核心流程详解

### 4.1 启动流程

```
1. main.py 读取 config.json
2. setup_logging() → 控制台 + 文件日志（旋转，10MB×3）
3. 初始化 Database("data/evoclaw.db") → 建表 + 自动迁移 ADD COLUMN
4. 初始化 ExchangeClient(config)
5. ExchangeClient.load_markets() → 缓存所有swap市场 + 建立符号映射
6. 动态选币：get_candidate_symbols(volume_threshold, price_threshold)
7. ExchangeClient.refresh_prices(symbols) → 预取价格
8. 初始化 Trader(client, db, "config.json")
9. 初始化 WebServer(client, db, "config.json", trader=trader)  ← v1.2 传入 trader

10. 启动时开仓（不干涉现有持仓，只补缺）：
   a. 获取交易所当前持仓 → 构建 current 集合
   b. 获取 DB 中跟踪的持仓 → 合并到 current 集合
   c. 构建 position_map（用于停补检查）
   d. 对每个 candidate_symbol × side，如果不在 current 中：
      - v1.4-fix: 调用 client.should_stop_replenish() 检查停补阈值
      - 若通过检查 → client.safe_open(symbol, open_side)
      - db.record_open(symbol, side, order_id, entry_price, amount, open_fee)
   e. 如果所有持仓已存在 → 跳过，不重复开仓

11. 启动 WebServer（0.0.0.0:8080）
12. 注册信号处理（SIGINT/SIGTERM → trader.stop()）
13. 启动 Trader.run() 主循环（阻塞，直到收到信号）
```

### 4.2 单次 tick 执行时序（v1.2）

```
T0:    _get_config() → 热加载配置
T0+0:  _ensure_symbols() → 检查是否需要刷新候选列表（24小时/手动）
T0+0:  refresh_prices(candidate_symbols) → 批量刷新价格
T0+0:  get_positions(candidate_symbols) → 获取候选币种的持仓（1次API）

T0+0:  check_all_close() [如果 enable_all_close=true]
       ├─ 从 DB 获取系统持仓
       ├─ 获取系统持仓的交易所数据
       ├─ 计算总盈利率（只算系统持仓）
       ├─ 触发 → 逐个平仓 + remove_open + record_trade（含open_fee）
       └─ 补仓 → replenish_all() [asyncio.gather并发]

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  check_single_close() [扫描 ALL positions，不限于候选列表]
       ├─ symbol in skip_symbols → 跳过
       ├─ profit_rate >= threshold → 平仓 + remove_open + record_trade（含open_fee）
       └─ 继续下一个

T0+0:  get_positions() → 重新获取所有持仓（单平可能平掉了非候选币种）

T0+0:  check_single_pair_close() [扫描 ALL positions]
       ├─ 排除白名单
       ├─ 只处理同时有多空的情况
       ├─ avg_rate >= threshold → 双向平仓 + record_trade(type=pair_close)
       └─ 继续下一个币种

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  check_margin_call() [扫描 ALL positions，不限于候选列表]
       ├─ 排除白名单
       ├─ pnl >= 0 → 跳过
       ├─ loss_rate >= side-specific threshold → add_position(当前持仓×multiplier) + 累加 open_fee + mark_margin_called
       └─ 继续下一个（下一轮tick若仍满足条件，会继续加仓）

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  replenish_missing()
       ├─ 获取所有持仓 → position_map（用于停补检查）
       ├─ 构建交易所持仓集合
       ├─ 对每个 candidate_symbol × side（只在候选列表中补）：
       │   ├─ 交易所已有 → 跳过
       │   ├─ DB有跟踪 → 跳过
       │   ├─ v1.4-fix: should_stop_replenish() → 价格未知默认 STOP
       │   └─ 通过检查 → _do_open() → open + record_open（含open_fee）
       └─ asyncio.gather 并发执行
```

**性能特征**：
- 单次 tick 最少 4 次 `get_positions` 调用
- 币种筛选使用缓存，不再每秒请求交易所
- 价格刷新使用批量 `fetch_tickers`，减少 API 调用
- 开仓/平仓使用 `asyncio.gather` 并发
- 目标单次 tick 耗时 < 500ms

### 4.3 防重复开仓机制

**问题场景**：
1. 系统开仓成功后，`record_open` 写入DB，但下一tick交易所还未返回持仓 → 可能重复开仓
2. 用户手动开仓，系统检测到空缺 → 可能叠加
3. 网络延迟导致开仓结果不确定 → 可能重复

**解决方案 — 三重防护**：

| 防线 | 检查点 | 防护内容 |
|------|--------|----------|
| 第1道 | `replenish_missing` → `key not in current` | 交易所已有持仓就不开 |
| 第2道 | `replenish_missing` → `not db.has_open()` | DB已有跟踪就不开 |
| 第3道 | `record_open` → `DELETE ... INSERT` | 即使重复写入，DB中只保留一条 |

### 4.4 全平流程（系统持仓隔离）

```
tick() → enable_all_close == true ?
  ├─ NO → 跳过全平检查
  └─ YES
         ↓
         system_positions = db.get_open_positions()  ← 只取系统持仓
         ↓
         exchange_positions = client.get_positions(system_symbols)
         ↓
         total_pnl = Σ(p.unrealizedPnl)  ← 只累加系统持仓
         total_value = Σ(entryPrice × contracts × contractSize)
         ↓
         total_pnl / total_value >= all_close_threshold ?
           ├─ YES
           │     ↓
           │   for sp in system_positions:  ← 只平系统持仓
           │     close_position(sp.symbol, sp.side)
           │     db.remove_open(sp.symbol, sp.side)
           │     db.insert_trade(..., fee=open_fee+close_fee)
           │     ↓
           │   replenish_all() ← 全平后立即补仓（含停补检查）
           └─ NO → 继续单币种检查
```

**手动持仓完全隔离**：
- 手动开的仓不在 `open_positions` 表中
- `get_open_positions()` 只返回系统持仓
- 手动持仓的盈亏不参与全平计算
- 手动持仓不会被全平操作关闭

### 4.5 补仓流程（只在候选列表中补）

```
replenish_missing(positions, candidate_symbols, sides):
  current = {symbol:side for each exchange position}
  tasks = []
  for symbol in candidate_symbols:      ← 只在候选列表中遍历
    for side in sides:
      key = f"{symbol}:{side}"
      if key not in current AND NOT db.has_open(symbol, side):
        tasks.append(_do_open(symbol, open_side, side))
  if tasks:
    await asyncio.gather(*tasks, return_exceptions=True)

_do_open(symbol, open_side, side):
  result = client.safe_open(symbol, open_side)
  if result:
    open_fee = result["average"] * result["amount"] * contract_size * 0.0005
    db.record_open(symbol, side, result.order_id, result.average, result.amount, open_fee)
```

> **关键设计**：某币被平仓后，如果它不再满足 volume_threshold + price_threshold 条件 → **不会补仓**。这避免了资金被低效币种占用。

### 4.6 动态选币与闭环逻辑

```
┌─────────────────────────────────────────────────────────────┐
│                     动态选币周期（默认24h）                    │
│  get_candidate_symbols(volume>=N, price<=M)                  │
│       ↓                                                      │
│  candidate_symbols = [ENAUSDT, DOGEUSDT, WCTUSDT, ...]       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        每秒 tick()                           │
│                                                              │
│  下单/补仓 ──→ 只在 candidate_symbols 中操作                  │
│       │                                                      │
│       │   某币被平仓后不在 candidate_symbols                 │
│       │   → replenish_missing 不会再开回来                   │
│       │                                                      │
│  平仓 ───────→ 扫描 ALL positions（不限于候选列表）           │
│       │   排除 skip_symbols 白名单                           │
│       │   盈利达标即平                                       │
│       │                                                      │
│  加仓 ───────→ 扫描 ALL positions（不限于候选列表）           │
│       │   排除 skip_symbols 白名单                           │
│       │   亏损达标即加仓                                     │
│       │                                                      │
│       └──→ 加仓后盈利 → 平仓逻辑仍然生效                     │
│            平仓后不在候选列表 → 不再补仓（结束闭环）          │
└─────────────────────────────────────────────────────────────┘
```

## 五、精度计算设计

### 5.1 下单精度对齐

ccxt `market` 对象提供精度信息：

```python
market = self.market_info[resolved]
contract_size = market['contractSize']       # 每张合约代表的基础资产数量
amount_precision = market['precision']['amount']  # 数量小数位数
min_notional = market['limits']['cost']['min']    # 最小名义价值(USDT)
min_amount = market['limits']['amount']['min']    # 最小数量
```

### 5.2 开仓数量计算（完整流程）

```
ENAUSDT 合约示例:
  contractSize = 1 (1张合约 = 1 ENA)
  minNotional = 5 USDT
  minAmount = 1
  amountPrecision = 0 (整数张)
  currentPrice ≈ 0.128

  计算:
  1. 基于minAmount: ceil(1 / 1) = 1张
  2. 基于minNotional: ceil(5 / (0.128 × 1)) = ceil(39.06) = 40张
  3. 取较大值: max(1, 40) = 40张
  4. 按精度取整: round(40, 0) = 40.0
  → 最终下单 40 张合约
```

### 5.3 盈利率计算（纯毛利，不含手续费）

```python
# 持仓价值 = 入场价格 × 合约数 × 合约大小
position_value = entry_price * contracts * contract_size

# 毛盈利率 = 未实现盈亏 / 持仓价值
profit_rate = unrealized_pnl / position_value

# 平仓条件
if profit_rate >= profit_threshold:  # 默认 0.002 = 0.2%
    close_position()
```

**注意**：不涉及任何手续费计算。`unrealizedPnl` 来自交易所 API，交易所返回的值本身就是毛盈亏。手续费只在**记录交易历史**时统计，不影响平仓决策。

### 5.4 手续费计算（v1.2）

| 环节 | 费率 | 公式 |
|------|------|------|
| 开仓手续费 | 0.05% | `entry_price * contracts * contract_size * 0.0005` |
| 平仓手续费 | 0.05% | `exit_price * contracts * contract_size * 0.0005` |
| 总手续费 | 0.1% | `open_fee + close_fee` |

> 加仓时累加 `added_fee` 到 `open_positions.open_fee`，平仓时一并计入。

## 六、前端页面设计

### 6.1 配置区（v1.3 分组显示）

| 分组 | 控件 | 类型 | 说明 |
|------|------|------|------|
| **币种筛选** | 交易量筛选 | 数字输入 | 24h成交量阈值（USDT），如 50000000 |
| **币种筛选** | 币单价筛选 | 数字输入 | 最新价格阈值（USDT），**<= 此值**的币才入选 |
| **币种筛选** | 币种刷新间隔 | 数字输入 | 自动刷新候选列表间隔（秒），默认 86400 |
| **币种筛选** | 立即刷新币种 | 按钮 | POST /api/refresh-symbols，立即重新筛选 |
| **基础交易** | 交易方向 | 下拉选择 | 双向 / 多 / 空 |
| **基础交易** | 平仓利润率 | 数字输入 | 默认 0.2，单位 % |
| **补仓设置** | 停补阈值 | 数字输入 | **v1.4** 默认 10%，单位 % |
| **账户级全平** | 启用全平 | 开关 | 默认关闭，热加载即时生效 |
| **账户级全平** | 全平利润率 | 数字输入 | 默认 0.2，单位 % |
| **亏损加仓** | 启用加仓 | 开关 | 默认关闭 |
| **亏损加仓** | 多单亏损加仓阈值 | 数字输入 | **v1.4** 默认 25%，单位 % |
| **亏损加仓** | 空单亏损加仓阈值 | 数字输入 | **v1.4** 默认 25%，单位 % |
| **亏损加仓** | 加仓倍数 | 数字输入 | 默认 2，倍于最小合约数 |
| **多空对平** | 启用对平 | 开关 | 默认关闭 |
| **多空对平** | 对平利润率 | 数字输入 | 默认 0.2，单位 %（可独立于平仓利润率） |
| **白名单** | 跳过平仓白名单 | 文本输入 | 每行一个，这些币跳过平仓、对平、加仓 |

### 6.2 数据展示区（v1.3 重构）

| 区块 | 数据 | 来源 | 刷新频率 |
|------|------|------|----------|
| 账户概览 | 余额、未实现盈亏、**EvoClaw实时盈亏率**、**全平/对平/单平次数** | API /api/account | 3秒 |
| **持仓状态** | **当前持仓数、多单持仓数、空单持仓数、持仓总盈亏、持仓总盈亏率** | API /api/account | 3秒 |
| 统计面板 | 累计盈亏、累计手续费、胜率、最高盈率、**多单平仓总数/盈利率/盈利、空单平仓总数/盈利率/盈利** | API /api/stats | 5秒 |

### 6.3 样式设计

- 深色主题（适合交易场景）
- 绿色=盈利，红色=亏损
- 盈利率接近平仓线时闪烁提示
- 手机端自动堆叠为单列
- 当前筛选币种以标签形式自动换行显示

## 七、API调用优化策略

### 7.1 减少调用次数

| 操作 | 优化方式 |
|------|----------|
| 持仓查询 | 单次 `fetch_positions(symbols)` 批量获取，不逐个查 |
| 开仓 | 使用 `asyncio.gather` 并发下单 |
| Market信息 | 启动时 `load_markets()` 一次缓存，全程复用 |
| 行情价格 | 启动时 `refresh_prices()` 预取，tick 中批量刷新 |
| 币种筛选 | **缓存24小时**，不再每秒请求交易所（v1.2） |

### 7.2 错误处理与重试

```python
async def safe_open(self, symbol, side, retries=1):
    """开仓带重试（网络抖动容错）"""
    for i in range(retries + 1):
        result = await self.open_position(symbol, side)
        if result:
            return result
        if i < retries:
            await asyncio.sleep(0.5)
    return None
```

**v1.2 增强重试**：`open_position` 内部遇到 `-4164`（名义价值不足）自动增加合约数重试，最多 3 次。

### 7.3 内存控制

- asyncio 单进程单线程，无额外线程开销
- 不缓存历史数据（全部存 SQLite）
- 币种列表缓存于内存（`_candidate_symbols`），24小时刷新一次
- 日志使用 `RotatingFileHandler`，10MB × 3个备份
- 无重量级框架（不用 Flask/FastAPI/Django）

## 八、错误处理与稳定性

| 场景 | 处理策略 |
|------|----------|
| API限流(429) | ccxt 内置 `enableRateLimit=True`，自动等待 |
| 网络断开 | ccxt 自动重连，tick 异常捕获后继续 |
| 下单失败 | `safe_open` 重试，`-4164` 自动增加合约数（v1.2） |
| 配置错误 | `_get_config()` 捕获异常，使用旧配置继续 |
| 数据库锁 | `threading.Lock` 保护所有DB操作 |
| 进程崩溃 | systemd `Restart=always` 自动重启 |
| 重复开仓 | 三重防护：交易所持仓检查 + DB跟踪检查 + DB唯一约束 |
| 保证金不足 | 记录 warning，跳过该币，下一 tick 再试 |

## 九、部署方案

### 9.1 启动方式

```bash
# 直接启动
cd /home/claudeuser/EvoClaw
python3 main.py

# 一键重启（推荐开发调试使用）
# 自动完成：停止进程 → 清理端口 → 备份并清空日志 → 启动服务
cd /home/claudeuser/EvoClaw && ./restart.sh

# systemd 服务（推荐生产环境）
sudo systemctl enable evoclaw
sudo systemctl start evoclaw
```

### 9.2 systemd 配置

```ini
[Unit]
Description=EvoClaw Trading System
After=network.target

[Service]
Type=simple
User=claudeuser
WorkingDirectory=/home/claudeuser/EvoClaw
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## 十、踩坑记录（调试经验）

### 10.1 交易所类选择

| 尝试 | 结果 |
|------|------|
| `ccxt.binance` | 报错 `positionside was not sent`，不支持合约专用参数 |
| `ccxt.binanceusdm` | ✅ 正确，USDT-M合约专用类 |

### 10.2 符号解析

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `No market info for ENAUSDT` | ccxt 内部符号是 `ENA/USDT:USDT` | 建立 `symbol_map` 双向映射 |
| 持仓匹配失败 | `fetch_positions` 返回 ccxt 格式符号 | 用 `user_symbol()` 反向转换 |

### 10.3 下单参数

| 错误 | 原因 | 正确做法 |
|------|------|----------|
| `positionside was not sent` | 开仓时加了 `reduceOnly: False` | 开仓只传 `positionSide`，不加 `reduceOnly` |
| `Order's notional must be no smaller than 5` | 下单数量太少，未基于价格计算 | 启动时 `refresh_prices()` 预取价格 |
| `amount_precision` 类型错误 | 返回值是字符串 `"0"` | `int(float(...))` 转换 |
| `-4164` 名义价值不足（v1.2） | 价格下跌后原计算合约数不够 | 自动增加合约数 30% 重试 |

### 10.4 重复开仓问题

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| 持仓叠加到1000+ | 每次tick都尝试开仓，只检查交易所持仓 | 增加 DB `open_positions` 表双重检查 |
| 修复不生效 | `.pyc` 缓存 | `rm -rf __pycache__` |

### 10.5 Hedge Mode

- 用户账户已启用双向持仓模式（Hedge Mode）
- 如果有挂单时无法切换模式，但我们的系统不需要切换
- `binanceusdm` 原生支持 Hedge Mode 的 `positionSide` 参数

### 10.6 已有持仓不触发平仓

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| 盈利1.11%不触发平仓 | 启动时已有持仓未记录到 `open_positions` 表 | `main.py` 启动时遍历交易所持仓，调用 `db.record_open()` 记录已有持仓 |

### 10.7 平仓盈亏记录为 0

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| 平仓后累计盈亏、胜率、最高盈率全部显示 0 | ccxt `create_order()` 不返回 `closedPnL` | `_record_trade()` 手动计算：`pnl = (exit-entry) * contracts * contract_size`（多/空方向相反） |

### 10.8 平仓参数报错

| 报错 | 根因 | 解决方案 |
|------|------|----------|
| `-1106: Parameter 'reduceonly' sent when not required` | Binance 对 `reduceOnly` 有严格限制 | 平仓只用 `params={"positionSide": side.upper()}`，不加 reduceOnly |
| `-4136: Target strategy invalid for MARKET,closePosition true` | 账户绑定了 TP/SL 策略 | 不使用 `closePosition: True`，改为直接用 `positionSide` 平仓 |

### 10.9 启动时 CWD 错误

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| `can't open file '/home/claudeuser/main.py'` | Bash CWD 在 `/home/claudeuser` | `main.py` 中 `os.chdir(os.path.dirname(__file__))` 确保 CWD 正确 |

### 10.10 价格缓存不刷新导致下单失败（v1.2）

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| 持续报 `-4164` 名义价值不足 | `refresh_prices` 只在启动时执行，价格下跌后缓存价格过期 | tick() 每轮调用 `refresh_prices()`，保持价格实时 |

### 10.11 前端 API 错误崩溃（v1.2）

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| `Cannot read properties of undefined (reading 'toFixed')` | `api()` 函数不检查 `response.ok`，500 错误返回的 JSON 被直接解析 | `api()` 增加 `if (!r.ok) return null;`，前端各 load 函数会安全退出 |

### 10.12 补仓停补阈值被绕过（v1.4-fix）

| 现象 | 根因 | 解决方案 |
|------|------|----------|
| 调整成交量阈值并刷新币种后，停补阈值失效，继续开仓 | 原 `replenish_missing` 内联检查：若 `_prices` 和 `market.info.lastPrice` 均无法获取，则 `price` 为 falsy，`if price and price > 0:` 直接跳过停补检查，代码fallthrough到开仓 | 提取 `should_stop_replenish()` 统一方法，**价格无法获取时默认返回 True（STOP）**，并应用到 `replenish_missing`、`replenish_all`、`main.py 启动` 全部三条开仓路径 |

## 十一、风险与注意事项

1. **微小盈利策略风险**：微小盈利策略依赖高胜率，亏损单可能拉低整体收益
2. **双向持仓风险**：同币种多空同时持仓，极端行情下可能双向亏损
3. **API延迟**：网络延迟可能影响0.2%的快速平仓，需确保服务器离交易所近
4. **全平触发**：全平后立刻补仓，如果市场持续波动，可能频繁全平补仓
5. **跳过平仓功能**：仅跳过盈利平仓、对平、加仓，全平时不跳过（防止整体亏损扩大）
6. **手动持仓隔离**：手动开的仓不会被系统平仓，但系统不会为其补仓
7. **动态选币风险**：某币满足条件被开仓后，下一周期不再满足条件 → 平仓后不再补仓，资金释放
8. **保证金风险**：候选币种过多（如 >50个）可能导致保证金不足，需根据账户余额调整 volume_threshold

## 十二、实施状态

| 模块 | 状态 | 备注 |
|------|------|------|
| `exchange_client.py` | ✅ 完成 | 符号解析、精度计算、下单（含-4164重试）、持仓查询、自动选币 |
| `trader.py` | ✅ 完成 | 动态选币缓存、开仓追踪、单平、全平、补仓（v1.4 停补阈值）、交易记录、加仓（v1.4 多空分离） |
| `database.py` | ✅ 完成 | 双表设计（含fee/open_fee）、线程安全、WAL模式、自动迁移、多空统计 |
| `web_server.py` | ✅ 完成 | 7个API端点、静态文件服务、trader缓存读取、持仓多空分布 |
| `web/index.html` | ✅ 完成 | 深色主题、移动端适配、配置分组、持仓状态、多空平仓统计、**v1.4 补仓/加仓配置** |
| `main.py` | ✅ 完成 | 启动流程、信号处理、日志系统、动态选币初始化 |
| `config.json` | ✅ 完成 | 热加载支持（含 v1.4 新增字段） |
| `DESIGN.md` | ✅ 完成 | 本文档（包含完整实现细节，**v1.6** 更新） |

---

## 十三、v1.5 更新记录

### 13.1 持仓矩阵全屏首页

**改动**：
- 新增「持仓矩阵」独立标签页，作为**默认首页**
- 数据仪表盘中的小型紧凑矩阵已移除
- 矩阵使用 10×10 大网格，每个格子显示：币种名、方向、开仓价、合约数、盈亏值、盈亏率
- 突破 body 的 `max-width:960px` 限制，真正占满视口宽度
- 响应式适配：小屏幕自动隐藏次要字段（价格/数量 → 方向/盈亏率），字体逐级缩小

**CSS 关键设计**：
```css
#panel-matrix {
  position:relative; left:50%; right:50%;
  margin-left:-50vw; margin-right:-50vw;
  width:100vw; padding:12px; max-width:none;
}
.full-cell .fc-sym { font-size:10px; word-break:break-all; }
```

### 13.2 亏损统计重构与排序

**持仓状态卡片重新排序**（10格）：

| 位置 | 字段 | 说明 |
|------|------|------|
| 1-3 | 持仓数 / 多单 / 空单 | 基础分布 |
| 4-5 | 历史持仓最高亏损额/率 | 账户级（所有持仓合计的历史最差） |
| 6-7 | 历史单币最高亏损额/率 | 单币种历史最差 |
| 8-9 | 实时单币最高亏损额/率 | 当前持仓中的最差 |
| 10 | 历史持仓最低率(过程中) | 持仓过程中见过的最低 rate |

**不再单独建立「亏损统计」板块**，所有亏损相关数据合并到「持仓状态」中。

### 13.3 历史单币最高亏损数据来源修正

**问题**：`hist_worst_trade_rate` 原从 `trades` 表查询，返回的是**已平仓交易**的亏损率（如 -2.37%），但用户关注的是**持仓过程中**出现过的最低 rate（如 -52.12%，存在 `runtime_stats` 中）。

**修复**：
- `hist_worst_trade_rate` 改为从 `runtime_stats.max_position_loss_rate` 读取
- 新增 `runtime_stats.hist_worst_trade_pnl` 字段，记录对应的历史最差单币亏损金额
- 每次 tick 检测到更差的 `current_max_loss_rate` 时，**同步更新**对应的 `hist_worst_trade_pnl`
- 若 `hist_worst_trade_pnl` 缺失（旧数据），用当前最差持仓的 pnl 自动初始化

```python
# api_account 中的更新逻辑
if current_max_loss_rate < hist_max_loss_rate:
    self.db.set_runtime_stat("max_position_loss_rate", current_max_loss_rate)
    self.db.set_runtime_stat("hist_worst_trade_pnl", current_max_loss_pnl)
```

### 13.4 实时单币最高亏损率一致性修复

**问题**：dashboard 显示的「实时单币最高亏损率」与矩阵图中第一格（最差持仓）的盈亏率不一致。

**根因**：原代码中 `worst_position_rate` 取的是**pnl金额最小**的那个持仓的 rate，而不是**rate最小**的持仓。

**修复**：在 `api_account` 中分别跟踪：
- `worst_pnl` = pnl 最负的金额（实时单币最高亏损额）
- `worst_rate` = rate 最负的值（实时单币最高亏损率）

两者可能来自不同持仓，各自代表不同维度的「最差」。

```python
worst_pnl = 0.0
worst_rate = 0.0
for p in all_positions:
    ...
    if pnl < worst_pnl:
        worst_pnl = pnl
    if rate < worst_rate:
        worst_rate = rate
```

### 13.5 系统监控面板

**v1.5 新增**：数据仪表盘顶部增加「系统监控」区块，显示：
- CPU 使用率（读取 `/proc/stat`）
- 内存使用（读取 `/proc/meminfo`）
- 硬盘使用（`shutil.disk_usage`）
- 网络吞吐（读取 `/proc/net/dev`）

用于诊断服务器性能瓶颈（如海外服务器高延迟、内存压力等）。

### 13.6 动态持仓数量与矩阵大小

**v1.5 新增**：`max_position_count` 配置项（默认 100）：
- 控制最高持仓上限，所有开仓路径（replenish_missing / replenish_all / 启动开仓）均检查此限制
- 矩阵网格数量同步此配置（`api/positions-map` 返回 `max_slots`）
- 前端根据 `max_slots` 动态重建网格

## 十四、v1.6 更新记录

### 14.1 持仓矩阵去掉 USDT/USDC 后缀

**v1.6 新增**：矩阵单元格中的币种名称不再显示 USDT/USDC 后缀，提升可读性。

- `web/index.html`：`displaySymbol = symbol.replace(/USDT$/, '').replace(/USDC$/, '')`
- 不影响内部逻辑，仅前端展示层处理

### 14.2 数据仪表盘文字自适应

**v1.6 新增**：所有统计卡片中的数值和标签使用 `clamp()` 实现响应式字体大小，随窗口宽度自动缩放。

- 数值：`font-size: clamp(11px, 2.5vw, 15px)`
- 标签：`font-size: clamp(8px, 1.8vw, 10px)`
- 保证在小屏手机和大屏桌面都有合适的可读性

### 14.3 盈利趋势图表

**v1.6 新增**：账户概览上方增加「盈利趋势」独立区块，使用 Canvas 绘制柱状图。

**功能**：
- 按小时或按天聚合盈亏数据（切换按钮）
- 数据来源：`/api/profit-trend?period=hour|day`，查询 `trades` 表按时间聚合
- 绿色柱 = 盈利，红色柱 = 亏损
- 自动计算最大/最小值做 Y 轴归一化

**后端实现**（`web_server.py`）：
```python
async def api_profit_trend(self, request):
    period = request.query.get("period", "hour")  # hour | day
    now = datetime.now(timezone.utc)
    if period == "day":
        since = now - timedelta(days=30)
        fmt = "%Y-%m-%d"
    else:
        since = now - timedelta(days=7)
        fmt = "%Y-%m-%d %H:00"
    # 按 fmt 分组聚合 SUM(pnl)
```

**前端实现**：
- `loadProfitTrend()` 异步加载数据
- `drawChart()` Canvas 渲染，支持高 DPI 屏幕
- `setTrendPeriod()` 切换周期并重新加载

### 14.4 API 响应缓存（防 Binance IP 封禁）

**v1.6-fix1**：前端每 3 秒同时请求 4-5 个 API + 交易主循环每秒调用 REST API，触发 Binance `418 I'm a teapot` IP 封禁，导致全部 API 返回 500。

**修复方案**：

| 措施 | 实现 |
|------|------|
| 后端 2s 缓存 | `WebServer._api_cache` 对 `/api/account`、`/api/stats`、`/api/positions-map`、`/api/profit-trend` 做 2 秒 TTL 缓存 |
| 前端延长轮询 | `refresh` 3s → 5s；`loadSystem` 5s → 10s |

```python
async def _cached_response(self, key, handler, request):
    now = time.monotonic()
    cached = self._api_cache.get(key)
    if cached and now - cached[0] < self._api_cache_ttl:
        return cached[1]
    resp = await handler(request)
    self._api_cache[key] = (now, resp)
    return resp
```

> **踩坑记录**：Binance 对 IP 级别的 REST API 调用有严格限流，超过阈值会封禁 IP（返回 418），封禁时长从几秒到数分钟不等。使用 WebSocket 可避免此问题，但当前系统基于 ccxt REST，只能通过减少调用频率缓解。

### 14.5 排除 USDC 市场，只交易 USDT

**v1.6-fix2**：系统意外选择了 USDC 计价的合约（如 XRP/USDC:USDC），但只应交易 USDT-M 合约。

**修复**：`ExchangeClient.load_markets()` 增加 `quote == "USDT"` 过滤。

```python
if m and m.get("swap") and m.get("quote") == "USDT":
    self.market_info[symbol] = m
```

- 加载市场数从 727 减少到 687（排除 40 个 USDC 市场）
- `get_candidate_symbols` 自动过滤（USDC 不在 `market_info` 中）
- 现有 USDC 持仓不受影响（只是不再补仓/新开）
