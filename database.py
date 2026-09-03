import json
import sqlite3
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/evoclaw.db"):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
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

            CREATE TABLE IF NOT EXISTS liquidations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                orig_qty REAL NOT NULL,
                avg_price REAL NOT NULL,
                executed_qty REAL NOT NULL,
                pnl REAL NOT NULL DEFAULT 0,
                liquidation_time TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_liq_batch ON liquidations(batch_id);
            CREATE INDEX IF NOT EXISTS idx_liq_time ON liquidations(liquidation_time);
            CREATE INDEX IF NOT EXISTS idx_liq_symbol ON liquidations(symbol);
        """)
        self.conn.commit()
        # Delisting ledger — records every proactive close (source='proactive')
        # and forced settlement (source='settled') caused by a coin being
        # delisted / settled on the exchange.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS delistings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                delist_time TEXT NOT NULL DEFAULT '',
                close_time TEXT NOT NULL,
                entry_price REAL NOT NULL DEFAULT 0,
                exit_price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                contract_size REAL NOT NULL DEFAULT 1,
                position_value REAL NOT NULL DEFAULT 0,
                pnl REAL NOT NULL DEFAULT 0,
                pnl_rate REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'proactive',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_delist_time ON delistings(close_time);
            CREATE INDEX IF NOT EXISTS idx_delist_symbol ON delistings(symbol);
            CREATE INDEX IF NOT EXISTS idx_delist_symbol_side ON delistings(symbol, side);
        """)
        self.conn.commit()
        # Migration: add columns if tables existed before
        for table, col, ctype in [
            ("open_positions", "margin_called", "INTEGER DEFAULT 0"),
            ("open_positions", "open_fee", "REAL DEFAULT 0"),
            ("open_positions", "slot_index", "INTEGER DEFAULT -1"),
            ("trades", "fee", "REAL DEFAULT 0"),
            ("open_positions", "tier_executed", "INTEGER DEFAULT -1"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
                self.conn.commit()
            except Exception:
                pass

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS runtime_stats (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_stats (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Open/margin position events — records every open and margin-call.
        # trades table only stores closes; this table enables open-side stats.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS opens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                type TEXT NOT NULL,
                open_time TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_opens_symbol ON opens(symbol);
            CREATE INDEX IF NOT EXISTS idx_opens_side ON opens(side);
            CREATE INDEX IF NOT EXISTS idx_opens_time ON opens(open_time);
        """)
        self.conn.commit()
        self._rebuild_stats()
        self._backfill_open_time()
        self._migrate_web_config()

    def _rebuild_stats(self):
        self.conn.execute("DELETE FROM trade_stats")
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
        self.conn.execute(
            """INSERT INTO trades
               (symbol, side, type, open_time, close_time,
                entry_price, exit_price, amount, pnl, pnl_rate, fee)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade["symbol"], side, ttype,
                trade.get("open_time", ""), trade["close_time"],
                trade["entry_price"], trade["exit_price"],
                trade["amount"], pnl, pnl_rate, fee,
            ),
        )
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
        elif ttype == "delist":
            # Dedicated type — counts toward total/win/loss/fee stats but not the
            # three main close-type counters (it is not a tier/pair/all close).
            pass
        else:
            self._inc_stat("single_count", 1)
        if side == "long":
            self._inc_stat("long_pnl", pnl)
            self._inc_stat("long_count", 1)
        else:
            self._inc_stat("short_pnl", pnl)
            self._inc_stat("short_count", 1)
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
        row = self.conn.execute(
            "SELECT slot_index FROM open_positions WHERE symbol=? AND side=?", (symbol, side)
        ).fetchone()
        return row[0] if row and row[0] >= 0 else None

    def get_used_slots(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT slot_index FROM open_positions WHERE slot_index >= 0"
        ).fetchall()
        return {r[0] for r in rows}

    def _allocate_slot(self, symbol: str, side: str, max_slots: int = 100) -> int | None:
        existing = self.get_slot(symbol, side)
        if existing is not None:
            return existing

        used = self.get_used_slots()

        opposite = "short" if side == "long" else "long"
        opp_slot = self.get_slot(symbol, opposite)
        if opp_slot is not None:
            neighbor = opp_slot + 1 if opp_slot % 2 == 0 else opp_slot - 1
            if 0 <= neighbor < max_slots and neighbor not in used:
                return neighbor

        start = 0 if side == "long" else 1
        for i in range(start, max_slots, 2):
            if i not in used:
                return i

        for i in range(max_slots):
            if i not in used:
                return i

        return None

    def record_open(self, symbol: str, side: str, order_id: str, entry_price: float, amount: float, open_fee: float = None, contract_size: float = 1, max_slots: int = 100):
        now = datetime.now(timezone.utc).isoformat()
        if open_fee is None:
            open_fee = entry_price * amount * contract_size * 0.0005
        slot = self._allocate_slot(symbol, side, max_slots=max_slots)
        self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
        self.conn.execute(
            """INSERT INTO open_positions (symbol, side, order_id, entry_time, entry_price, amount, open_fee, slot_index)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, order_id, now, entry_price, amount, open_fee, slot if slot is not None else -1),
        )
        self.conn.commit()

    def update_open_amount(self, symbol: str, side: str, new_amount: float):
        """Update position amount after partial close."""
        self.conn.execute(
            "UPDATE open_positions SET amount=? WHERE symbol=? AND side=?",
            (new_amount, symbol, side),
        )
        self.conn.commit()

    def remove_open(self, symbol: str, side: str):
        self.conn.execute("DELETE FROM open_positions WHERE symbol=? AND side=?", (symbol, side))
        self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT symbol, side, order_id, entry_time, entry_price, amount, margin_called, open_fee, slot_index FROM open_positions"
        ).fetchall()
        columns = ["symbol", "side", "order_id", "entry_time", "entry_price", "amount", "margin_called", "open_fee", "slot_index"]
        return [dict(zip(columns, r)) for r in rows]

    def mark_margin_called(self, symbol: str, side: str, new_amount: float, added_fee: float = 0):
        self.conn.execute(
            "UPDATE open_positions SET margin_called=1, amount=?, open_fee = open_fee + ? WHERE symbol=? AND side=?",
            (new_amount, added_fee, symbol, side),
        )
        self.conn.commit()

    def has_open(self, symbol: str, side: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM open_positions WHERE symbol=? AND side=?", (symbol, side)
        ).fetchone()
        return row is not None

    def get_open_entry_time(self, symbol: str, side: str) -> str:
        """Get entry_time from open_positions for a symbol+side (may be about to close)."""
        row = self.conn.execute(
            "SELECT entry_time FROM open_positions WHERE symbol=? AND side=?", (symbol, side)
        ).fetchone()
        return row[0] if row else ""

    # ===== Open/Margin event tracking (for open-side stats) =====

    def record_open_event(self, symbol: str, side: str, event_type: str, price: float,
                          amount: float, order_id: str = ''):
        """Record an open ('open') or margin-call ('margin') event."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO opens (symbol, side, type, open_time, price, amount, order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, event_type, now, price, amount, order_id),
        )
        self.conn.commit()

    def get_daily_open_stats(self, day_prefix: str) -> dict:
        """Count open/margin events by side for a given day (YYYY-MM-DD prefix)."""
        rows = self.conn.execute(
            """SELECT side, type, COUNT(*)
               FROM opens WHERE open_time LIKE ?
               GROUP BY side, type""",
            (day_prefix + '%',),
        ).fetchall()
        result = {"open": {"long": 0, "short": 0}, "margin": {"long": 0, "short": 0}}
        for side, etype, cnt in rows:
            if etype in result and side in result[etype]:
                result[etype][side] = cnt
        return result

    def get_symbol_open_summary(self, contract_sizes: dict = None) -> list[dict]:
        """Aggregate open/margin events per symbol+side for the symbol summary table."""
        rows = self.conn.execute(
            """SELECT symbol, side, type, COUNT(*) as cnt,
                      COALESCE(SUM(amount),0) as qty,
                      COALESCE(SUM(price*amount),0) as value
               FROM opens GROUP BY symbol, side, type""",
        ).fetchall()
        grouped = {}
        for symbol, side, etype, cnt, qty, value in rows:
            key = f"{symbol}:{side}"
            g = grouped.setdefault(key, {
                "symbol": symbol, "side": side,
                "open_count": 0, "open_qty": 0.0, "open_value": 0.0,
                "margin_count": 0, "margin_qty": 0.0, "margin_value": 0.0,
            })
            if etype == "open":
                g["open_count"] = cnt
                g["open_qty"] = qty
                g["open_value"] = value
            elif etype == "margin":
                g["margin_count"] = cnt
                g["margin_qty"] = qty
                g["margin_value"] = value
        return list(grouped.values())

    def _backfill_open_time(self):
        """One-time migration: fill empty open_time in trades from open_positions records."""
        empty_count = self.conn.execute("SELECT COUNT(*) FROM trades WHERE open_time='' OR open_time IS NULL").fetchone()[0]
        if empty_count == 0:
            return
        log.info(f"Backfilling {empty_count} trades with empty open_time from open_positions...")
        # For each trade with empty open_time where position still exists, copy entry_time
        self.conn.execute("""
            UPDATE trades SET open_time = (
                SELECT entry_time FROM open_positions
                WHERE open_positions.symbol = trades.symbol
                  AND open_positions.side = trades.side
                  AND open_positions.entry_time != ''
                  AND open_positions.entry_time IS NOT NULL
                LIMIT 1
            )
            WHERE (trades.open_time = '' OR trades.open_time IS NULL)
              AND EXISTS (
                SELECT 1 FROM open_positions
                WHERE open_positions.symbol = trades.symbol
                  AND open_positions.side = trades.side
                  AND open_positions.entry_time != ''
                  AND open_positions.entry_time IS NOT NULL
              )
        """)
        self.conn.commit()
        filled = empty_count - self.conn.execute("SELECT COUNT(*) FROM trades WHERE open_time='' OR open_time IS NULL").fetchone()[0]
        remaining = self.conn.execute("SELECT COUNT(*) FROM trades WHERE open_time='' OR open_time IS NULL").fetchone()[0]
        log.info(f"Backfill complete: filled {filled} trades, {remaining} remain empty (position no longer in DB)")

    # ===== Stats =====

    def get_stats(self, account_balance: float = 0) -> dict:
        rows = self.conn.execute("SELECT key, value FROM trade_stats").fetchall()
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
        row = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0

    def get_historical_worst_trade(self) -> dict | None:
        row = self.conn.execute(
            "SELECT symbol, side, pnl, pnl_rate FROM trades WHERE pnl < 0 ORDER BY pnl ASC LIMIT 1"
        ).fetchone()
        if row:
            return {"symbol": row[0], "side": row[1], "pnl": round(row[2], 4), "pnl_rate": round(row[3], 6)}
        return None

    # ===== Liquidation tracking =====

    def record_liquidation(self, batch_id: str, symbol: str, side: str, orig_qty: float,
                           avg_price: float, executed_qty: float, pnl: float, time_str: str):
        self.conn.execute(
            """INSERT INTO liquidations
               (batch_id, symbol, side, orig_qty, avg_price, executed_qty, pnl, liquidation_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, symbol, side, orig_qty, avg_price, executed_qty, pnl, time_str),
        )
        self.conn.commit()

    def record_liquidations_batch(self, records: list[dict]):
        """Batch insert for startup historical backfill."""
        for r in records:
            self.conn.execute(
                """INSERT OR IGNORE INTO liquidations
                   (batch_id, symbol, side, orig_qty, avg_price, executed_qty, pnl, liquidation_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["batch_id"], r["symbol"], r["side"], r["orig_qty"], r["avg_price"],
                 r["executed_qty"], r["pnl"], r["time"]),
            )
        self.conn.commit()

    def get_liquidation_stats(self) -> dict:
        row = self.conn.execute(
            """SELECT
                 COUNT(DISTINCT batch_id),
                 COALESCE(SUM(pnl), 0),
                 COUNT(DISTINCT symbol),
                 COALESCE(SUM(executed_qty), 0)
               FROM liquidations"""
        ).fetchone()
        return {
            "event_count": row[0] if row else 0,
            "total_pnl": round(row[1], 4) if row else 0,
            "pairs_count": row[2] if row else 0,
            "total_qty": round(row[3], 2) if row else 0,
        }

    def get_liquidation_top10(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT symbol, side, avg_price, orig_qty, pnl, liquidation_time
               FROM liquidations ORDER BY pnl ASC LIMIT 10"""
        ).fetchall()
        return [
            {"symbol": r[0], "side": r[1], "avg_price": round(r[2], 6),
             "orig_qty": round(r[3], 2), "pnl": round(r[4], 4), "liquidation_time": r[5]}
            for r in rows
        ]

    def get_liquidation_events(self, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """Return batched liquidation events + total count."""
        rows = self.conn.execute(
            """SELECT batch_id, liquidation_time,
                      COUNT(*) as pair_count,
                      COALESCE(SUM(pnl), 0) as total_pnl,
                      COALESCE(SUM(executed_qty), 0) as total_qty
               FROM liquidations
               GROUP BY batch_id
               ORDER BY liquidation_time DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = self.conn.execute(
            "SELECT COUNT(DISTINCT batch_id) FROM liquidations"
        ).fetchone()[0]
        events = [
            {"batch_id": r[0], "time": r[1], "pair_count": r[2],
             "total_pnl": round(r[3], 4), "total_qty": round(r[4], 2)}
            for r in rows
        ]
        return events, total if total else 0

    # ===== Delisting records (downlisted / settled coins) =====

    def record_delisting(self, r: dict):
        """Record one delisting close/settlement event."""
        self.conn.execute(
            """INSERT INTO delistings
               (symbol, side, delist_time, close_time, entry_price, exit_price,
                amount, contract_size, position_value, pnl, pnl_rate, fee, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["side"], r.get("delist_time", ""), r["close_time"],
             r["entry_price"], r["exit_price"], r["amount"],
             r.get("contract_size", 1), r["position_value"],
             r["pnl"], r["pnl_rate"], r.get("fee", 0), r.get("source", "proactive")),
        )
        self.conn.commit()

    def get_delisting_summary(self) -> dict:
        """Aggregate stats for the delisting ledger."""
        row = self.conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(pnl), 0),
                      COALESCE(SUM(position_value), 0),
                      COALESCE(SUM(fee), 0),
                      COUNT(DISTINCT symbol),
                      COUNT(CASE WHEN source='proactive' THEN 1 END),
                      COUNT(CASE WHEN source='settled' THEN 1 END)
               FROM delistings"""
        ).fetchone()
        return {
            "record_count": row[0] if row else 0,
            "total_pnl": round(row[1], 4) if row else 0,
            "total_value": round(row[2], 4) if row else 0,
            "total_fee": round(row[3], 4) if row else 0,
            "coin_count": row[4] if row else 0,
            "proactive_count": row[5] if row else 0,
            "settled_count": row[6] if row else 0,
        }

    def get_delistings(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """Return the delisting ledger rows (newest first) + total count."""
        rows = self.conn.execute(
            """SELECT id, symbol, side, delist_time, close_time,
                      entry_price, exit_price, amount, contract_size,
                      position_value, pnl, pnl_rate, fee, source
               FROM delistings ORDER BY id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = self.conn.execute("SELECT COUNT(*) FROM delistings").fetchone()[0]
        columns = [
            "id", "symbol", "side", "delist_time", "close_time",
            "entry_price", "exit_price", "amount", "contract_size",
            "position_value", "pnl", "pnl_rate", "fee", "source",
        ]
        return [dict(zip(columns, r)) for r in rows], total if total else 0

    def has_delisting_recent(self, symbol: str, side: str, since_iso: str) -> bool:
        """True if a delisting record for symbol+side already exists after since_iso
        (used to avoid double-recording a settlement on repeated vanish detections)."""
        row = self.conn.execute(
            "SELECT 1 FROM delistings WHERE symbol=? AND side=? AND close_time >= ? LIMIT 1",
            (symbol, side, since_iso),
        ).fetchone()
        return row is not None

    def sum_trades_pnl(self, symbol: str, side: str, since_iso: str) -> float:
        """Sum of realized PnL already recorded by us for symbol+side since since_iso.
        When reconciling a settlement against Binance userTrades we subtract this
        amount so only the unrecorded (settlement) part is booked."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE symbol=? AND side=? AND close_time >= ?",
            (symbol, side, since_iso),
        ).fetchone()
        return row[0] if row else 0.0

    # ===== App Config (stored in DB, source of truth for ALL settings) =====

    def load_config(self) -> dict:
        """Load full config from DB, converting types from stored strings."""
        rows = self.conn.execute("SELECT key, value FROM config").fetchall()
        config = {}
        for key, raw in rows:
            try:
                config[key] = json.loads(raw)
            except Exception:
                config[key] = raw
        return config

    def save_config(self, config: dict):
        """Replace all config entries with the given dict (full overwrite)."""
        self.conn.execute("DELETE FROM config")
        for key, val in config.items():
            self.conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)",
                (key, json.dumps(val)),
            )
        self.conn.commit()

    def seed_config(self, defaults: dict):
        """Initialize config from defaults if table is empty (first-run migration)."""
        existing = self.conn.execute("SELECT COUNT(*) FROM config").fetchone()[0]
        if existing == 0:
            self.save_config(defaults)
            return True
        return False

    def upsert_config_key(self, key: str, value):
        """Insert or update a single config key without affecting others.

        Phase 4: Used to migrate sensitive keys (exchange_kwargs) into DB
        without a full config overwrite.
        """
        self.conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def load_web_config(self) -> dict:
        """Load frontend display config (matrix_slots/matrix_columns) from DB."""
        slots = self.get_runtime_stat("matrix_slots", 100)
        cols = self.get_runtime_stat("matrix_columns", 10)
        return {"matrix_slots": int(slots), "matrix_columns": int(cols)}

    def save_web_config(self, cfg: dict):
        """Save frontend display config to DB runtime_stats."""
        if "matrix_slots" in cfg:
            self.set_runtime_stat("matrix_slots", float(cfg["matrix_slots"]))
        if "matrix_columns" in cfg:
            self.set_runtime_stat("matrix_columns", float(cfg["matrix_columns"]))

    def _migrate_web_config(self):
        """One-time migration: read web/config.json and store in DB, then delete the file."""
        import os
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "config.json")
        if not os.path.exists(file_path):
            return
        try:
            import json
            with open(file_path) as f:
                wc = json.load(f)
            slots = int(wc.get("matrix_slots", 100))
            cols = int(wc.get("matrix_columns", 10))
            # Only write if not already in DB
            db_slots = int(self.get_runtime_stat("matrix_slots", 0))
            if db_slots <= 0:
                self.set_runtime_stat("matrix_slots", float(slots))
                self.set_runtime_stat("matrix_columns", float(cols))
                log.info(f"Migrated web config from file: matrix_slots={slots}, matrix_columns={cols}")
            os.remove(file_path)
            log.info(f"Removed {file_path}")
        except Exception as e:
            log.warning(f"Web config migration failed (non-fatal): {e}")

    # ===== End App Config =====

    def set_tier_executed(self, symbol: str, side: str, tier: int):
        """Persist tier close state so it survives restarts."""
        self.conn.execute(
            "UPDATE open_positions SET tier_executed=? WHERE symbol=? AND side=?",
            (tier, symbol, side),
        )
        self.conn.commit()

    def load_tier_states(self) -> dict:
        """Load persisted tier close states (key -> tier_index)."""
        rows = self.conn.execute(
            "SELECT symbol, side, tier_executed FROM open_positions WHERE tier_executed >= 0"
        ).fetchall()
        return {f"{r[0]}:{r[1]}": r[2] for r in rows}

    def increment_margin_call_count(self, increment: int = 1):
        self.conn.execute(
            "INSERT INTO runtime_stats (key, value, updated_at) VALUES ('margin_call_count', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value, updated_at=excluded.updated_at",
            (increment, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    # ===== Runtime stats =====

    def get_runtime_stat(self, key: str, default: float = 0) -> float:
        row = self.conn.execute(
            "SELECT value FROM runtime_stats WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_runtime_stat(self, key: str, value: float):
        self.conn.execute(
            "INSERT INTO runtime_stats (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def checkpoint(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            log.warning(f"WAL checkpoint failed: {e}")

    def checkpoint_restart(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            log.warning(f"WAL checkpoint failed: {e}")

    def close(self):
        self.conn.close()
