# EvoClaw 设计文档

> EvoClaw — 高速微盈利加密货币自动交易系统

## 一、项目概述

### 1.1 定位与目标

EvoClaw 是一个基于 Binance USDT-M 永续合约的自动化交易系统，核心策略为**双向持仓 + 分层止盈 + 亏损加仓 + 账户级全平**。

| 设计目标 | 说明 |
|---------|------|
| 速度优先 | 每秒轮询持仓，盈利达标立即平仓，与时间赛跑 |
| 极简功能 | 只做一件事：开仓 → 盈利平仓，不做复杂策略 |
| 低内存 | 7×24 运行，进程常驻内存 < 100MB |
| API 节省 | 批量获取数据、多级缓存，避免频繁请求交易所 |
| 双保险盈利 | 单币种盈利平仓 + 账户总盈利全平，二者缺一不可 |
| 动态选币 | 按成交量和价格自动筛选交易对，无需手动维护币种列表 |

### 1.2 技术栈

| 层级 | 技术 | 选型理由 |
|------|------|----------|
| 后端运行时 | Python 3.12 + asyncio | 异步 I/O、极低内存、ccxt 原生支持 |
| 交易所 SDK | ccxt v4 (`binanceusdm`) | USDT-M 永续合约专用类，支持 Hedge Mode 双向持仓 |
| HTTP API | aiohttp | 异步 Web 框架，与 ccxt 同源 |
| 数据库 | SQLite (WAL 模式) | 零配置、零依赖、单文件、异步友好 |
| 前端 | 单 HTML + 原生 JS | 无构建步骤、手机适配、< 50KB |
| 配置 | JSON 文件 | 热加载、无需重启 |

### 1.3 项目结构

```
/home/claudeuser/EvoClaw/
├── main.py                  # 入口：启动所有模块、信号处理、主循环
├── restart.sh               # 一键重启脚本（停止→清理→启动）
├── config.json              # 交易配置（前端可修改，热加载）
├── web/config.json          # 前端显示配置（独立持久化）
├── DESIGN.md                # 本文档
├── requirements.txt         # 依赖清单
├── exchange_client.py       # 交易所客户端（符号解析、精度计算、下单、自动选币）
├── trader.py                # 交易引擎核心（动态选币、开仓、平仓、补仓、全平）
├── database.py              # SQLite 数据库（三表 + 手续费 + 统计）
├── web_server.py            # Web 服务（API + 前端 + 手动刷新币种）
├── data/
│   ├── evoclaw.db           # SQLite 数据库文件
│   └── trader.log           # 旋转日志文件
└── web/
    └── index.html           # 前端单页应用
```

## 二、系统架构

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    asyncio Event Loop                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ Exchange     │  │   Trader     │  │   Web Server     │   │
│  │ Client       │  │   (Engine)   │  │   (aiohttp)      │   │
│  │              │  │              │  │                  │   │
│  │ • 符号解析    │  │ • 动态选币   │  │ • 配置管理       │   │
│  │ • 精度计算    │  │ • 开仓+追踪  │  │ • 统计查询       │   │
│  │ • 下单       │  │ • 平仓+记录  │  │ • 静态 HTML      │   │
│  │ • 持仓查询    │  │ • 补仓       │  │ • 手动刷新       │   │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘   │
│         │                 │                    │             │
│  ┌──────▼─────────────────▼────────────────────▼──────────┐  │
│  │              Database (SQLite + WAL)                   │  │
│  │  • trades 表        — 历史成交，永久保存                │  │
│  │  • open_positions 表 — 系统当前管理持仓，平仓即删        │  │
│  │  • runtime_stats 表 — 运行时统计（如补仓次数、最大亏损）│  │
│  └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  web/index.html │
                    │  • 数据仪表盘    │
                    │  • 持仓矩阵      │
                    │  • 交易配置      │
                    └─────────────────┘
```

### 2.2 模块依赖关系

```
main.py
  ├── ExchangeClient (ccxt.binanceusdm)
  ├── Database (SQLite)
  ├── Trader (依赖 ExchangeClient、Database、config.json)
  └── WebServer (依赖 ExchangeClient、Database、config.json、Trader)
