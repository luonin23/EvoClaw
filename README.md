# EvoClaw v2.0

双模式自动交易系统：**低杠杆网格**（合约）· **现货定投**。

## 核心原则

1. **不设止损，也不被强平** —— 靠低杠杆（默认 3x）+ 账户敞口闸门（总名义 ≤ 权益 3 倍）实现。
2. **模拟盘优先** —— 默认 `dry_run=true`，完整计算并记录每一笔「本应下的单」，但不发真实订单。
3. **双模式隔离** —— 通过配置切换，同时只跑一种。

## 快速开始

```bash
pip install -r requirements.txt
python3 main.py          # 或 ./restart-v2.sh
```

- 控制台：http://localhost:8091
- 介绍页：http://localhost:8091/intro.html
- 首次启动会在日志里打印随机管理密码（写接口需要）

## 配置

- 数据库 `config` 表是唯一真源；网页「交易配置」可改（需密码）。
- 关键参数：
  - `mode`: `off` / `grid` / `dca`
  - `dry_run`: `true`（安全）/ `false`（实盘）
  - `grid.leverage` / `grid.max_exposure_ratio`: **两次爆仓换来的保险丝，请勿调高**
  - `grid.symbols[]`: `symbol / lower / upper / grids / size_usdt`（lower/upper 填 0 = 首次按现价自动生成）

## 目录

```
main.py            入口：日志 / PID锁 / 启动 / Web
config.py          默认配置
database.py        SQLite（WAL + 跨线程锁 + 启动备份）
exchange_client.py ccxt 封装（合约 + 现货，精度/最小额/错误码）
engine.py          主循环，按 mode 调度策略
strategies/grid.py 方案B 低杠杆网格
strategies/dca.py  方案C 现货定投
web_server.py      aiohttp API（写接口需鉴权）
web/index.html     仪表盘
web/intro.html     介绍页
```

## 历史

- **v1.0**（已归档，tag `v1.0-final` / 分支 `archive/v1`）：对冲 + 分档止盈 + 亏损加仓。
  实测止盈 +361.6U，但两次强平 −516.6U，净 −155U。根因：单边止盈藏浮亏 + 17 倍账户杠杆。
- **v2.0**（当前）：改用低杠杆、可控敞口的网格与定投。
