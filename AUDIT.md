# EvoClaw 代码审计报告

> 生成时间：2026-07-08
> 审计范围：`main.py`, `trader.py`, `exchange_client.py`, `database.py`, `web_server.py`, `web/index.html`, `web/intro.html`, `config.json`, `ecosystem.config.json`, `health_monitor.sh`, `restart.sh`
>
> **状态更新**：截至提交 `79fccb0`，本报告中大部分 bug 与缺陷已修复，包括 `trader.py` 两个高优先级 bug、手续费不一致、symbol map 错误、前端 XSS、`config.json` 移除等。Web API 鉴权与 SQLite 并发锁因用户明确排除未改动。

---

## 1. 执行摘要

EvoClaw 是一个基于 Python asyncio + CCXT 的 Binance USDS-M 永续合约对冲交易机器人，采用 SQLite 持久化，aiohttp 提供 Web 仪表盘。整体架构清晰，核心交易逻辑集中在 `trader.py`，但存在若干**高优先级 bug**、**安全风险**和**可维护性问题**需要尽快修复。

### 关键发现（按优先级）

| 优先级 | 问题 | 影响 |
|--------|------|------|
| 🔴 高 | `trader.py:266` 海象运算符优先级 bug，导致 stale-price 告警逻辑完全错误 | 价格异常时告警策略失效 |
| 🔴 高 | `trader.py:101` f-string 条件表达式 bug，heartbeat 日志可能只剩 `price_age=N/A` | 关键运行信息丢失 |
| 🔴 高 | Web 配置接口无鉴权，任何人可修改交易配置和 API Key | 资金安全风险 |
| 🟡 中 | `exchange_client.py:101` symbol map 构建逻辑错误 | 部分币种可能无法解析下单 |
| 🟡 中 | `database.py:13` `check_same_thread=False` 且无锁，并发任务可能触发 SQLite 线程错误 | 运行时崩溃/数据损坏风险 |
| 🟡 中 | 矩阵图每次都全量重建 DOM，且无数据 diff | 性能差、闪烁 |
| 🟢 低 | 多处硬编码手续费 0.0005 | 费率变化时盈亏统计失真 |
| 🟢 低 | 前端单文件 2100+ 行，无模块拆分 | 维护困难 |

---

## 2. 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                          main.py                             │
│  启动器：PID 锁、日志、配置迁移、市场加载、仓位修复、启动服务   │
└──────────────────┬──────────────────────────────────────────┘
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
┌─────────┐  ┌──────────┐  ┌─────────────┐
│ trader  │  │ exchange │  │   web_server │
│  .py    │  │ _client  │  │    .py       │
│ 交易引擎 │  │ CCXT 封装 │  │  API + 静态页│
└────┬────┘  └────┬─────┘  └──────┬──────┘
     │            │               │
     └────────────┼───────────────┘
                  ▼
            ┌──────────┐
            │ database │
            │  .py     │
            │ SQLite   │
            └──────────┘
