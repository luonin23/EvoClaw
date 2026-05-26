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
            ("open_positions", "slot_index", "INTEGER DEFAULT -1"),
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
        # Incremental trade stats table (O(1) lookup, rebuilt from trades on init)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_stats (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0
            )
        """)
        self.conn.commit()
        # Ensure stats are consistent with existing trades
        self._rebuild_stats()

    def _rebuild_stats(self):
        """Rebuild trade_stats from trades table. Called once at init."""
        with self.lock:
            # Clear existing stats
            self.conn.execute("DELETE FROM trade_stats")
            # Aggregate from trades
            row = self.conn.execute("""
                SELECT
                    COALESCE(SUM(pnl), 0),
                    COALESCE(SUM(fee), 0),
                    COALESCE(MAX(pnl_rate), 0),
                    COUNT(CASE WHEN pnl > 0 THEN 1 END),
                    COUNT(CASE WHEN pnl <= 0 THEN 1 END),
                    COUNT(*),
                    COUNT(CASE WHEN type = 'all_close' THEN 1 END),
                    COUNT(CASE WHEN type = 'pair_close' THEN 1 END),
                    COUNT(CASE WHEN type = 'single' THEN 1 END),
                    COALESCE(SUM(CASE WHEN side = 'long' THEN pnl END), 0),
                    COUNT(CASE WHEN side = 'long' THEN 1 END),
                    COALESCE(SUM(CASE WHEN side = 'short' THEN pnl END), 0),
                    COUNT(CASE WHEN side = 'short' THEN 1 END)
                FROM trades
            """).fetchone()
            keys = [
                "total_pnl", "total_fee", "max_pnl_rate",
                "win_count", "loss_count", "total_count",
                "all_close_count", "pair_close_count", "single_count",
                "long_pnl", "long_count", "short_pnl", "short_count"
            ]
            for k, v in zip(keys, row):
                self.conn.execute(
                    "INSERT INTO trade_stats (key, value) VALUES (?, ?)",
                    (k, v)
                )
            self.conn.commit()

    def _inc_stat(self, key: str, delta: float):
        """Atomically increment a trade_stats value."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO trade_stats (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (key, delta)
            )

    def insert_trade(self, trade: dict):
        pnl = trade["pnl"]
        fee = trade.get("fee", 0)
        pnl_rate = trade["pnl_rate"]
        side = trade["side"]
        ttype = trade.get("type", "single")
        with self.lock:
            self.conn.execute(
                """INSERT INTO trades
                   (symbol, side, type, open_time, close_time,
                    entry_price, exit_price, amount, pnl, pnl_rate, fee)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["symbol"],
                    side,
                    ttype,
                    trade.get("open_time", ""),
                    trade["close_time"],
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["amount"],
                    pnl,
                    pnl_rate,
                    fee,
                ),
            )
            # Incremental stat updates (O(1) instead of full table scan later)
            self._inc_stat("total_pnl", pnl)
            self._inc_stat("total_fee", fee)
            self._inc_stat("total_count", 1)
            if pnl > 0:
                self._inc_stat("win_count", 1)
            else:
                self._inc_stat("loss_count", 1)
            if ttype == "all_close":
                self._inc_stat("all_close_count", 1)
            elif ttype == "pair_close":
                self._inc_stat("pair_close_count", 1)
            else:
                self._inc_stat("single_count", 1)
            if side == "long":
                self._inc_stat("long_pnl", pnl)
                self._inc_stat("long_count", 1)
            else:
                self._inc_stat("short_pnl", pnl)
                self._inc_stat("short_count", 1)
            # Update max_pnl_rate if this trade sets a new record
            cur_max = self.conn.execute("SELECT value FROM trade_stats WHERE key='max_pnl_rate'").fetchone()
            if cur_max is None or pnl_rate > cur_max[0]:
                self.conn.execute(
                    "INSERT INTO trade_stats (key, value) VALUES ('max_pnl_rate', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (pnl_rate,)
                )
            self.conn.commit()

    # ===== Open positions tracking =====

    def get_slot(self, symbol: str, side: str) -> int | None:
        """Get assigned slot index for a symbol+side. Returns None if not assigned."""
        with self.lock:
            row = self.conn.execute(
                "SELECT slot_index FROM open_positions WHERE symbol=? AND side=?", (symbol, side)
            ).fetchone()
        return row[0] if row and row[0] >= 0 else None

    def get_used_slots(self) -> set[int]:
        """Get all currently assigned slot indices."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT slot_index FROM open_positions WHERE slot_index >= 0"
            ).fetchall()
        return {r[0] for r in rows}

    def _allocate_slot(self, symbol: str, side: str) -> int | None:
        """Allocate a slot index (0-99) for symbol+side.
        Same-symbol long/short are placed adjacent when possible.
        Returns None if no slot available.
        """
        # Check if already allocated
        existing = self.get_slot(symbol, side)
        if existing is not None:
            return existing

        used = self.get_used_slots()

        # Try to place adjacent to opposite side of same symbol
        opposite = "short" if side == "long" else "long"
        opp_slot = self.get_slot(symbol, opposite)
        if opp_slot is not None:
            neighbor = opp_slot + 1 if opp_slot % 2 == 0 else opp_slot - 1
            if 0 <= neighbor < 100 and neighbor not in used:
                return neighbor

        # Find smallest slot of preferred parity (even for long, odd for short)
        start = 0 if side == "long" else 1
        for i in range(start, 100, 2):
            if i not in used:
                return i

        # Fallback: any available slot
        for i in range(100):
            if i not in used:
                return i

        return None

    def record_open(self, symbol: str, side: str, order_id: str, entry_price: float, amount: float, open_fee: float = None):
        """Record a system-opened position. Auto-allocates slot_index."""
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        if open_fee is None:
            open_fee = entry_price * amount * 0.0005
        slot = self._allocate_slot(symbol, side)
        with self.lock:
            # Remove any stale entry for this symbol+side
            self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
            self.conn.execute(
                """INSERT INTO open_positions (symbol, side, order_id, entry_time, entry_price, amount, open_fee, slot_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, side, order_id, now, entry_price, amount, open_fee, slot if slot is not None else -1),
            )
            self.conn.commit()

    def remove_open(self, symbol: str, side: str):
        """Remove a closed position from tracking. Slot is implicitly released by DELETE."""
        with self.lock:
            self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
            self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        """Get all system-tracked open positions."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT symbol, side, order_id, entry_time, entry_price, amount, margin_called, open_fee, slot_index FROM open_positions"
            ).fetchall()
        columns = ["symbol", "side", "order_id", "entry_time", "entry_price", "amount", "margin_called", "open_fee", "slot_index"]
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
            rows = self.conn.execute(
                "SELECT key, value FROM trade_stats"
            ).fetchall()
        stats = {k: v for k, v in rows}
        total_pnl = stats.get("total_pnl", 0)
        total_fee = stats.get("total_fee", 0)
        total_count = int(stats.get("total_count", 0))
        win_count = stats.get("win_count", 0)
        long_count = int(stats.get("long_count", 0))
        short_count = int(stats.get("short_count", 0))
        account_profit_rate = total_pnl / account_balance if account_balance > 0 else 0
        win_rate = win_count / total_count if total_count > 0 else 0
        long_pnl_rate = stats.get("long_pnl", 0) / long_count if long_count > 0 else 0
        short_pnl_rate = stats.get("short_pnl", 0) / short_count if short_count > 0 else 0

        return {
            "total_pnl": round(total_pnl, 4),
            "total_fee": round(total_fee, 4),
            "max_single_profit_rate": round(stats.get("max_pnl_rate", 0), 6),
            "account_profit_rate": round(account_profit_rate, 6),
            "win_rate": round(win_rate, 4),
            "long_count": long_count,
            "short_count": short_count,
            "total_count": total_count,
            "all_close_count": int(stats.get("all_close_count", 0)),
            "pair_close_count": int(stats.get("pair_close_count", 0)),
            "single_count": int(stats.get("single_count", 0)),
            "long_pnl": round(stats.get("long_pnl", 0), 4),
            "long_pnl_rate": round(long_pnl_rate, 6),
            "short_pnl": round(stats.get("short_pnl", 0), 4),
            "short_pnl_rate": round(short_pnl_rate, 6),
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

    def get_historical_worst_trade(self) -> dict | None:
        """Return the worst (most negative) single trade from history."""
        with self.lock:
            row = self.conn.execute(
                "SELECT symbol, side, pnl, pnl_rate FROM trades WHERE pnl < 0 ORDER BY pnl ASC LIMIT 1"
            ).fetchone()
        if row:
            return {"symbol": row[0], "side": row[1], "pnl": round(row[2], 4), "pnl_rate": round(row[3], 6)}
        return None

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

    def checkpoint(self):
        """Checkpoint WAL to prevent unbounded growth."""
        try:
            with self.lock:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            __import__("logging").getLogger(__name__).warning(f"WAL checkpoint failed: {e}")

    def checkpoint_restart(self):
        """Force WAL checkpoint (PASSIVE) to reduce WAL size. PASSIVE never blocks or deadlocks."""
        try:
            with self.lock:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            __import__("logging").getLogger(__name__).warning(f"WAL checkpoint failed: {e}")

    def close(self):
        self.conn.close()
