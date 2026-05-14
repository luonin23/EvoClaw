import sqlite3
import os
import threading


class Database:
    def __init__(self, db_path: str = "data/evoclaw.db"):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                type TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                amount REAL NOT NULL,
                pnl REAL NOT NULL,
                pnl_rate REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(close_time);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

            -- Track system-managed open positions
            CREATE TABLE IF NOT EXISTS open_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_id TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                amount REAL NOT NULL,
                margin_called INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_open_pos_symbol_side ON open_positions(symbol, side);
        """)
        self.conn.commit()
        # Migration: add columns if tables existed before
        for table, col, ctype in [
            ("open_positions", "margin_called", "INTEGER DEFAULT 0"),
            ("open_positions", "open_fee", "REAL DEFAULT 0"),
            ("trades", "fee", "REAL DEFAULT 0"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
                self.conn.commit()
            except Exception:
                pass  # column already exists

        # Runtime stats table (for persistent runtime metrics)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS runtime_stats (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def insert_trade(self, trade: dict):
        with self.lock:
            self.conn.execute(
                """INSERT INTO trades
                   (symbol, side, type, open_time, close_time,
                    entry_price, exit_price, amount, pnl, pnl_rate, fee)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["symbol"],
                    trade["side"],
                    trade.get("type", "single"),
                    trade.get("open_time", ""),
                    trade["close_time"],
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["amount"],
                    trade["pnl"],
                    trade["pnl_rate"],
                    trade.get("fee", 0),
                ),
            )
            self.conn.commit()

    # ===== Open positions tracking =====

    def record_open(self, symbol: str, side: str, order_id: str, entry_price: float, amount: float, open_fee: float = None):
        """Record a system-opened position."""
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        if open_fee is None:
            open_fee = entry_price * amount * 0.0005
        with self.lock:
            # Remove any stale entry for this symbol+side
            self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
            self.conn.execute(
                """INSERT INTO open_positions (symbol, side, order_id, entry_time, entry_price, amount, open_fee)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (symbol, side, order_id, now, entry_price, amount, open_fee),
            )
            self.conn.commit()

    def remove_open(self, symbol: str, side: str):
        """Remove a closed position from tracking."""
        with self.lock:
            self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
            self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        """Get all system-tracked open positions."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT symbol, side, order_id, entry_time, entry_price, amount, margin_called, open_fee FROM open_positions"
            ).fetchall()
        columns = ["symbol", "side", "order_id", "entry_time", "entry_price", "amount", "margin_called", "open_fee"]
        return [dict(zip(columns, r)) for r in rows]

    def mark_margin_called(self, symbol: str, side: str, new_amount: float, added_fee: float = 0):
        """Mark position as margin-called and update amount."""
        with self.lock:
            self.conn.execute(
                "UPDATE open_positions SET margin_called=1, amount=?, open_fee = open_fee + ? WHERE symbol=? AND side=?",
                (new_amount, added_fee, symbol, side),
            )
            self.conn.commit()

    def has_open(self, symbol: str, side: str) -> bool:
        """Check if system has a tracked open position for symbol+side."""
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM open_positions WHERE symbol=? AND side=?", (symbol, side)
            ).fetchone()
            return row is not None

    # ===== Stats =====

    def get_stats(self, account_balance: float = 0) -> dict:
        with self.lock:
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
                    COALESCE(SUM(CASE WHEN side = 'long' THEN pnl END), 0) AS long_pnl,
                    COALESCE(AVG(CASE WHEN side = 'long' THEN pnl_rate END), 0) AS long_pnl_rate,
                    COALESCE(SUM(CASE WHEN side = 'short' THEN pnl END), 0) AS short_pnl,
                    COALESCE(AVG(CASE WHEN side = 'short' THEN pnl_rate END), 0) AS short_pnl_rate
                FROM trades
            """).fetchone()

        total_pnl = row[0]
        total_fee = row[1]
        account_profit_rate = total_pnl / account_balance if account_balance > 0 else 0

        return {
            "total_pnl": round(total_pnl, 4),
            "total_fee": round(total_fee, 4),
            "max_single_profit_rate": round(row[2], 6),
            "account_profit_rate": round(account_profit_rate, 6),
            "win_rate": round(row[3], 4),
            "long_count": row[4],
            "short_count": row[5],
            "total_count": row[6],
            "all_close_count": row[7],
            "pair_close_count": row[8],
            "single_count": row[9],
            "long_pnl": round(row[10], 4),
            "long_pnl_rate": round(row[11], 6),
            "short_pnl": round(row[12], 4),
            "short_pnl_rate": round(row[13], 6),
        }

    def get_recent_trades(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, symbol, side, type, open_time, close_time,
                          entry_price, exit_price, amount, pnl, pnl_rate, fee
                   FROM trades ORDER BY id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()

        columns = [
            "id", "symbol", "side", "type", "open_time", "close_time",
            "entry_price", "exit_price", "amount", "pnl", "pnl_rate", "fee",
        ]
        return [dict(zip(columns, r)) for r in rows]

    def get_total_trades(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0

    def increment_margin_call_count(self, increment: int = 1):
        with self.lock:
            self.conn.execute(
                "INSERT INTO runtime_stats (key, value, updated_at) VALUES ('margin_call_count', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value, updated_at=excluded.updated_at",
                (increment, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
            )
            self.conn.commit()

    # ===== Runtime stats =====

    def get_runtime_stat(self, key: str, default: float = 0) -> float:
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM runtime_stats WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_runtime_stat(self, key: str, value: float):
        with self.lock:
            self.conn.execute(
                "INSERT INTO runtime_stats (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
            )
            self.conn.commit()

    def close(self):
        self.conn.close()