```

### 核心模块职责

| 文件 | 职责 | 行数（约） |
|------|------|-----------|
| `main.py` | 入口、引导、PID 锁、配置迁移、启动 Web/Trader | 386 |
| `trader.py` | 交易循环、止盈/加仓/补仓/平仓/风控逻辑 | 846 |
| `exchange_client.py` | CCXT 封装、下单、行情、仓位查询、强平记录 | 504 |
| `database.py` | SQLite 持久化、统计、配置、slot 分配 | 504 |
| `web_server.py` | aiohttp API、静态文件、缓存 | 561 |
| `web/index.html` | 交易终端（仪表盘/矩阵/配置） | 2110+ |
| `web/intro.html` | 营销落地页 | 1420+ |

---

## 3. 逐文件分析

### 3.1 `main.py` — 启动与引导

#### 设计亮点
- 使用 `fcntl` PID 文件锁防止重复启动实例。
- 配置从 `config.json` 迁移到 SQLite，数据库成为配置唯一来源。
- 启动时与交易所仓位对账，修复 DB 与交易所不一致。
- 信号处理（SIGINT/SIGTERM）实现优雅关闭。

#### 缺陷与 bug
- **非致命错误被吞掉**：市场加载、仓位获取、强平回填等启动错误只记录 warning 后继续，可能导致在错误状态下运行。
- **`get_positions` 失败时 `positions = []`**（约 line 285）：若启动时交易所 API 超时，会误判为空仓，进而尝试大量开仓，可能超出仓位上限。
- **手续费计算不一致**：
  - `main.py:319` 计算开仓手续费：`average * amount * contract_size * 0.0005`
  - `database.py:231` 默认手续费：`entry_price * amount * 0.0005`
  - 两者一个乘了 `contract_size`，一个没乘，导致不同代码路径的手续费不一致。
- **直接访问 `db.conn`**（line 195）：绕过 `Database` 抽象层查询 `liquidations` 数量。

---

### 3.2 `trader.py` — 交易引擎

#### 设计亮点
- 单事件循环 asyncio，无锁设计，简化并发模型。
- 每一步交易后都会重新拉取仓位，保持状态一致。
- 实现了分层止盈、全仓止盈、对平、加仓、补仓等完整策略。
- `-2027`（最大持仓限制）断路器和加仓冷却机制。

#### 高优先级 bug
1. **海象运算符优先级 bug（`trader.py:266-272`）**
   ```python
   if tick_count := getattr(self, '_stale_warn_count', 0) == 0:
       log.error("...")
   self._stale_warn_count = (tick_count + 1) % 60
   ```
   `:=` 优先级低于 `==`，所以 `tick_count` 实际上是 `True/False` 而不是计数。逻辑完全错误，应该改为：
   ```python
   tick_count = getattr(self, '_stale_warn_count', 0)
   if tick_count == 0:
       log.error("...")
   self._stale_warn_count = (tick_count + 1) % 60
   ```

2. **heartbeat 日志条件表达式 bug（`trader.py:101`）**
   ```python
   log.info(
       f"HEARTBEAT: tick={tick_count} ... "
       f"price_age=..." if self._last_price_ok > 0 else "price_age=N/A"
   )
   ```
   `if/else` 只作用于最后一个 f-string。当 `_last_price_ok <= 0` 时，整条日志输出变成 `"price_age=N/A"`，前面的 tick/pos/price 等信息全部丢失。

3. **`_record_trade` 的 `open_time` 永远为空字符串（`trader.py:831-835`）**
   ```python
   "open_time": "",
   ```
   没有从数据库的 `entry_time` 读取，导致历史交易记录缺少开仓时间。

#### 中优先级缺陷
- **`check_all_close` 手续费估算错误**：当 `open_fee <= 0` 且仓位不在 `_system_pos_map` 时，用原始 `contracts` 反推手续费。若该仓位曾被部分平仓，剩余合约数已变化，手续费估算会错。
- **`_record_2027_failure` 在任何加仓失败时都会被调用**：即使失败原因是网络错误而非 `-2027`，也会触发断路器。
- **margin call 清除失败连击逻辑**：当仓位盈利时清除失败连击，若随后再次亏损，可立即重新加仓，可能形成快速加仓循环。
- **强平检测取最早记录**：`force_map` 保留 10 分钟内第一次强平记录，若同一币种在短时间内多次强平，可能用错数据。
- **`_record_trade` 声明为 `async` 但无 I/O**：可改为同步函数，减少事件循环开销。

#### 设计缺陷
- **无止损机制**：只有加仓摊薄（martingale），没有硬止损，极端行情下可能大幅亏损。
- **单币种仓位无上限**：仅限制总仓位数量，单个币种可能占用大量保证金。
- **手续费 0.0005 多处硬编码**：Binance 费率变动或 BNB 折扣时统计失真。
- **交易所与 DB 更新非原子**：下单成功后若进程崩溃，DB 可能丢失该仓位；虽然有启动修复，但不是原子保证。

---

### 3.3 `exchange_client.py` — 交易所封装

#### 设计亮点
- 基于 `ccxt.async_support.binanceusdm`，成熟稳定。
- 所有 CCXT 调用包裹 `asyncio.wait_for` 超时。
- `-4164`（名义值过小）自动递增金额重试。
- 使用 `LogThrottle` 抑制重复日志。

#### 高优先级 bug
1. **symbol map 构建逻辑错误（`exchange_client.py:101-105`）**
   ```python
   if exchange_id and exchange_id not in self.market_info:
       self.symbol_map[exchange_id] = symbol
   base = symbol.replace(":", "").replace("/", "")
   if base and base not in self.market_info:
       self.symbol_map[base] = symbol
   ```
   这里应该检查 `not in self.symbol_map`，却错误地检查了 `not in self.market_info`。这会导致部分别名未被加入 `symbol_map`，用户输入的某些币种无法解析。

#### 中优先级缺陷
- **`close_all_positions` 丢弃异常结果**：
  ```python
  results = await asyncio.gather(*tasks, return_exceptions=True)
  return [r for r in results if not isinstance(r, BaseException)]
  ```
  如果内部任务抛出异常（而非常规返回错误 dict），失败被静默丢弃。
- **网络错误无重试**：`refresh_prices`、`get_candidate_symbols`、`fetch_liquidations`、`get_positions` 仅在失败时记录日志，没有重试机制。
- **固定延迟重试**：`open_position` 使用固定 0.2s 重试，没有指数退避。
- **未处理 `ccxt.NetworkError` / `ccxt.ExchangeNotAvailable`**：统一 catch `Exception`，无法区分可重试与永久错误。

---

### 3.4 `database.py` — 数据持久化

#### 设计亮点
- SQLite WAL 模式提升并发性能。
- 完整的统计表、交易表、持仓表、强平表、配置表。
- `insert_trade` 原子地更新交易记录和统计。

#### 高优先级缺陷
- **`check_same_thread=False` 且无锁（`database.py:13`）**
  ```python
  sqlite3.connect(db_path, check_same_thread=False)
  ```
  虽然 asyncio 是单线程，但多个协程可能并发调用 DB。SQLite 连接不是线程安全的，缺少 `asyncio.Lock` 或 threading lock 可能导致 `SQLite objects created in a thread can only be used in that same thread` 错误，甚至在极端情况下损坏数据。
  **建议**：所有 DB 操作外包一个 `asyncio.Lock`；或在事件循环中序列化 DB 调用。

#### 中优先级缺陷
- **迁移循环吞掉所有异常（`database.py:69-80`）**
  ```python
  try:
      self.conn.execute(f"ALTER TABLE ...")
  except Exception:
      pass
  ```
  会静默忽略真正的数据库错误。
- **`_rebuild_stats` 先 DELETE 再 INSERT**：若中途崩溃，统计表为空。
- **`save_config` 先 DELETE 再 INSERT**：若中途崩溃，配置丢失。
- **`insert_trade` 多步操作无事务包裹**：交易记录插入后、统计更新前崩溃会导致不一致。
- **checkpoint 与 checkpoint_restart 实现完全相同**：冗余。

---

### 3.5 `web_server.py` — Web 服务与 API

#### 设计亮点
- aiohttp 轻量服务，内嵌静态页。
- 15 秒 API 缓存，减少交易所/DB 压力。
- 配置 GET 接口对 API Key/Secret 做脱敏。

#### 高优先级安全缺陷
- **所有 API 均无鉴权**
  - `POST /api/config` 可直接修改交易配置和交易所 API Key。
  - `POST /api/web-config` 可直接修改前端配置。
  - 前端密码 `19830422` 仅在前端 JS 校验，后端不验证。
  **影响**：任何能访问 `http://IP:8080` 的人都可以控制交易机器人或窃取/替换 API Key。

