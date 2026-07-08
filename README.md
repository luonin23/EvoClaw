# EvoClaw

高速微盈利加密货币自动交易系统，基于 Binance USDT-M 永续合约双向持仓模式（Hedge Mode）。

## 核心特性

- **微盈利策略**：单币种盈利 0.2% 即平仓，追求高胜率、快周转
- **双向持仓**：同一币种同时持有 LONG + SHORT，对冲单边风险
- **动态选币**：按 24h 成交量和币单价自动筛选交易对，无需手动维护
- **账户级全平**：整体盈利达标时一键全平，快速落袋
- **亏损加仓**：亏损达标时自动追加仓位，降低平均成本
- **多空对平**：多空持仓平均盈利达标时双向平仓
- **停补保护**：对方仓位偏离过大时自动停止补仓，防止无限加仓
- **持仓矩阵**：100 格可视化矩阵，实时展示每单盈亏状态

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.12 + asyncio |
| 交易所 | ccxt (binanceusdm) |
| Web | aiohttp |
| 数据库 | SQLite (WAL 模式) |
| 前端 | 单 HTML + 原生 JS |

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务（首次启动会用内置默认配置初始化 SQLite）
python3 main.py

# 3. 访问 Web UI，在「交易配置」页填写 API 密钥
open http://localhost:8080
```

## 配置说明

所有配置均持久化在 `data/evoclaw.db` 中。首次启动时，`main.py` 会用内置默认配置初始化数据库；之后所有配置都通过 Web UI 或 `/api/config` 修改。

首次使用必须在 Web UI 的「交易配置」页填写：
- `exchange_kwargs.apiKey` — Binance API Key
- `exchange_kwargs.secret` — Binance API Secret

主要配置项：

| 字段 | 说明 |
|------|------|
| `side` | 交易方向：`both` / `long` / `short` |
| `profit_tiers` | 分层止盈档位，每档包含 `threshold`（盈利率）和 `close_pct`（平仓比例） |
| `replenish_stop_threshold` | 停补阈值：对方仓位偏离≥此比例时停止补仓 |
| `max_position_count` | 最高持仓数量上限 |
| `enable_all_close` | 启用账户级全平 |
| `enable_margin_call` | 启用亏损加仓 |
| `margin_call_threshold_long/short` | 多空加仓亏损阈值 |
| `volume_threshold` | 24h 成交量筛选（USDT） |
| `price_threshold` | 币单价筛选（USDT），只选价格≤此值的币 |

## 一键重启

```bash
./restart.sh
```

自动完成：停止进程 → 清理端口 → 备份日志 → 启动服务。

## 系统要求

- Python 3.12+
- Binance 账户已开通 USDT-M 合约交易
- 账户已启用 **双向持仓模式（Hedge Mode）**
- 服务器建议部署在靠近交易所的区域以降低延迟

## 风险提示

1. 本系统为自动化交易程序，使用即表示您了解并愿意承担相关风险
2. 双向持仓模式下，极端行情可能导致多空同时亏损
3. 亏损加仓功能会放大仓位，请根据风险承受能力谨慎使用
4. 建议先用小资金测试，确认策略符合预期后再加大投入

## 设计文档

详见 [DESIGN.md](./DESIGN.md)，包含完整的技术架构、模块设计、核心流程和踩坑记录。
