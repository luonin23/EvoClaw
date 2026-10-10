"""EvoClaw v2.0 — 数据层。

与 v1 相同的安全实践：WAL 模式 + 单连接跨线程加锁 + 启动备份 + TRUNCATE 检查点。
v2 使用独立数据库文件（data/evoclaw-v2.db），与 v1 完全隔离。
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/evoclaw-v2.db"):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # 单连接会被事件循环线程与 to_thread 线程池同时使用，必须有锁保护
        # 多语句事务（sqlite3 只保证单条语句原子）。
        self._lk = threading.RLock()
        self._init_tables()
        # 所有复合写方法统一加锁
        for _name in (
            "save_config", "seed_config", "set_runtime_stat", "insert_order",
            "update_order_status", "upsert_grid_state", "replace_grid_levels",
            "insert_grid_level", "fill_grid_level", "mark_grid_level",
            "upsert_dca_state", "insert_dca_record",
            "backup_to", "checkpoint",
        ):
            setattr(self, _name, self._make_locked(getattr(self, _name)))

    def _make_locked(self, fn):
        def wrapper(*args, **kwargs):
            with self._lk:
                return fn(*args, **kwargs)
        return wrapper

    # ===== 建表 =====
    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_stats (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- 所有成交（含 dry-run 模拟单）
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                mode TEXT NOT NULL,            -- grid | dca
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,            -- buy | sell
                type TEXT NOT NULL,            -- grid_buy | grid_sell | dca_buy | close
                price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,  -- 名义价值 (price*amount)
                fee REAL NOT NULL DEFAULT 0,
                order_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'filled',
                dry_run INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
            CREATE INDEX IF NOT EXISTS idx_orders_mode ON orders(mode);

            -- 网格配置与累计状态（每 symbol 一行）
            CREATE TABLE IF NOT EXISTS grid_state (
                symbol TEXT PRIMARY KEY,
                lower REAL NOT NULL DEFAULT 0,
                upper REAL NOT NULL DEFAULT 0,
                grids INTEGER NOT NULL DEFAULT 0,
                size_usdt REAL NOT NULL DEFAULT 0,
                base_qty REAL NOT NULL DEFAULT 0,      -- 当前持仓数量
                total_cost REAL NOT NULL DEFAULT 0,    -- 持仓总成本
                realized_pnl REAL NOT NULL DEFAULT 0,
                buy_count INTEGER NOT NULL DEFAULT 0,
                sell_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- 网格挂单/成交明细
            CREATE TABLE IF NOT EXISTS grid_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                level_idx INTEGER NOT NULL,
                price REAL NOT NULL,
                side TEXT NOT NULL,            -- buy | sell
                amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending | filled
                order_id TEXT NOT NULL DEFAULT '',
                filled_ts TEXT NOT NULL DEFAULT '',
                created_ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_grid_levels_sym ON grid_levels(symbol, level_idx);

            -- 定投每期记录
            CREATE TABLE IF NOT EXISTS dca_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                dry_run INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_dca_records_ts ON dca_records(ts);

            -- 定投累计状态（每 symbol 一行）
            CREATE TABLE IF NOT EXISTS dca_state (
                symbol TEXT PRIMARY KEY,
                total_cost REAL NOT NULL DEFAULT 0,
                total_qty REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                buy_count INTEGER NOT NULL DEFAULT 0,
                last_buy_ts TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()
        # 迁移：为已存在的 grid_state 补列
        for _col, _type in [("total_cost", "REAL NOT NULL DEFAULT 0")]:
            try:
                self.conn.execute(f"ALTER TABLE grid_state ADD COLUMN {_col} {_type}")
                self.conn.commit()
            except Exception:
                pass

    # ===== 配置 =====
    def load_config(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM config").fetchall()
        cfg = {}
        for key, raw in rows:
            try:
                cfg[key] = json.loads(raw)
            except Exception:
                cfg[key] = raw
        return cfg

    def save_config(self, config: dict):
        self.conn.execute("DELETE FROM config")
        for key, val in config.items():
            self.conn.execute("INSERT INTO config (key, value) VALUES (?, ?)",
                              (key, json.dumps(val)))
        self.conn.commit()

    def seed_config(self, defaults: dict) -> bool:
        existing = self.conn.execute("SELECT COUNT(*) FROM config").fetchone()[0]
        if existing == 0:
            self.save_config(defaults)
            return True
        return False

    # ===== 运行时状态 =====
    def get_runtime_stat(self, key: str, default: float = 0) -> float:
        row = self.conn.execute("SELECT value FROM runtime_stats WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_runtime_stat(self, key: str, value: float):
        self.conn.execute(
            "INSERT INTO runtime_stats (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    # ===== 订单 =====
    def insert_order(self, o: dict):
        self.conn.execute(
            """INSERT INTO orders
               (ts, mode, symbol, side, type, price, amount, cost, fee, order_id, status, dry_run, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (o.get("ts") or datetime.now(timezone.utc).isoformat(),
             o["mode"], o["symbol"], o["side"], o["type"],
             float(o.get("price", 0)), float(o.get("amount", 0)), float(o.get("cost", 0)),
             float(o.get("fee", 0)), str(o.get("order_id", "")),
             o.get("status", "filled"), 1 if o.get("dry_run") else 0, o.get("note", "")),
        )
        self.conn.commit()

    def update_order_status(self, order_id: str, status: str):
        self.conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        self.conn.commit()

    def get_orders(self, limit: int = 100, mode: str = None) -> list[dict]:
        if mode:
            rows = self.conn.execute(
                """SELECT id, ts, mode, symbol, side, type, price, amount, cost, fee,
                          order_id, status, dry_run, note
                   FROM orders WHERE mode=? ORDER BY id DESC LIMIT ?""", (mode, limit)).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT id, ts, mode, symbol, side, type, price, amount, cost, fee,
                          order_id, status, dry_run, note
                   FROM orders ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        cols = ["id", "ts", "mode", "symbol", "side", "type", "price", "amount",
                "cost", "fee", "order_id", "status", "dry_run", "note"]
        return [dict(zip(cols, r)) for r in rows]

    # ===== 网格 =====
    def get_grid_state(self, symbol: str = None):
        if symbol:
            row = self.conn.execute(
                """SELECT symbol, lower, upper, grids, size_usdt, base_qty,
                          realized_pnl, buy_count, sell_count FROM grid_state WHERE symbol=?""",
                (symbol,)).fetchone()
            if not row:
                return None
            cols = ["symbol", "lower", "upper", "grids", "size_usdt", "base_qty",
                    "total_cost", "realized_pnl", "buy_count", "sell_count"]
            return dict(zip(cols, row))
        rows = self.conn.execute(
            """SELECT symbol, lower, upper, grids, size_usdt, base_qty, total_cost,
                      realized_pnl, buy_count, sell_count FROM grid_state""").fetchall()
        cols = ["symbol", "lower", "upper", "grids", "size_usdt", "base_qty",
                "total_cost", "realized_pnl", "buy_count", "sell_count"]
        return [dict(zip(cols, r)) for r in rows]

    def upsert_grid_state(self, symbol: str, **fields):
        self.conn.execute(
            "INSERT INTO grid_state (symbol, updated_at) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO NOTHING",
            (symbol, datetime.now(timezone.utc).isoformat()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE grid_state SET {cols}, updated_at=? WHERE symbol=?",
                (*fields.values(), datetime.now(timezone.utc).isoformat(), symbol))
        self.conn.commit()

    def get_grid_levels(self, symbol: str = None) -> list[dict]:
        if symbol:
            rows = self.conn.execute(
                """SELECT id, symbol, level_idx, price, side, amount, status, order_id,
                          filled_ts, created_ts FROM grid_levels WHERE symbol=?
                   ORDER BY level_idx""", (symbol,)).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT id, symbol, level_idx, price, side, amount, status, order_id,
                          filled_ts, created_ts FROM grid_levels ORDER BY symbol, level_idx""").fetchall()
        cols = ["id", "symbol", "level_idx", "price", "side", "amount", "status",
                "order_id", "filled_ts", "created_ts"]
        return [dict(zip(cols, r)) for r in rows]

    def replace_grid_levels(self, symbol: str, levels: list[dict]):
        """重建某 symbol 的全部网格档位（改区间/格数时调用）。"""
        self.conn.execute("DELETE FROM grid_levels WHERE symbol=?", (symbol,))
        now = datetime.now(timezone.utc).isoformat()
        for lv in levels:
            self.conn.execute(
                """INSERT INTO grid_levels
                   (symbol, level_idx, price, side, amount, status, order_id, filled_ts, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, lv["level_idx"], lv["price"], lv["side"], lv.get("amount", 0),
                 lv.get("status", "pending"), lv.get("order_id", ""), lv.get("filled_ts", ""), now))
        self.conn.commit()

    def insert_grid_level(self, symbol: str, level_idx: int, price: float, side: str,
                          amount: float, status: str = "pending") -> int:
        """插入一条挂单（网格用订单模型：成交后生成反向单）。"""
        cur = self.conn.execute(
            """INSERT INTO grid_levels
               (symbol, level_idx, price, side, amount, status, order_id, filled_ts, created_ts)
               VALUES (?, ?, ?, ?, ?, ?, '', '', ?)""",
            (symbol, level_idx, price, side, amount, status,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return cur.lastrowid

    def fill_grid_level(self, row_id: int, order_id: str = ""):
        self.conn.execute(
            "UPDATE grid_levels SET status='filled', order_id=?, filled_ts=? WHERE id=?",
            (order_id, datetime.now(timezone.utc).isoformat(), row_id))
        self.conn.commit()

    def mark_grid_level(self, symbol: str, level_idx: int, status: str,
                        order_id: str = "", amount: float = None):
        if amount is None:
            self.conn.execute(
                "UPDATE grid_levels SET status=?, order_id=?, filled_ts=? "
                "WHERE symbol=? AND level_idx=?",
                (status, order_id,
                 datetime.now(timezone.utc).isoformat() if status == "filled" else "",
                 symbol, level_idx))
        else:
            self.conn.execute(
                "UPDATE grid_levels SET status=?, order_id=?, amount=?, filled_ts=? "
                "WHERE symbol=? AND level_idx=?",
                (status, order_id, amount,
                 datetime.now(timezone.utc).isoformat() if status == "filled" else "",
                 symbol, level_idx))
        self.conn.commit()

    # ===== 定投 =====
    def get_dca_state(self, symbol: str = None):
        if symbol:
            row = self.conn.execute(
                """SELECT symbol, total_cost, total_qty, avg_price, buy_count, last_buy_ts
                   FROM dca_state WHERE symbol=?""", (symbol,)).fetchone()
            if not row:
                return None
            cols = ["symbol", "total_cost", "total_qty", "avg_price", "buy_count", "last_buy_ts"]
            return dict(zip(cols, row))
        rows = self.conn.execute(
            """SELECT symbol, total_cost, total_qty, avg_price, buy_count, last_buy_ts
               FROM dca_state""").fetchall()
        cols = ["symbol", "total_cost", "total_qty", "avg_price", "buy_count", "last_buy_ts"]
        return [dict(zip(cols, r)) for r in rows]

    def upsert_dca_state(self, symbol: str, **fields):
        self.conn.execute(
            "INSERT INTO dca_state (symbol, updated_at) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO NOTHING",
            (symbol, datetime.now(timezone.utc).isoformat()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE dca_state SET {cols}, updated_at=? WHERE symbol=?",
                (*fields.values(), datetime.now(timezone.utc).isoformat(), symbol))
        self.conn.commit()

    def insert_dca_record(self, r: dict):
        self.conn.execute(
            """INSERT INTO dca_records (ts, symbol, price, amount, cost, fee, dry_run)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (r.get("ts") or datetime.now(timezone.utc).isoformat(), r["symbol"],
             float(r.get("price", 0)), float(r.get("amount", 0)), float(r.get("cost", 0)),
             float(r.get("fee", 0)), 1 if r.get("dry_run") else 0))
        self.conn.commit()

    def get_dca_records(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, ts, symbol, price, amount, cost, fee, dry_run
               FROM dca_records ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        cols = ["id", "ts", "symbol", "price", "amount", "cost", "fee", "dry_run"]
        return [dict(zip(cols, r)) for r in rows]

    # ===== 备份 / 检查点 =====
    def checkpoint(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            log.warning(f"WAL checkpoint failed: {e}")

    def backup_to(self, backup_dir: str, keep: int = 14) -> str | None:
        try:
            os.makedirs(backup_dir, exist_ok=True)
            self.checkpoint()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            path = os.path.join(backup_dir, f"evoclaw-v2-{stamp}.db")
            shutil.copyfile(self.db_path, path)
            os.chmod(path, 0o600)
            backups = sorted(f for f in os.listdir(backup_dir)
                             if f.startswith("evoclaw-v2-") and f.endswith(".db"))
            for old in backups[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, old))
                except OSError:
                    pass
            return path
        except Exception as e:
            log.warning(f"DB backup failed (non-fatal): {e}")
            return None

    def close(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.conn.close()