#### 中优先级缺陷
- **`api_liquidations` 同步阻塞事件循环（`web_server.py:542-548`）**
  在 async handler 中直接调用 `self.db.conn.execute(...)`，会阻塞整个事件循环。
- **`api_system` 缓存判断 bug（`web_server.py:388`）**
  ```python
  if self._system_cache[1] and now - self._system_cache[0] < 5:
  ```
  如果缓存数据是空 dict（falsy），会绕过缓存重新读取 `/proc`。
- **`api_account` 中 `balance <= 0` 时强制设为 1**：掩盖真实余额为 0 的情况，影响盈亏率展示。
- **无 CORS、无速率限制、无输入校验**：`skip_symbols` 等配置直接写入，存在被滥用的可能。
- **静态文件缓存永不过期**：`_static_cache` 加载后不再刷新，修改 HTML 需重启服务。

---

### 3.6 `web/index.html` — 交易终端

#### 设计亮点
- 完整的 CSS 设计系统（`:root` tokens），暗色主题。
- 三个 tab（仪表盘/矩阵/配置）通过 URL hash 可直连。
- Canvas 自绘盈利趋势图，无需外部图表库。
- 背景粒子和 pipeline 数据流粒子增强视觉。

#### 高优先级缺陷
- **多处 XSS 风险（用户输入未转义直接插入 HTML）**
  - `loadAccount` 中 `active_symbols` 直接 `innerHTML`。
  - `loadLiquidations` 中 `top10` 和 `events` 直接拼接到 `innerHTML`。
  - 矩阵 tooltip 直接插入 `slot.symbol`。
  - 虽然 symbol 来自交易所，但若被污染仍可执行 XSS。