```

### 2.3 启动顺序与生命周期

```
1. main.py 读取 config.json
2. setup_logging() → 控制台 + trader.log（3MB × 5 备份）+ errors.log（2MB × 3 备份，仅 WARNING+）
3. Database("data/evoclaw.db") → 建表 + 零停机迁移（ALTER TABLE）
4. ExchangeClient(config) → load_markets() 缓存市场信息 + 符号映射
5. ExchangeClient.get_candidate_symbols() → 动态选币
6. ExchangeClient.refresh_prices(symbols) → 预取价格
7. Trader(client, db, "config.json") → 初始化交易引擎
8. WebServer(client, db, "config.json", trader=trader) → 初始化 Web 服务
9. 启动时仓位对齐：遍历交易所持仓 → db.record_open() 追踪已有仓位
10. WebServer 启动（0.0.0.0:8080）
11. 注册信号处理（SIGINT/SIGTERM → trader.stop()）
12. Trader.run() 主循环启动（阻塞，直到信号关闭）
```

## 三、核心模块设计

### 3.1 交易引擎 (trader.py)

#### 3.1.1 主循环设计

`Trader.run()` 以固定间隔（默认 1 秒）持续执行 `tick()`：

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

**配置热重载**：每次 `tick()` 开头调用 `_get_config()`，基于 `os.path.getmtime()` 检测配置文件变化，变化时才重新加载，无需重启进程。

#### 3.1.2 Tick 执行阶段

单次 `tick()` 按固定顺序执行以下阶段，每个阶段后重新获取持仓以确保状态一致：

| 顺序 | 阶段 | 扫描范围 | 说明 |
|------|------|----------|------|
| 1 | 动态选币 | — | 检查是否需要刷新候选列表（缓存 24h） |
| 2 | 刷新价格 | candidate_symbols | 批量刷新价格缓存 |
| 3 | 全仓止盈 | 系统追踪的持仓 | 账户整体盈利达标 → 全平 + 立即补仓 |
| 4 | 分层止盈 | **所有持仓** | 单币种盈利达标 → 按比例平仓 |
| 5 | 多空对平 | **所有持仓** | 同币种多空双边平均盈利达标 → 双向平仓 |
| 6 | 亏损加仓 | **所有持仓** | 亏损达标 → 追加仓位（可重复触发） |
| 7 | 仓位补充 | candidate_symbols | 只在候选列表中补缺 |

> **设计决策**：平仓/加仓扫描**所有持仓**（不限于候选列表），只有下单/补仓使用 `candidate_symbols`。这样实现完整闭环：某币被平仓后不在候选列表 → 不再补仓；但该币持仓亏损 → 仍然会加仓；加仓后盈利 → 仍然会平仓。

#### 3.1.3 五大交易策略

**1) 账户级全平 (`check_all_close`)**

- 只计算系统追踪的持仓（`open_positions` 表）
- 总盈利率 ≥ `all_close_threshold` 时，逐个平仓 + `replenish_all()` 立即补仓
- 白名单 `skip_symbols` 中的币种不参与全平

**2) 分层止盈 (`check_single_close`)**

- 扫描所有持仓，排除白名单
- 5 档分级止盈（`profit_tiers`）：利润越高平仓比例越大
- 每档只执行一次（`_tier_executed` 缓存已执行的最高档位）
- 向后兼容：未配置 `profit_tiers` 时回退到单层 `profit_threshold`

| 档位 | 阈值 | 平仓比例 | 示例 |
|------|------|----------|------|
| 1 | 5% | 30% | 盈利 5% 平掉 30% 仓位 |
| 2 | 12.9% | 30% | 盈利 12.9% 再平 30% |
| 3 | 25.5% | 100% | 盈利 25.5% 全平 |
| 4 | 37.8% | 50% | 盈利 37.8% 平 50% |
| 5 | 50% | 50% | 盈利 50% 平 50% |

**3) 多空对平 (`check_single_pair_close`)**

- 扫描所有持仓，排除白名单
- 同一币种同时存在 long + short 时，计算双边平均盈利率
- 平均盈利率 ≥ `pair_close_threshold` → 双向平仓（含亏损边）

**4) 亏损加仓 (`check_margin_call`)**

- 扫描所有持仓，排除白名单
- 多单亏损 ≥ `margin_call_threshold_long` 或空单亏损 ≥ `margin_call_threshold_short`
- 加仓数量 = 当前持仓合约数 × `margin_call_multiplier`
- **最小下单量保护**：若计算值低于交易所最小名义价值（如 5 USDT），自动提升为 calc_min_contracts() 计算出的最小合约数，确保加仓单不会被交易所拒绝
- **可重复触发**：每轮 tick 满足条件就继续加，无次数限制
- 加仓时累加 `added_fee` 到 `open_positions.open_fee`

**5) 仓位补充 (`replenish_missing`)**

- 只在 `candidate_symbols` 中遍历
- 三重防重复开仓：
  1. 交易所已有持仓 → 跳过
  2. DB 已有跟踪 → 跳过
  3. `record_open` 中 `DELETE ... INSERT` 唯一约束
- 停补检查：对方仓位偏离 ≥ `replenish_stop_threshold` 时停止补仓
- 并发开仓：`asyncio.gather(*tasks, return_exceptions=True)`

#### 3.1.4 断路器模式

针对 Binance 错误码 `-2027`（超出最大持仓限制）：

- 连续失败 5 次后，该交易对跳过 10 分钟
- 成功后自动清除计数
- 避免对同一问题交易对无限重试

#### 3.1.5 单 Tick 数据一致性

- 每个阶段后重新获取持仓，确保状态一致
- 使用同一 `position_map` 对象在 tick 内传递
- 每次 tick 结束调用 `db.checkpoint()`，防止 SQLite WAL 文件无限增长

### 3.2 交易所交互层 (exchange_client.py)

#### 3.2.1 符号映射机制

用户配置使用 `ENAUSDT` 格式，ccxt 内部使用 `ENA/USDT:USDT` 格式。系统在启动时建立双向映射：

```python
# 正向映射：用户符号 → ccxt 符号
self.symbol_map["ENAUSDT"] = "ENA/USDT:USDT"
# 反向映射：ccxt 符号 → 用户符号
self._reverse_map["ENA/USDT:USDT"] = "ENAUSDT"
```

> **踩坑记录**：`fetch_positions()` 返回的 `symbol` 字段是 ccxt 格式，必须用 `user_symbol()` 转换后才能与筛选出的币种比较。

#### 3.2.2 自动选币

条件：`volume >= volume_threshold` AND `price <= price_threshold`

- `price_threshold` 是 **<=**（小于等于），目的是筛选低价币，降低每仓保证金占用
- `volume_threshold` 是 **>=**（大于等于），目的是筛选高流动性币
- 调用一次 `fetch_tickers()` 获取所有币种的成交量和价格
- 缓存 24 小时，tick 直接使用缓存

#### 3.2.3 下单参数规则（极其严格）

| 场景 | 正确做法 | 错误做法 | 报错 |
|------|----------|----------|------|
| 开仓 | `params={"positionSide": "LONG"/"SHORT"}` | 加 `reduceOnly: False` | 多余参数报错 |
| 平仓 | `params={"positionSide": "LONG"/"SHORT"}` | 加 `reduceOnly` | `-1106` |
| 平仓 | 市价单 + `positionSide` | `closePosition: True` | `-4136` |

> **原因**：账户绑定了 TP/SL 策略，`closePosition: True` 与 MARKET 单不兼容；Binance 对 `reduceOnly` 有严格限制，平仓时不需要该参数。

#### 3.2.4 错误码处理

| 错误码 | 含义 | 处理策略 |
|--------|------|----------|
| `-4164` | 名义价值不足 | 自动增加合约数 30% 重试，最多 3 次 |
| `-2027` | 超出最大持仓限制 | 记录警告，断路器计数 |

#### 3.2.5 最小合约数计算

综合 `contractSize`、`min_notional`、`min_amount`、`amount_precision` 计算：

```
1. 基于 min_amount: ceil(min_amount / contract_size)
2. 基于 min_notional: ceil(min_notional / (price × contract_size))
3. 取较大值，按精度取整
```

> **踩坑记录**：`amount_precision` 在某些交易所返回字符串类型（如 `"0"`），必须 `int(float(...))` 转换。

#### 3.2.6 停补保护 (`should_stop_replenish`)

```python
def should_stop_replenish(sym, side, stop_threshold, position_map):
    """判断是否应该停止补仓。安全默认：价格无法获取时返回 True（停止）。"""
