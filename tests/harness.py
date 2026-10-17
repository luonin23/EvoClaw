"""测试公共设施：把 tests/ 从项目和外部依赖上解耦。

用标准库 unittest（项目零测试依赖，实盘机器人不引入新包）。
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 用**真实**的 exchange_client，不装桩：装了桩会让 `from main import ...`
# 找不到 ExchangeClient（main 在模块级导入它）。ccxt 本来就是运行依赖，
# 导入它不建连接、不发请求，多花的那点时间换测试跑在真代码上，值。
import exchange_client  # noqa: E402,F401


class FakeClient:
    """可编程的交易所替身：默认什么都不做，测试按需覆盖。

    所有方法都带真实签名（含 strict / positionSide 这些容易写错的地方），
    这样测试才能真正覆盖到线上会走的分支。
    """

    dry_run = False

    def __init__(self, positions=None, open_orders=None, raise_positions=False,
                 raise_open_orders=False, cancel_ok=True, hold=False,
                 price=100.0, trades=None, spot_free=1000.0, min_amt=0.0,
                 min_notional=0.0, futures_available=10.0, asset_free=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.raise_positions = raise_positions          # 模拟接口失败
        self.raise_open_orders = raise_open_orders
        self.cancel_ok = cancel_ok
        self.hold = hold                                # 是否回报空头持仓
        self.price = price
        self.trades = trades or []                      # fetch_my_trades 的返回
        self.spot_free = spot_free                      # 现货可用 USDT
        self.asset_free = asset_free                    # 非 USDT 资产的现货可用（None=不设限）
        self.min_amt = min_amt                          # 最小可下量（0=不限制）
        self.min_notional = min_notional                # 交易所最小下单额（0=不限制）
        self.futures_available = futures_available      # 合约可用 USDT（划转用）
        self.cancelled: list[tuple[str, str]] = []
        self.market_orders: list[tuple] = []
        self.stop_orders: list[tuple] = []
        self.positions_calls = 0
        self.transfers: list[float] = []
        self.spot_orders_status: dict[str, dict] = {}
        self.limit_orders: list[tuple] = []

    # --- 标识 ---
    def user_symbol(self, s: str) -> str:
        return s

    # --- 行情 / 精度 ---
    def last_price(self, sym):
        return self.price

    async def ensure_price(self, sym):
        return self.price

    def round_price(self, sym, p, kind="futures"):
        return round(float(p), 8)

    def round_amount(self, sym, raw, kind="futures"):
        return float(raw)

    def min_amount(self, sym, p, kind="futures"):
        return self.min_amt

    def min_notional_of(self, sym, kind="futures"):
        # 与真实实现同构：返回该市场当前生效的最小下单额
        return self.min_notional

    def amount_for_notional(self, sym, u, p, kind="futures"):
        # 按名义金额换算数量（和真实实现同构）—— 返回固定 1.0 会让
        # 「买 1.5U 却下了 100U」这类超支检查误判。
        # min_notional > 0 时模拟交易所行为：金额不够最小额就抬到最小一手。
        if not p:
            return 0.0
        return max(u, self.min_notional) / p

    def range_pos(self, sym):
        return 0.95

    def market_info(self, sym, kind="futures"):
        return {"contractSize": 1}

    async def get_futures_balance(self):
        return {"balance": 30.0, "available": 10.0}

    # --- 现货（定投 / 利润定投 / 现货筛选 用得到）---
    async def ensure_spot(self):
        return None

    async def refresh_prices(self, symbols):
        return None

    async def get_spot_balance(self, asset="USDT"):
        if asset == "USDT":
            free = self.spot_free
        else:
            free = self.asset_free if self.asset_free is not None else float("inf")
        return {"free": free, "total": free}

    async def ensure_spot_funds(self, needed):
        """与真实实现同构：缺多少划多少；合约也不够就失败。"""
        short = float(needed) - self.spot_free
        if short <= 1e-9:
            return True, 0.0
        if self.futures_available < short:
            return False, 0.0
        await self.transfer_futures_to_spot(short)
        self.futures_available -= short
        return True, short

    async def transfer_futures_to_spot(self, amount):
        self.transfers.append(float(amount))
        self.spot_free += float(amount)
        return {"id": "T1", "amount": amount}

    async def fetch_spot_order(self, order_id, user_symbol):
        return self.spot_orders_status.get(str(order_id))

    async def fetch_spot_open_orders(self, user_symbol):
        return []

    async def cancel_spot_order(self, order_id, user_symbol):
        # 与合约 cancel_order 同构：记录被撤的单、受 cancel_ok 控制
        self.cancelled.append((user_symbol, str(order_id)))
        return self.cancel_ok

    # --- 账户 ---
    async def get_positions(self):
        self.positions_calls += 1
        if self.raise_positions:
            raise RuntimeError("接口 503")
        if self.hold:
            return [{"symbol": "X", "side": "short", "contracts": 5,
                     "entryPrice": 100.0, "liquidationPrice": 130,
                     "unrealizedPnl": 0.0, "markPrice": 100.0}]
        return list(self._positions)

    # --- 挂单 ---
    async def fetch_open_orders(self, sym, kind="futures", strict=False):
        if self.raise_open_orders:
            if strict:
                raise RuntimeError("接口 503")
            return []
        return list(self._open_orders)

    async def fetch_open_algo_orders(self, sym):
        return []

    async def cancel_algo_orders(self, sym):
        return 0

    async def fetch_my_trades(self, sym, since=None, limit=100):
        return list(self.trades)

    async def fetch_order_status(self, oid, sym, kind="futures"):
        return "open"

    # --- 下单 ---
    async def cancel_order(self, oid, sym, kind="futures"):
        self.cancelled.append((sym, str(oid)))
        return self.cancel_ok

    async def create_limit_order(self, sym, side, amt, px, kind="futures",
                                 reduce_only=False, position_side="LONG"):
        oid = f"L{len(self.limit_orders)}"
        self.limit_orders.append((sym, side, amt, px, kind, oid))
        return {"order_id": oid, "price": px, "amount": amt, "dry_run": False}

    async def create_market_order(self, sym, side, amt, kind="futures", position_side="LONG"):
        self.market_orders.append((sym, side, amt))
        return {"order_id": "M1", "price": 100.0}

    async def create_stop_order(self, sym, side, amt, px, position_side="SHORT", kind="futures"):
        self.stop_orders.append((sym, side, px))
        return {"order_id": "S1"}


def temp_db():
    """返回 (Database, 路径)。调用方负责 db.close() 并删除文件。"""
    from database import Database
    path = tempfile.mktemp(suffix=".db")
    return Database(path), path


def grid_cfg(**over):
    """一份最小可用的 grid 配置；用 kwargs 覆盖个例需要的键。"""
    base = {"min_tp_ratio": 0.002, "step_ratio": 0.3, "auto_range_pct": 0.15,
            "entry_limit_only": True, "max_exposure_ratio": 3.0, "leverage": 3,
            "short_enabled": False, "short_pos_min": 0.85, "short_confirm": 0.01,
            "short_entry_offset": 0.025, "short_stop_pct": 0.035, "short_tp_pct": 0.10}
    base.update(over)
    return {"grid": base}


def make_grid(client, db, config, selection=None):
    """构造 GridStrategy 并预设选币清单（_selection 平时由选币流程填充）。"""
    from strategies.grid import GridStrategy
    g = GridStrategy(client=client, db=db, config=config)
    if selection is not None:
        g._selection = [{"symbol": s} for s in selection]
    return g