#### 中优先级缺陷
- **矩阵图每次刷新全量重建 DOM**：`loadFullMatrix` 每次 `container.innerHTML = ''` 并重建所有 cell，100+ 仓位时性能差、可能闪烁。
- **多个 API 同时请求无去重**：每次 refresh 同时发 `/api/account`、`/api/stats`、`/api/profit-trend`、`/api/liquidations`。
- **toggle trading 提示文案逻辑反了（`index.html:1103`）**：
  ```javascript
  showToast(trading ? '交易已停止' : '交易已启动');
  ```
  切换后 `trading` 已更新，导致提示与实际状态相反。
- **全局命名空间污染**：大量 `window._xxx` 和下划线全局变量，无模块拆分。

#### 已在前序迭代中修复的问题
- ✅ 矩阵图 cell 进入动画已移除，改为即时刷新。
- ✅ 矩阵图在手机端高度不一致已修复（`grid-auto-rows: 1fr`）。
- ✅ 粒子效果已覆盖全页。

---

### 3.7 `web/intro.html` — 营销落地页

#### 设计亮点
- 视觉风格统一，10 个 section 完整展示产品。
- 背景粒子 + IntersectionObserver  reveal 动画。
- 响应式布局，移动端适配。

#### 缺陷
- **与 `index.html` 大量 CSS 重复**：`:root` tokens、字体、aurora 动画、网格背景均重复声明，无共享样式文件。
- **无实质安全风险**：没有动态渲染用户输入。

---

### 3.8 配置与运维脚本

#### `ecosystem.config.json`
- PM2 配置合理，`max_memory_restart: 512M` 防止内存泄漏导致崩溃。
- 缺少 `env` 区分开发/生产。

#### `health_monitor.sh`
- 检查 `/api/health` 和进程存活，300 秒阈值与 `main.py` 一致。
- 使用 `kill -9` 强制重启过于粗暴，可能丢失正在执行的交易状态。

#### `restart.sh`
- 遗留脚本，已被 PM2 取代，建议删除或标记为 deprecated。

#### `.git-broken`
- 项目根目录存在 `.git-broken` 备份目录，应在确认无用后清理，避免仓库污染。

---

## 4. 详细 bug 清单

| # | 文件 | 行 | 问题 | 严重度 | 修复建议 |
|---|------|-----|------|--------|----------|
| 1 | `trader.py` | 266-272 | 海象运算符优先级导致 `_stale_warn_count` 逻辑错误 | 🔴 高 | 拆分为赋值 + 比较 |
| 2 | `trader.py` | 101 | heartbeat f-string 条件表达式导致日志信息丢失 | 🔴 高 | 将 if/else 提到 f-string 外部 |
| 3 | `web_server.py` | 30-45 | 所有 API 无鉴权 | 🔴 高 | 增加 token/cookie 鉴权或仅监听 localhost/内网 |
| 4 | `exchange_client.py` | 101-105 | symbol map 构建检查错误 | 🟡 中 | 改为 `not in self.symbol_map` |
| 5 | `database.py` | 13 | `check_same_thread=False` 且无并发锁 | 🟡 中 | 增加 `asyncio.Lock` 保护 DB 操作 |
| 6 | `trader.py` | 831-835 | `_record_trade` 的 `open_time` 永远为空 | 🟡 中 | 从 `entry_time` 读取 |
| 7 | `web_server.py` | 542-548 | 强平接口同步阻塞事件循环 | 🟡 中 | 使用 `asyncio.to_thread` 或专门的 executor |
| 8 | `web/index.html` | 1172, 1701, 1716, 1274 | XSS：用户输入直接插入 innerHTML | 🟡 中 | 使用 `textContent` 或模板转义 |
| 9 | `web/index.html` | 1103 | toggle trading 提示文案反了 | 🟢 低 | 改为 `!trading` |
| 10 | `main.py` | 319 / `database.py` | 手续费计算不一致 | 🟢 低 | 统一手续费计算函数 |
| 11 | `web_server.py` | 388 | system cache 判断误用 truthiness | 🟢 低 | 用 `is not None` |
| 12 | `exchange_client.py` | 500-503 | `close_all_positions` 静默吞异常 | 🟢 低 | 记录并返回失败明细 |

---

## 5. 设计缺陷

### 5.1 安全架构缺失
- 后端无任何鉴权机制，公网部署等于把交易机器人控制权交给任何人。
- API Key 在 `/api/config` GET 中部分脱敏，但 POST 可写入完整密钥。

### 5.2 并发与数据一致性
- DB 连接无锁保护。
- 交易所下单与 DB 记录非原子。
- 多个协程可能并发读写 SQLite。

### 5.3 风险策略
- 只有加仓（martingale）没有止损，极端行情下风险敞口无限放大。
- 单币种无仓位上限，可能过度集中。