```

- 统一封装到 `ExchangeClient`，所有开仓路径共用同一套判断逻辑
- **价格无法获取时默认 STOP**（旧逻辑会跳过检查直接开仓，构成安全漏洞）
- `replenish_missing`、`replenish_all`、`main.py 启动开仓` 三处全部调用

### 3.3 Web 服务 (web_server.py)

#### 3.3.1 路由设计

| 端点 | 方法 | 用途 |
|------|------|------|
| `/` | GET | 返回 `web/index.html` |
| `/web/config.json` | GET | 返回前端独立配置文件 |
| `/api/web-config` | GET/POST | 前端显示配置读写 |
| `/api/config` | GET/POST | 交易配置读写（GET 时脱敏 API 密钥） |
| `/api/account` | GET | 账户余额 + 持仓分布 + 亏损统计 |
| `/api/positions` | GET | 当前持仓列表（含盈亏率） |
| `/api/positions-map` | GET | 持仓矩阵数据（按 `pnl_rate` 排序，返回全部持仓） |
| `/api/stats` | GET | 累计盈亏、胜率、多空统计 |
| `/api/system` | GET | CPU / 内存 / 磁盘 / 网络实时数据 |
| `/api/profit-trend` | GET | 按小时/天聚合盈亏趋势 |
| `/api/trades` | GET | 分页查询历史交易记录 |
| `/api/refresh-symbols` | POST | 手动刷新候选币种 |

#### 3.3.2 API 响应缓存

为防止 Binance IP 封禁（`418 I'm a teapot`），对高频接口做 15 秒 TTL 缓存：

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

- 使用 `asyncio.Lock` 防止缓存击穿
- 缓存键按时间粒度区分（如 `profit_trend_hour`、`profit_trend_day`）

#### 3.3.3 线程池隔离

所有数据库查询通过 `asyncio.to_thread()` 在线程池执行，避免阻塞 aiohttp 事件循环：

```python
async def _db_sync(self, fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
```

#### 3.3.4 持仓矩阵数据接口

`/api/positions-map` 返回**全部持仓数据**（已按 `pnl_rate` 升序排序），不再用 `max_position_count` 截取。前端 `matrix_slots` 作为纯显示上限，负责截断显示。

### 3.4 数据库 (database.py)

#### 3.4.1 三表设计

| 表名 | 用途 | 生命周期 |
|------|------|----------|
| `trades` | 历史记录所有已平仓交易（含手续费） | 只增不减，永久保存 |
| `open_positions` | 追踪系统当前管理的未平仓持仓 | 开仓时插入，平仓时删除 |
| `runtime_stats` | 运行时统计（补仓次数、最大亏损率等） | 持续更新 |

#### 3.4.2 Schema

**trades 表**

```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,        -- 'single' / 'all_close' / 'pair_close'
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    amount REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_rate REAL NOT NULL,
    fee REAL DEFAULT 0,        -- 总手续费（开仓 + 平仓）
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**open_positions 表**

```sql
CREATE TABLE open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_id TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    amount REAL NOT NULL,
    margin_called INTEGER DEFAULT 0,
    open_fee REAL DEFAULT 0,   -- 累计开仓手续费（含加仓）
    slot_index INTEGER DEFAULT -1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, side)
);
```

**runtime_stats 表**

```sql
CREATE TABLE runtime_stats (
    key TEXT PRIMARY KEY,
    value REAL DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### 3.4.3 Slot 分配算法

为前端矩阵可视化分配 0-99 的索引：

- long 优先偶数 slot，short 优先奇数 slot
- 同一币种的 long/short 尽量相邻
- slot 随 `DELETE` 释放，可复用

#### 3.4.4 零停机迁移

启动时通过 `try/except` 包裹 `ALTER TABLE ADD COLUMN`，兼容旧数据库：

```python
try:
    conn.execute("ALTER TABLE trades ADD COLUMN fee REAL DEFAULT 0")
except sqlite3.OperationalError:
    pass  # 列已存在
```

#### 3.4.5 核心统计查询

`get_stats()` 汇总：总盈亏、总手续费、最大单笔盈利率、胜率、多空计数、多空盈亏、平仓类型分布等。

### 3.5 前端 (web/index.html)

#### 3.5.1 整体架构

- 单 HTML 文件，无构建步骤
- **Cyber-FinTech Terminal** 视觉风格：深色太空蓝基底 + 霓虹青 accent
- **字体栈**：Geist（Variable，100-900）+ JetBrains Mono（等宽数据）
- **图标系统**：Phosphor Icons (`ph-*`)，全矢量、单色、支持 weight 变换
- **三标签页 SPA**：数据仪表盘 / 持仓矩阵 / 交易配置
- **视觉特效**：Liquid Glass 面板、neon glow 边框、动态脉冲边线、滚动 triggered stagger reveal
- **响应式**：移动端优先的断点缩放，Bento Grid 自动重排

#### 3.5.2 设计系统 (Design Tokens)

所有颜色与特效通过 `:root` CSS 变量集中管理，并保留 legacy 别名保证 JS 向后兼容：

| Token | 值 | 用途 |
|-------|-----|------|
| `--bg-base` | `#070a12` | 页面底层背景 |
| `--bg-elevated` | `#0d1220` | 卡片/面板背景 |
| `--accent` | `#00d4ff` | 主强调色（霓虹青） |
| `--success` | `#00e5a0` | 盈利/做多指示 |
| `--error` | `#ff4757` | 亏损/做空指示 |
| `--color-primary` | `var(--accent)` | **Legacy 别名** |
| `--color-long` | `var(--success)` | **Legacy 别名** |
| `--color-short` | `var(--error)` | **Legacy 别名** |

#### 3.5.3 标签页结构与 Bento Grid 布局

**数据仪表盘** (`ph-squares-four`)：
- 左侧主区（跨 2 行）：**交易决策流图**（SVG，17 节点中心主干 + 左向分支布局，粒子动画）
- 右侧 stacking：**系统监控** + **盈利趋势** + **账户概览**
- 底部全宽行（`bento-bottom-row`，`1fr 1fr` 等分）：**持仓状态** + **交易统计**

**持仓矩阵** (`ph-grid-four`)：全屏响应式网格，按盈亏率热力着色。

**交易配置** (`ph-gear`)：9 大分组配置面板。

#### 3.5.4 数据刷新机制

| 数据源 | 刷新频率 | 说明 |
|--------|----------|------|
| 账户/统计 | 10 秒 | `setInterval(refresh, 10000)`，带 `AbortController` 取消过期请求 |
| 系统监控 | 10 秒 | `setInterval(loadSystem, 10000)` |
| 策略流图 | 10 秒 | 随 dashboard 主刷新循环调用 `updateFlowGraph()` |
| 页面可见性 | — | 隐藏时暂停所有定时器，恢复时立即全量刷新 |
| 防重复刷新 | — | `window._isRefreshing` 标志位 + `lastPeriod` 防陈旧数据 |

**初始化职责**：
- `loadDashboardView()`：首次构建 SVG 流图、加载配置、初始化 `_prevStats` / `_prevAccount`。

#### 3.5.5 持仓矩阵

- 突破 `max-width: 960px` 限制，占满视口宽度
- 网格大小由 `window.frontendConfig.matrix_slots`（默认 100）和 `matrix_columns`（默认 10）控制
- 使用 `DocumentFragment` 一次性构建 DOM，子元素预创建并缓存
- 币种名去除 `USDT`/`USDC` 后缀
- 响应式适配：小屏幕逐级隐藏次要字段，字体自动缩小

**数据流**：
1. 后端 `/api/positions-map` 返回全部持仓（按 `pnl_rate` 升序排序）
2. 前端 `matrix_slots` 作为硬性显示上限：
   - 持仓 < 格子数：显示全部，剩余格子为空
   - 持仓 > 格子数：显示前 N 个（亏损最多的优先），多余的隐藏

> **设计决策**：`matrix_slots`（前端显示配置）与 `max_position_count`（交易配置）完全解耦。前者只控制显示，后者只控制交易系统的开仓上限。

#### 3.5.6 盈利趋势图

- Canvas 2D 绘制，支持高 DPI（`devicePixelRatio` 适配）
- 按小时/天聚合盈亏数据，绿色/红色面积填充 + 二次贝塞尔曲线平滑
- **增强特效**：线条 neon glow（`shadowBlur` + accent 色）、数据点脉冲动画
- 响应式缩放：基于设计宽度 600px 计算 `scale` 因子
- 标签防重叠：根据图表宽度计算最大标签数，均匀采样
- **所在位置**：Dashboard 右侧账户概览上方

#### 3.5.7 策略决策流图 (Dashboard 主区)

**核心实现**：
- 纯 SVG 渲染，无外部依赖
- **17 个节点**覆盖完整交易生命周期：Tick 开始 → 刷新交易对/价格/持仓 → 五项策略检查 → 对应动作执行 → 数据库持久化 → Tick 结束
- **Layout C — 中心主干 + 左向分支**：
  - 主干（居中）：数据节点（圆角矩形）+ 决策节点（菱形）垂直排列
  - 分支（左侧）：动作节点（胶囊矩形）与对应决策节点水平对齐，通过横向边线连接
  - 返回边线：从动作节点右侧以 S 型贝塞尔曲线回到下一决策节点左侧
- **动态边线**：
  - 实线 = 策略启用且流程活跃
  - 虚线 = 策略禁用或路径未触发
  - `stroke-dashoffset` 脉冲动画，速度反映系统繁忙度
  - 粒子动画：沿边线持续移动的小圆点，颜色区分路径类型
- **节点状态**：
  - 激活态：霓虹青 glow + 实心填充
  - 触发态：绿色 glow（动作节点执行时）
  - 禁用态：50% 透明度 + 灰色边框
  - 错误态：红色脉冲
- **边线标签**：`触发`（yes 分支）、`继续`（no 分支），带背景遮罩防重叠

#### 3.5.8 配置面板

9 个分组：币种筛选、基础交易、分档止盈、补仓设置、账户级全平、亏损加仓、多空对平、白名单、前端显示设置。

**配置加载**：
- 交易配置：`loadConfig()` 从 `/api/config` 获取
- 前端配置：`loadFrontendConfig()` 从 `/web/config.json` 获取（带 `cache: no-store` 防浏览器缓存）

**配置保存**：
- 同时提交两份配置：交易配置 → `POST /api/config`，前端配置 → `POST /api/web-config`
- 保存成功后立即更新 `window.frontendConfig`，确保即时生效
## 四、配置体系

### 4.1 交易配置 (config.json)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `exchange` | str | `"binance"` | 交易所标识（实际使用 binanceusdm） |
| `exchange_kwargs` | dict | — | API 密钥等参数 |
| `side` | str | `"both"` | `"long"` / `"short"` / `"both"` |
| `profit_threshold` | float | `0.002` | 单层止盈毛盈利率阈值（向后兼容） |
| `profit_tiers` | list | — | 5 档分层止盈配置 |
| `replenish_stop_threshold` | float | `0` | 补仓停补阈值：对方仓位偏离 ≥ 此值时停止补仓 |
| `max_position_count` | int | `100` | 最大持仓数量上限，控制交易系统开仓上限 |
| `enable_all_close` | bool | `false` | 账户级全平开关 |
| `all_close_threshold` | float | `0.002` | 全平触发毛盈利率阈值 |
| `skip_symbols` | list | `[]` | 跳过平仓/加仓/对平的白名单 |
| `enable_margin_call` | bool | `false` | 亏损加仓开关 |
| `margin_call_threshold_long` | float | `0.01` | 多单亏损加仓阈值 |
| `margin_call_threshold_short` | float | `0.01` | 空单亏损加仓阈值 |
| `margin_call_multiplier` | float | `2` | 加仓倍数（基于当前持仓量） |
| `enable_single_pair_close` | bool | `false` | 多空对平开关 |
| `pair_close_threshold` | float | `0.002` | 多空对平触发盈利率 |
| `position_check_interval` | int | `1` | tick 间隔（秒） |
| `volume_threshold` | float | `0` | 24h 成交量筛选阈值（动态选币） |
| `price_threshold` | float | `0` | 币单价筛选阈值，**<= 此值**的币才入选 |
| `symbol_refresh_interval` | int | `86400` | 候选币种自动刷新间隔（秒） |

**分层止盈 5 档结构示例**：

```json
[
    {"threshold": 0.05,   "close_pct": 0.3},
    {"threshold": 0.129,  "close_pct": 0.3},
    {"threshold": 0.255,  "close_pct": 1.0},
    {"threshold": 0.378,  "close_pct": 0.5},
    {"threshold": 0.5,    "close_pct": 0.5}
]
```

### 4.2 前端配置 (web/config.json)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `matrix_slots` | int | `100` | 持仓矩阵显示格子数上限（纯显示参数，与交易逻辑解耦） |
| `matrix_columns` | int | `10` | 持仓矩阵列数 |

## 五、核心流程详解

### 5.1 启动流程

```
1. main.py 读取 config.json
2. setup_logging() → 控制台 + 文件日志（5MB × 5 备份）
3. Database("data/evoclaw.db") → 建表 + 自动迁移
4. ExchangeClient(config) → load_markets() → 仅保留 USDT 计价 swap
5. get_candidate_symbols(volume_threshold, price_threshold) → 动态选币
6. refresh_prices(symbols) → 预取价格
7. Trader(client, db, "config.json")
8. WebServer(client, db, "config.json", trader=trader)
9. 启动时仓位对齐：
   a. 获取交易所当前持仓 → 构建 current 集合
   b. 获取 DB 跟踪持仓 → 合并到 current
   c. 对每个 candidate_symbol × side，若不在 current 中：
      - should_stop_replenish() 检查停补阈值
      - 通过 → safe_open() + record_open()
10. WebServer 启动（0.0.0.0:8080）
11. 注册信号处理（SIGINT/SIGTERM → trader.stop()）
12. Trader.run() 主循环启动
```

### 5.2 单次 Tick 执行时序

```
T0:    _get_config() → 热加载配置
T0+0:  _ensure_symbols() → 检查候选列表缓存（24h）
T0+0:  refresh_prices(candidate_symbols) → 批量刷新价格
T0+0:  get_positions(candidate_symbols) → 获取候选币种持仓

T0+0:  check_all_close() [if enable_all_close]
       ├─ DB 获取系统持仓 → 获取交易所数据
       ├─ 计算总盈利率 → 达标则逐个平仓 + remove_open + record_trade
       └─ replenish_all() 并发补仓

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  check_single_close() [扫描 ALL positions]
       ├─ skip_symbols → 跳过
       ├─ profit_rate >= threshold → 平仓 + record_trade
       └─ 继续下一个

T0+0:  get_positions() → 重新获取所有持仓

T0+0:  check_single_pair_close() [扫描 ALL positions]
       ├─ 排除白名单
       ├─ 同币种多空双边 avg_rate >= threshold → 双向平仓
       └─ 继续下一个

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  check_margin_call() [扫描 ALL positions]
       ├─ 排除白名单
       ├─ loss_rate >= threshold → add_position(当前持仓 × multiplier)
       └─ 继续下一个（下一轮 tick 若仍满足，会继续加仓）

T0+0:  get_positions(candidate_symbols) → 重新获取

T0+0:  replenish_missing()
       ├─ 获取所有持仓 → position_map（停补检查用）
       ├─ 构建交易所持仓集合
       ├─ 遍历 candidate_symbol × side：
       │   ├─ 交易所已有 / DB 有 → 跳过
       │   ├─ should_stop_replenish() → 价格未知默认 STOP
       │   └─ 通过 → _do_open() → 并发执行
       └─ asyncio.gather 并发

T0+0:  db.checkpoint() → 防止 WAL 无限增长
```

**性能特征（v2.0 优化后）**：
- 单次 tick **1 次** `get_positions`（仅在发生交易后 re-fetch，最多 3 次）
- 币种筛选使用缓存，不再每秒请求交易所
- 价格刷新使用批量 `fetch_tickers`
- 开仓/平仓使用 `asyncio.gather` 并发
- 静态 HTML 内存缓存，避免重复磁盘读取
- 数据库使用顶级 import，消除 `__import__` 热路径开销
- API 缓存只存纯 dict 数据，防止 Response 对象内存泄露

### 5.3 精度计算

**开仓数量计算（完整流程）**：

```
ENAUSDT 合约示例:
  contractSize = 1 (1张合约 = 1 ENA)
  minNotional = 5 USDT
  minAmount = 1
  amountPrecision = 0 (整数张)
  currentPrice ≈ 0.128

  1. 基于 minAmount: ceil(1 / 1) = 1张
  2. 基于 minNotional: ceil(5 / (0.128 × 1)) = ceil(39.06) = 40张
  3. 取较大值: max(1, 40) = 40张
  4. 按精度取整: round(40, 0) = 40.0
  → 最终下单 40 张合约
```

**盈利率计算（纯毛利，不含手续费）**：

```python
position_value = entry_price * contracts * contract_size
profit_rate = unrealized_pnl / position_value
```

> 手续费只在**记录交易历史**时统计，不影响平仓决策。

**手续费计算**：

| 环节 | 费率 | 公式 |
|------|------|------|
| 开仓 | 0.05% | `entry_price * contracts * contract_size * 0.0005` |
| 平仓 | 0.05% | `exit_price * contracts * contract_size * 0.0005` |
| 总手续费 | 0.1% | `open_fee + close_fee` |

### 5.4 动态选币与闭环逻辑

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

## 六、关键设计决策

### 6.1 交易所选型

- 使用 `ccxt.binanceusdm` 而非 `ccxt.binance` — 只有前者能正确处理 USDT-M 合约的 Hedge Mode 双向持仓
- 必须启用 Hedge Mode，否则 `positionSide` 参数无效

### 6.2 数据库选型

- SQLite WAL 模式：零配置、单文件、异步友好
- `PRAGMA synchronous=NORMAL` 平衡性能与安全性
- 双表分离：`trades` 永久保存 vs `open_positions` 动态跟踪
- 线程锁保护所有 DB 操作（`check_same_thread=False`）

### 6.3 前端架构

- 单 HTML + 原生 JS：< 50KB、无构建、零依赖
- 前端配置（`web/config.json`）与交易配置（`config.json`）完全分离
- `matrix_slots` 与 `max_position_count` 解耦：前者纯显示，后者控制交易

### 6.4 缓存策略

| 缓存对象 | TTL | 说明 |
|----------|-----|------|
| API 响应 | 15 秒 | 防 Binance IP 封禁（418） |
| 余额 | 5 分钟 | 减少账户查询 |
| 系统指标 | 5 秒 | CPU/内存/网络 |
| 候选币种 | 24 小时 | `symbol_refresh_interval` |
| 价格缓存 | 每 tick 刷新 | `refresh_prices()` |

### 6.5 错误处理策略

| 场景 | 策略 |
|------|------|
| `-2027` 超出最大持仓 | 断路器：连续 5 次失败后跳过 10 分钟 |
| `-4164` 名义价值不足 | 自动增加合约数 30% 重试，最多 3 次 |
| API 限流 (429) | ccxt 内置 `enableRateLimit=True` |
| 网络断开 | tick 异常捕获后继续 |
| 配置错误 | `_get_config()` 捕获异常，使用旧配置继续 |
| 前端 API 错误 | `api()` 非 2xx 返回 null，各 load 函数安全退出 |

## 七、踩坑记录与教训

### 7.1 交易所集成

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `positionside was not sent` | 开仓时加了 `reduceOnly: False` | 开仓只传 `positionSide`，不加 `reduceOnly` |
| `-1106: Parameter 'reduceonly' sent when not required` | 平仓时加了 `reduceOnly` | 平仓只用 `positionSide`，不加 `reduceOnly` |
| `-4136: Target strategy invalid for MARKET, closePosition true` | 账户绑定了 TP/SL 策略 | 不使用 `closePosition: True`，改用 `positionSide` |
| `positionSide` 值大小写 | Binance 要求大写 | 必须为 `"LONG"` / `"SHORT"` |

### 7.2 符号与精度

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `No market info for ENAUSDT` | ccxt 内部符号是 `ENA/USDT:USDT` | 建立 `symbol_map` 双向映射 |
| 持仓匹配失败 | `fetch_positions` 返回 ccxt 格式符号 | 用 `user_symbol()` 反向转换 |
| `amount_precision` 类型错误 | 返回值是字符串 `"0"` | `int(float(...))` 转换 |
| `-4164` 名义价值不足 | 价格下跌后原计算合约数不够 | 自动增加 30% 重试 |

### 7.3 重复开仓与追踪

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 持仓叠加到 1000+ | 只检查交易所持仓 | 增加 DB `open_positions` 双重检查 |
| 已有持仓不触发平仓 | 启动时未记录到 `open_positions` | 启动时遍历交易所持仓并 `record_open()` |
| 平仓盈亏记录为 0 | ccxt `create_order()` 不返回 `closedPnL` | `_record_trade()` 手动计算 PnL |
| 加仓被交易所拒绝 | 加仓数量 = contracts x multiplier，未检查最小名义价值 | 加仓前与 `calc_min_contracts()` 取较大值，不足时自动提升到最小下单量 |

### 7.4 补仓停补漏洞

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 停补阈值被绕过 | 价格无法获取时 `if price and price > 0` 直接跳过检查 | 提取 `should_stop_replenish()` 统一方法，**价格未知默认返回 True（STOP）** |
| 启动时不停补 | 启动开仓路径未检查停补 | `main.py` 启动时也调用 `should_stop_replenish()` |

### 7.5 性能与稳定性

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| Binance IP 封禁 418 | 前端 3 秒轮询 + 后端每秒 REST | 后端 15 秒 API 缓存 + 前端延长到 5-10 秒 |
| SQLite WAL 无限增长 | 未做 checkpoint | 每次 tick 结束调用 `db.checkpoint()` |
| 价格缓存不刷新 | `refresh_prices` 只在启动时执行 | tick() 每轮调用 `refresh_prices()` |
| 前端 API 错误崩溃 | `api()` 不检查 `response.ok` | `if (!r.ok) return null`，各 load 函数安全退出 |

### 7.6 动态选币

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 选到 USDC 市场 | `load_markets()` 未过滤 quote | 增加 `quote == "USDT"` 过滤 |
| CWD 错误 | 启动时工作目录不对 | `os.chdir(os.path.dirname(__file__))` |

## 八、部署与运维

### 8.1 环境要求

- Python 3.12+
- Binance 账户已开通 USDT-M 合约交易
- 账户已启用 **双向持仓模式（Hedge Mode）**
- 建议部署在靠近交易所的区域以降低延迟

### 8.2 启动方式

```bash
# 直接启动
cd /home/claudeuser/EvoClaw && python3 main.py

# 一键重启（推荐）
cd /home/claudeuser/EvoClaw && ./restart.sh

# systemd 服务（生产环境）
sudo systemctl enable evoclaw
sudo systemctl start evoclaw
```

### 8.3 restart.sh 流程

```bash
# 正常重启
./restart.sh

# 看门狗模式（30s 检测 + 自动重启 + crash 日志）
./restart.sh --watch

# 状态查询
./restart.sh --status
```

核心流程：
1. 停止现有进程（优雅终止 → 强制 kill）
2. 清理端口 8080
3. 启动 EvoClaw（python3 main.py）
4. 验证服务状态

看门狗模式：每 30 秒检测进程存活，若挂掉自动重启并将 crash 前最后 10 行日志写入 `data/crash.log`。

### 8.4 日志管理

- **trader.log**：`RotatingFileHandler`，3MB 轮转，保留 5 个备份
- **errors.log**：独立错误日志，仅记录 WARNING 及以上级别，2MB 轮转，保留 3 个备份
- **crash.log**：看门狗模式下记录崩溃时间和 crash 前日志
- 启动时自动清理超出备份数量的旧日志文件
- 日志级别：INFO 及以上写入 trader.log，WARNING+ 同时写入 errors.log

### 8.5 信号处理

- `SIGINT` / `SIGTERM` → `trader.stop()` → 优雅退出主循环

## 九、风险与注意事项

1. **微小盈利策略风险**：依赖高胜率，亏损单可能拉低整体收益
2. **双向持仓风险**：同币种多空同时持仓，极端行情下可能双向亏损
3. **亏损加仓风险**：会放大仓位，需根据风险承受能力谨慎使用
4. **API 延迟**：网络延迟可能影响快速平仓，需确保服务器离交易所近
5. **全平触发**：全平后立刻补仓，如果市场持续波动，可能频繁全平补仓
6. **白名单限制**：`skip_symbols` 仅跳过盈利平仓、对平、加仓，全平时不跳过
7. **动态选币风险**：某币被开仓后下一周期不再满足条件 → 平仓后不再补仓，资金释放
8. **保证金风险**：候选币种过多可能导致保证金不足，需根据账户余额调整 `volume_threshold`

## 附录：版本历史

| 版本 | 日期 | 关键变更 |
|------|------|----------|
| v1.1 | — | 新增亏损加仓功能 |
| v1.2 | — | 动态选币、手续费记录、批量价格刷新、`-4164` 自动重试、前端 API 错误防护 |
| v1.3 | — | 持仓状态独立区块、多空分别统计、前端重构 |
| v1.4 | — | 补仓停补阈值、多空分离加仓阈值、一键重启脚本、启动时停补检查 |
| v1.5 | — | 持仓矩阵全屏首页、亏损统计重构、系统监控面板、动态持仓数量与矩阵大小 |
| v1.6 | — | 矩阵去掉 USDT 后缀、文字自适应、盈利趋势图表、API 响应缓存、排除 USDC 市场、矩阵格子数修复 |
| v1.9 | — | web_server api_account 单次循环、消除多余 get_positions、circuit breaker 自动重试 |
| v2.0 | 2026-05-27 | **稳定性与性能大修**：错误日志独立分离（errors.log）、API 缓存存 dict 防内存泄露、tick 按需 re-fetch positions（5次→1-3次）、静态 HTML 内存缓存、database 消除 __import__ 开销、close_position 精简双try、restart.sh --watch 看门狗 + --status、log.maxBytes 3MB、启动 GC