### 5.4 可维护性
- 前端两个 HTML 文件共 3500+ 行，内联 CSS/JS，无模块拆分。
- 大量魔法数字和硬编码配置。
- 无单元测试/集成测试。

### 5.5 性能
- 矩阵图每次全量重建 DOM。
- 前端同时发起多个 API 请求且无去重。
- 背景粒子持续运行，虽然已限制 30fps，但仍消耗资源。

---

## 6. 优先改进建议

### 立刻执行（本周）
1. **修复 `trader.py:266` 和 `trader.py:101` 的 bug**。
2. **为 Web API 增加鉴权**：至少增加一个后端验证的 config token，并在前端保存到 `localStorage`；生产环境建议配合 Nginx Basic Auth 或仅监听 `127.0.0.1`。
3. **修复 `exchange_client.py:101` symbol map bug**。
4. **给 SQLite 操作加 `asyncio.Lock`**，或确保所有 DB 调用在单个协程/线程中序列化。

### 短期执行（本月）
5. **矩阵图改为 diff 更新**：按 symbol 做 key，只更新数据/位置变化，避免全量重建。
6. **前端 XSS 全面修复**：所有动态内容使用 `textContent` 或 HTML 转义。
7. **统一手续费计算**：提取 `calculate_fee(amount, price, contract_size)` 函数。
8. **修复 `_record_trade` 的 `open_time`** 和 toggle trading 提示文案。
9. **`api_liquidations` 改为异步执行**，避免阻塞事件循环。

### 中期执行（未来 1-3 月）
10. **增加止损机制**（按币种或账户整体）。
11. **前端模块化重构**：将 CSS/JS 拆分为独立文件，引入构建工具（Vite/Parcel）或至少按组件拆分。
12. **增加测试覆盖**：至少覆盖 `trader.py` 核心决策函数和 `exchange_client.py` 的下单/重试逻辑。
13. **配置变更审计日志**：记录谁、何时修改了配置和 API Key。
14. **清理 `.git-broken` 和废弃的 `restart.sh`**。

---

## 7. 最小修复代码示例

### 7.1 修复 `trader.py:266-272`
```python
# 修改前
if tick_count := getattr(self, '_stale_warn_count', 0) == 0:
    log.error("PRICE STALE: using cached prices, age=%.0fs", age)
self._stale_warn_count = (tick_count + 1) % 60

# 修改后
tick_count = getattr(self, '_stale_warn_count', 0)
if tick_count == 0:
    log.error("PRICE STALE: using cached prices, age=%.0fs", age)
self._stale_warn_count = (tick_count + 1) % 60
```

### 7.2 修复 `trader.py:101`
```python
# 修改前
log.info(
    f"HEARTBEAT: tick={tick_count} ... "
    f"price_age={time.monotonic() - self._last_price_ok:.0f}s" if self._last_price_ok > 0 else "price_age=N/A"
)

# 修改后
price_age = f"{time.monotonic() - self._last_price_ok:.0f}s" if self._last_price_ok > 0 else "N/A"
log.info(
    f"HEARTBEAT: tick={tick_count} pos={len(self._system_pos_map)} "
    f"skipped_2027={len(self._fail2027_skipped_at)} mc_cooldowns={len(self._mc_last_success)} "
    f"price_age={price_age}"
)
```

### 7.3 修复 `exchange_client.py:101-105`
```python
# 修改前
if exchange_id and exchange_id not in self.market_info:
    self.symbol_map[exchange_id] = symbol
base = symbol.replace(":", "").replace("/", "")
if base and base not in self.market_info:
    self.symbol_map[base] = symbol

# 修改后
if exchange_id and exchange_id not in self.symbol_map:
    self.symbol_map[exchange_id] = symbol
base = symbol.replace(":", "").replace("/", "")
if base and base not in self.symbol_map:
    self.symbol_map[base] = symbol
```

### 7.4 为 `database.py` 加锁
```python
import asyncio

class Database:
    def __init__(self, db_path):
        self._lock = asyncio.Lock()
        # ...

    async def execute(self, query, params=()):
        async with self._lock:
            return self.conn.execute(query, params)
```

---

## 8. 结论

EvoClaw 在交易策略和整体架构上已有相当完整的设计，但在**关键 bug 修复**、**安全加固**、**并发数据一致性**、**前端性能与安全**方面仍有明显改进空间。建议优先修复本报告列出的高优先级问题，再逐步推进中长期重构。

> 注意：本审计基于静态代码分析，未包含运行时行为验证。部分缺陷（如 SQLite 并发问题）需要在真实高负载或异常网络环境下才能稳定复现。
