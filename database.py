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
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def _iso_window(ts_iso: str, seconds: int) -> tuple[str, str]:
    """把 ISO 时间戳扩成一个 ±seconds 的 ISO 窗口（用于同表时间邻近查询）。"""
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return ((dt - timedelta(seconds=seconds)).isoformat(),
            (dt + timedelta(seconds=seconds)).isoformat())


class Database:
    def __init__(self, db_path: str = "data/evoclaw-v2.db"):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._wal = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        # 单连接会被事件循环线程与 to_thread 线程池同时使用，必须有锁保护
        # 多语句事务（sqlite3 只保证单条语句原子）。
        self._lk = threading.RLock()
        self._init_tables()
        # 所有复合写方法统一加锁
        for _name in (
            "save_config", "seed_config", "set_runtime_stat", "insert_order",
            "save_config_audited", "config_rollback",
            "update_order_status", "upsert_grid_state", "replace_grid_levels",
            "insert_grid_level", "fill_grid_level", "clear_grid_order_id", "mark_grid_level",
            "add_lot", "consume_lots", "reset_lots", "clear_lots",
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
                note TEXT NOT NULL DEFAULT '',
                pnl REAL NOT NULL DEFAULT 0,
                pnl_rate REAL NOT NULL DEFAULT 0
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
                status TEXT NOT NULL DEFAULT 'active', -- active=在选清单 / draining=已掉出,只卖不买
                realized_pnl REAL NOT NULL DEFAULT 0,
                buy_count INTEGER NOT NULL DEFAULT 0,
                sell_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- 网格逐笔持仓账本：每一笔买入单独记一行，卖出按价格配对核销。
            -- 开仓/加仓/平仓来回多次时，每笔卖出的盈亏都能追溯到它对应的
            -- 那笔买入；且「卖价 > 该笔买入价」→ 单笔必然盈利。
            CREATE TABLE IF NOT EXISTS grid_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts TEXT NOT NULL,
                price REAL NOT NULL,              -- 该笔买入成交价（成本）
                qty REAL NOT NULL,                -- 原始数量
                remaining REAL NOT NULL,          -- 剩余未卖出数量
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_grid_lots_sym ON grid_lots(symbol, remaining);

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
            CREATE TABLE IF NOT EXISTS config_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                config TEXT NOT NULL,      -- 改动**之前**的完整 config（JSON）
                changed TEXT NOT NULL,     -- 本次改了哪些键，{key: [旧值, 新值]}
                source TEXT NOT NULL DEFAULT ''
            );
            -- 现货挂单：限价单挂出去后要跨 tick 跟踪成交，不能只记在内存里
            CREATE TABLE IF NOT EXISTS spot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0,
                cost_usdt REAL NOT NULL DEFAULT 0,
                order_id TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                profit_share REAL NOT NULL DEFAULT 0,  -- 该单对应的利润份额（成交后推进水位线）
                status TEXT NOT NULL DEFAULT 'pending',   -- pending|filled|cancelled
                filled_ts TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS seen_trades (
                symbol TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (symbol, trade_id)
            );
            CREATE TABLE IF NOT EXISTS dca_state (
                symbol TEXT PRIMARY KEY,
                total_cost REAL NOT NULL DEFAULT 0,
                total_qty REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                buy_count INTEGER NOT NULL DEFAULT 0,
                last_buy_ts TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            -- 强平记录：来自币安 forceOrders（逐仓/全仓被强平的每一笔平仓单）。
            -- order_id 唯一 → 重复同步天然幂等。
            CREATE TABLE IF NOT EXISTS liquidations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL UNIQUE,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT '',
                position_side TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                notional REAL NOT NULL DEFAULT 0,
                margin_mode TEXT NOT NULL DEFAULT '',
                pnl REAL NOT NULL DEFAULT 0,   -- 交易所收入流水汇总（含手续费）
                raw TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_liquidations_ts ON liquidations(ts);
        """)
        self.conn.commit()
        # 迁移：为已存在的表补列（已存在则忽略）
        for _tbl, _col, _type in [("grid_state", "total_cost", "REAL NOT NULL DEFAULT 0"),
                                  ("grid_state", "status", "TEXT NOT NULL DEFAULT 'active'"),
                                  ("liquidations", "pnl", "REAL NOT NULL DEFAULT 0"),
                                  ("orders", "liq_source", "TEXT NOT NULL DEFAULT ''"),
                                  ("orders", "pnl", "REAL NOT NULL DEFAULT 0"),
                                  ("orders", "pnl_rate", "REAL NOT NULL DEFAULT 0"),
                                  ("dca_records", "note", "TEXT NOT NULL DEFAULT ''"),
                                  ("spot_orders", "profit_share", "REAL NOT NULL DEFAULT 0")]:
            try:
                self.conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_type}")
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

    # ===== 现货挂单 =====
    def insert_spot_order(self, o: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO spot_orders (ts, symbol, side, amount, price, cost_usdt,
                                        order_id, purpose, profit_share, status, filled_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (o.get("ts") or datetime.now(timezone.utc).isoformat(), o["symbol"], o["side"],
             float(o.get("amount", 0)), float(o.get("price", 0)),
             float(o.get("cost_usdt", 0)), str(o.get("order_id", "")),
             o.get("purpose", ""), float(o.get("profit_share", 0)),
             o.get("status", "pending"), o.get("filled_ts", "")))
        self.conn.commit()
        return int(cur.lastrowid)

    def filled_spot_orders(self, purpose: str, symbol: str = "") -> list[dict]:
        """某个用途已成交的现货单（马丁格尔的持仓/均价/档位全部由它推导）。"""
        q = "SELECT * FROM spot_orders WHERE status='filled' AND purpose=?"
        args: list = [purpose]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        rows = self.conn.execute(q + " ORDER BY id", args).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM spot_orders LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def get_spot_order(self, row_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM spot_orders WHERE id=?",
                                (int(row_id),)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM spot_orders LIMIT 0").description]
        return dict(zip(cols, row))

    def pending_spot_orders(self, purpose: str = "") -> list[dict]:
        if purpose:
            rows = self.conn.execute(
                "SELECT * FROM spot_orders WHERE status='pending' AND purpose=? ORDER BY id",
                (purpose,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM spot_orders WHERE status='pending' ORDER BY id").fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM spot_orders LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def update_spot_order(self, row_id: int, status: str, filled_ts: str = ""):
        self.conn.execute(
            "UPDATE spot_orders SET status=?, filled_ts=? WHERE id=?",
            (status, filled_ts, int(row_id)))
        self.conn.commit()

    # ===== 强平记录 =====
    def insert_liquidation(self, r: dict) -> bool:
        """写入一条强平记录；按交易所 order_id 幂等。返回是否新增。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO liquidations
               (order_id, ts, symbol, side, position_side, price, amount, notional,
                margin_mode, pnl, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(r["order_id"]), r["ts"], r["symbol"], r.get("side", ""),
             r.get("position_side", ""), float(r.get("price", 0)),
             float(r.get("amount", 0)), float(r.get("notional", 0)),
             r.get("margin_mode", ""), float(r.get("pnl", 0)), r.get("raw", "")))
        self.conn.commit()
        return cur.rowcount > 0

    def update_liquidation_pnl(self, order_id: str, pnl: float):
        self.conn.execute("UPDATE liquidations SET pnl=? WHERE order_id=?",
                          (float(pnl), str(order_id)))
        self.conn.commit()

    def update_liquidation_order_pnl(self, symbol: str, ts_iso: str, pnl: float,
                                     seconds: int = 600):
        """把监控先插入的简版强平记录的盈亏补上（只改 pnl 仍为 0 的行，
        绝不覆盖对账路径的权威盈亏）。"""
        lo, hi = _iso_window(ts_iso, seconds)
        # pnl_rate 固定 0：强平盈亏是按「标的整仓」汇总的，与单张强平单的名义
        # 不成比例，算出来会是 -857% 这种误导值。前端对强平行显示「—」。
        self.conn.execute(
            """UPDATE orders SET
                   pnl = CASE WHEN pnl = 0 THEN ? ELSE pnl END,
                   pnl_rate = 0
               WHERE symbol=? AND type='liquidation' AND ts BETWEEN ? AND ?""",
            (float(pnl), symbol, lo, hi))
        self.conn.commit()

    def first_order_ts(self) -> str:
        """v2 的第一笔（非强平）订单时间 —— 区分「历史强平/运行中强平」的分界。"""
        row = self.conn.execute(
            "SELECT MIN(ts) FROM orders WHERE type != 'liquidation'").fetchone()
        return row[0] or ""

    def claim_external_close(self, symbol: str, ts_iso: str, seconds: int = 600) -> int:
        """找同时段、同标的的「外部平仓」行（对账先记、强平监控后到）。

        返回行 id；没有返回 0。
        """
        lo, hi = _iso_window(ts_iso, seconds)
        row = self.conn.execute(
            """SELECT id FROM orders WHERE symbol=? AND type='grid_external'
               AND ts BETWEEN ? AND ? ORDER BY id LIMIT 1""",
            (symbol, lo, hi)).fetchone()
        return int(row[0]) if row else 0

    def upgrade_to_liquidation(self, row_id: int, liq: dict, pnl: float,
                               source: str):
        """把对账先记的「外部平仓」行升级为「强制平仓」（交易所权威数据为准）。"""
        self.conn.execute(
            """UPDATE orders SET type='liquidation', ts=?, price=?, amount=?, cost=?,
                      pnl=?, pnl_rate=0, note=?, order_id=?, liq_source=?
               WHERE id=?""",
            (liq["ts"], float(liq.get("price", 0)), float(liq.get("amount", 0)),
             float(liq.get("notional", 0)), float(pnl),
             f"强制平仓（{liq.get('position_side') or '仓位'}）",
             str(liq.get("order_id", "")), source, int(row_id)))
        self.conn.commit()

    def dedup_external_vs_liquidation(self, seconds: int = 600) -> int:
        """清理历史双记：同一标的 ±窗口内的 grid_external 与 liquidation 是
        同一次强平（对账路径先记 grid_external，监控同步后又插 liquidation）
        → 删掉 grid_external 行。返回删除数。"""
        ext = self.conn.execute(
            "SELECT id, symbol, ts FROM orders WHERE type='grid_external'").fetchall()
        liqs = self.conn.execute(
            "SELECT symbol, ts FROM liquidations").fetchall()
        from datetime import datetime as _dt
        def _t(s):
            try:
                d = _dt.fromisoformat(str(s).replace("Z", "+00:00"))
                return d.timestamp() if d.tzinfo else d.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                return None
        liq_times = [(s, _t(ts)) for s, ts in liqs]
        removed = 0
        for oid, sym, ts in ext:
            t = _t(ts)
            if t is None:
                continue
            if any(s == sym and lt is not None and abs(t - lt) <= seconds
                   for s, lt in liq_times):
                self.conn.execute("DELETE FROM orders WHERE id=?", (int(oid),))
                removed += 1
        if removed:
            self.conn.commit()
        return removed

    def normalize_liquidation_rates(self):
        """历史强平行的盈亏率一律清零（该比率对强平没有意义）；
        并把 v2 首笔订单之前的强平标记为 backfill（不进策略统计）。"""
        self.conn.execute(
            "UPDATE orders SET pnl_rate=0 WHERE type='liquidation' AND pnl_rate != 0")
        fts = self.first_order_ts()
        if fts:
            self.conn.execute(
                """UPDATE orders SET liq_source='backfill'
                   WHERE type='liquidation' AND liq_source='' AND ts < ?""",
                (fts,))
        self.conn.commit()

    def get_liquidations(self, limit: int = 100, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, order_id, ts, symbol, side, position_side, price, amount,
                      notional, margin_mode, pnl
               FROM liquidations ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            (int(limit), int(offset))).fetchall()
        cols = ["id", "order_id", "ts", "symbol", "side", "position_side", "price",
                "amount", "notional", "margin_mode", "pnl"]
        return [dict(zip(cols, r)) for r in rows]

    def liquidation_stats(self) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*), COUNT(DISTINCT symbol), COALESCE(SUM(notional), 0),
                      COALESCE(MIN(ts), ''), COALESCE(MAX(ts), ''), COUNT(DISTINCT ts),
                      COALESCE(SUM(pnl), 0)
               FROM liquidations""").fetchone()
        return {"count": int(row[0] or 0), "symbols": int(row[1] or 0),
                "total_notional": round(float(row[2] or 0), 4),
                "first_ts": row[3] or "", "last_ts": row[4] or "",
                # 同一次强平会在一秒内批量产生多张强平单（实测一次 91 张）
                "events": int(row[5] or 0),
                "total_pnl": round(float(row[6] or 0), 4)}

    def liquidation_near(self, symbol: str, ts_iso: str, seconds: int = 600) -> dict | None:
        """找同一标的、时间相近的强平记录（用于把「外部平仓」识别成强平）。"""
        lo, hi = _iso_window(ts_iso, seconds)
        row = self.conn.execute(
            """SELECT id, order_id, ts, symbol, side, position_side, price, amount,
                      notional, margin_mode, pnl
               FROM liquidations WHERE symbol=? AND ts BETWEEN ? AND ?
               ORDER BY ts LIMIT 1""", (symbol, lo, hi)).fetchone()
        if not row:
            return None
        cols = ["id", "order_id", "ts", "symbol", "side", "position_side", "price",
                "amount", "notional", "margin_mode", "pnl"]
        return dict(zip(cols, row))

    def has_order_near(self, symbol: str, ts_iso: str, seconds: int = 600) -> bool:
        """同一标的时间窗内是否已有成交记录（避免强平记录重复插入 orders）。"""
        lo, hi = _iso_window(ts_iso, seconds)
        row = self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE symbol=? AND ts BETWEEN ? AND ?",
            (symbol, lo, hi)).fetchone()
        return int(row[0] or 0) > 0

    def record_liquidation_order(self, o: dict, seconds: int = 600,
                                 overwrite: bool = False,
                                 source: str = "monitor") -> bool:
        """把一笔强平写进成交记录；同一标的时间窗内只保留一行。

        overwrite=True 用于**对账补记**：本地有完整账本（数量/成本/盈亏），
        以它为准覆盖监控先行插入的简版记录。
        overwrite=False 用于强平监控：已有记录就跳过，绝不覆盖对账数据。
        source：'backfill'=90 天历史回补（v2 上线前的事件，只展示、不进统计）；
                'monitor'=运行中监控；'reconcile'=对账发现。
        返回 True 表示新插入。
        """
        lo, hi = _iso_window(o["ts"], seconds)
        row = self.conn.execute(
            """SELECT id FROM orders
               WHERE symbol=? AND type='liquidation' AND ts BETWEEN ? AND ?
               ORDER BY id LIMIT 1""", (o["symbol"], lo, hi)).fetchone()
        if row:
            if overwrite:
                self.conn.execute(
                    """UPDATE orders SET mode=?, side=?, price=?, amount=?, cost=?,
                              fee=?, pnl=?, pnl_rate=?, note=?, order_id=?,
                              dry_run=?, status='filled', liq_source=?
                       WHERE id=?""",
                    (o.get("mode", "grid"), o["side"], float(o.get("price", 0)),
                     float(o.get("amount", 0)), float(o.get("cost", 0)),
                     float(o.get("fee", 0)), float(o.get("pnl", 0)),
                     float(o.get("pnl_rate", 0)), o.get("note", "强制平仓"),
                     str(o.get("order_id", "")), 1 if o.get("dry_run") else 0,
                     source, int(row[0])))
                self.conn.commit()
            return False
        self.insert_order({**o, "mode": o.get("mode", "grid"),
                           "type": "liquidation", "liq_source": source})
        return True

    def realized_pnl_total(self) -> float:
        """合约累计已实现盈亏（所有平仓类记录之和）。

        利润定投的触发依据 —— 用 orders 表而不是 grid_state.realized_pnl，
        因为后者在这套部署里基本是 0（历史遗留），而 orders 才是成交记录的真源，
        与页面上的「总盈亏」同源。
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM orders "
            "WHERE type IN ('grid_sell','grid_trail','grid_external','short_close','close')"
        ).fetchone()
        return float(row[0] or 0)

    def dca_round_stats(self) -> tuple[int, float]:
        """现货马丁格尔的历史轮数与累计已实现盈亏（只统计现货网格的卖出，
        绝不混进 realized_pnl_total —— 那是利润定投的水位线口径，只认合约利润）。"""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM orders WHERE type='dca_sell'"
        ).fetchone()
        return int(row[0] or 0), float(row[1] or 0)

    def dca_invested_total(self, note: str = "") -> float:
        """定投累计投入的 USDT（按 note **精确**过滤，例如只看「利润定投」那部分）。

        不用 LIKE：note 是精确写入的常量（"利润定投"/"定期定投"/""），
        模糊匹配会被「碰巧包含同样字样」的其他记录污染上限统计。
        """
        if note:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM dca_records WHERE note = ?",
                (note,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM dca_records").fetchone()
        return float(row[0] or 0)

    # ===== 配置改动历史 / 回滚 / 审计 =====
    @staticmethod
    def _mask_sensitive(cfg: dict) -> dict:
        """历史快照里**绝不存密钥明文** —— 否则等于把 API Key 复制 20 份。

        代价是「回滚不再还原密钥」：这是刻意为之 —— 回滚到一份带着**旧密钥**的
        配置，轻则下单全挂，重则把可能已作废的 key 写回去。回滚永远保留
        当前正在用的密钥（见 config_rollback）。
        """
        out = dict(cfg)
        kw = dict(out.get("exchange_kwargs") or {})
        if kw:
            out["exchange_kwargs"] = {
                "apiKey": (kw.get("apiKey", "")[:6] + "…") if kw.get("apiKey") else "",
                "secret": "***" if kw.get("secret") else "",
            }
        if out.get("web_password"):
            out["web_password"] = "***"
        return out

    def save_config_audited(self, new_cfg: dict, changed: dict, source: str = "",
                            keep: int = 20) -> None:
        """写入配置，并留一份「改动前」的完整快照，供回滚与审计。

        与 save_config 分开是刻意的：`_refresh_selection` 每 10 分钟就会
        save_config 一次（写选币结果），那种内部写入不该污染历史。
        只有**用户改配置**这条路径才走这里。快照中的密钥/密码做掩码
        （见 _mask_sensitive）。
        """
        prev = self._mask_sensitive(self.load_config())
        self.conn.execute(
            "INSERT INTO config_history (ts, config, changed, source) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(),
             json.dumps(prev, ensure_ascii=False),
             json.dumps(changed, ensure_ascii=False), source))
        self.conn.execute(
            "DELETE FROM config_history WHERE id NOT IN "
            "(SELECT id FROM config_history ORDER BY id DESC LIMIT ?)", (keep,))
        self.conn.commit()
        self.save_config(new_cfg)

    def config_history(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ts, changed, source FROM config_history ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "changed": json.loads(r[2] or "{}"),
                 "source": r[3]} for r in rows]

    def config_rollback(self, history_id: int = 0) -> dict | None:
        """回滚到某次改动**之前**的配置。history_id=0 表示最近一次。

        回滚本身也记一条历史 —— 否则「回滚后又想回滚回去」就没路了。
        """
        if history_id:
            row = self.conn.execute(
                "SELECT id, config, changed FROM config_history WHERE id=?",
                (history_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id, config, changed FROM config_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        target = json.loads(row[1])
        # 历史快照里的密钥/密码是掩码。回滚时**保留当前正在用的**——
        # 把配置滚回去了、却顺手把（可能已换掉的）密钥也滚回去，轻则下单全挂、
        # 重则把作废的 key 写回去。参数可以回滚，凭据永远不跟着滚。
        current = self.load_config()
        for k in ("exchange_kwargs", "web_password"):
            if k in target:
                target[k] = current.get(k)
        self.save_config_audited(target, {"__rollback__": [None, f"history#{row[0]}"]},
                                 source="rollback")
        return target

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

    def runtime_keys(self, prefix: str = "") -> list[str]:
        rows = self.conn.execute("SELECT key FROM runtime_stats WHERE key LIKE ?",
                                 (prefix + "%",)).fetchall()
        return [r[0] for r in rows]

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
               (ts, mode, symbol, side, type, price, amount, cost, fee, order_id, status,
                dry_run, note, pnl, pnl_rate, liq_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (o.get("ts") or datetime.now(timezone.utc).isoformat(),
             o["mode"], o["symbol"], o["side"], o["type"],
             float(o.get("price", 0)), float(o.get("amount", 0)), float(o.get("cost", 0)),
             float(o.get("fee", 0)), str(o.get("order_id", "")),
             o.get("status", "filled"), 1 if o.get("dry_run") else 0, o.get("note", ""),
             float(o.get("pnl", 0)), float(o.get("pnl_rate", 0)),
             o.get("liq_source", "")),
        )
        self.conn.commit()

    def update_order_status(self, order_id: str, status: str):
        self.conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        self.conn.commit()

    # ===== 交易所成交明细去重（补录用）=====
    def trade_seen(self, symbol: str, trade_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_trades WHERE symbol=? AND trade_id=?",
            (symbol, str(trade_id))).fetchone() is not None

    def mark_trade_seen(self, symbol: str, trade_id: str, ts: str = ""):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_trades (symbol, trade_id, ts) VALUES (?, ?, ?)",
            (symbol, str(trade_id), ts))
        self.conn.commit()

    def get_orders(self, limit: int = 100, mode: str = None,
                   offset: int = 0) -> list[dict]:
        # 按成交时间排，而不是自增 id —— 补记/回填的记录 id 更大但时间更早，
        # 按 id 排会让它们跑到最前面，看起来像"时间乱了"
        where, args = "", []
        if mode:
            where = " WHERE mode=?"
            args.append(mode)
        rows = self.conn.execute(
            f"""SELECT id, ts, mode, symbol, side, type, price, amount, cost, fee,
                       order_id, status, dry_run, note, pnl, pnl_rate
                FROM orders{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            (*args, int(limit), int(offset))).fetchall()
        cols = ["id", "ts", "mode", "symbol", "side", "type", "price", "amount",
                "cost", "fee", "order_id", "status", "dry_run", "note", "pnl", "pnl_rate"]
        return [dict(zip(cols, r)) for r in rows]

    def orders_count(self, mode: str = None) -> int:
        """成交总笔数（分页用），可与 get_orders 传同一个 mode。"""
        if mode:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE mode=?", (mode,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()
        return int(row[0] or 0)

    def get_order_summary(self, mode: str = None) -> dict:
        """成交汇总：总盈亏、总手续费、买卖笔数、平仓胜负数。

        「平仓」按类型判定而不是按方向 —— 空单平仓是 buy，但同样是平仓、
        同样有盈亏；按方向算会把它排除在胜率之外，与前端盈亏列口径打架。
        现货马丁的止盈卖出（dca_sell）同样是平仓，必须计入。
        """
        # 平仓口径：普通平仓全算；强平（liquidation）只算 v2 运行期间的
        # （monitor/reconcile）—— 90 天历史回补（backfill）是 v1 时代的爆仓记录，
        # 混进来会让「总盈亏 -175U」与「胜率 84%」互相矛盾（实测发生过）。
        # 历史强平仍在成交明细与强平记录页完整展示，只是不进策略统计。
        closing = ("(type IN ('grid_sell','grid_trail','grid_external',"
                   "'short_close','close','dca_sell')"
                   " OR (type='liquidation' AND liq_source != 'backfill'))")
        where = " WHERE mode=?" if mode else ""
        row = self.conn.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN {closing} THEN pnl END), 0),
                   COALESCE(SUM(fee), 0),
                   COUNT(CASE WHEN side='sell' THEN 1 END),
                   COUNT(CASE WHEN side='buy' THEN 1 END),
                   COUNT(CASE WHEN {closing} AND pnl > 0 THEN 1 END),
                   COUNT(CASE WHEN {closing} AND pnl <= 0 THEN 1 END),
                   COALESCE(SUM(CASE WHEN {closing} THEN cost END), 0)
            FROM orders{where}""", (mode,) if mode else ()).fetchone()
        # 单笔平均盈亏率：与明细页每笔的「盈亏率」列同一口径，能直接对上。
        # （之前展示的 Σpnl/Σ卖出名义 是周转收益率 —— 网格资金反复周转，
        #   数字会远小于体感收益率，实测被用户质疑「1.55% 是不是算错了」。）
        q = f"SELECT AVG(pnl_rate) FROM orders WHERE {closing} AND pnl_rate != 0"
        args = []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        avg_rate = self.conn.execute(q, args).fetchone()[0]
        sell_cost = row[6] or 0
        return {
            "total_pnl": round(row[0], 4),
            "total_fee": round(row[1], 4),
            "net_pnl": round(row[0] - row[1], 4),
            "sell_count": row[2], "buy_count": row[3],
            "win_count": row[4], "loss_count": row[5],
            "win_rate": round(row[4] / (row[4] + row[5]), 4) if (row[4] + row[5]) > 0 else 0,
            "total_pnl_rate": round(row[0] / sell_cost, 6) if sell_cost > 0 else 0,
            "avg_pnl_rate": round(float(avg_rate), 6) if avg_rate is not None else 0,
        }

    # ===== 网格 =====
    def get_grid_state(self, symbol: str = None):
        if symbol:
            row = self.conn.execute(
                """SELECT symbol, lower, upper, grids, size_usdt, base_qty, total_cost, status,
                          realized_pnl, buy_count, sell_count FROM grid_state WHERE symbol=?""",
                (symbol,)).fetchone()
            if not row:
                return None
            cols = ["symbol", "lower", "upper", "grids", "size_usdt", "base_qty",
                    "total_cost", "status", "realized_pnl", "buy_count", "sell_count"]
            return dict(zip(cols, row))
        rows = self.conn.execute(
            """SELECT symbol, lower, upper, grids, size_usdt, base_qty, total_cost, status,
                      realized_pnl, buy_count, sell_count FROM grid_state""").fetchall()
        cols = ["symbol", "lower", "upper", "grids", "size_usdt", "base_qty",
                "total_cost", "status", "realized_pnl", "buy_count", "sell_count"]
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

    def clear_grid_order_id(self, row_id: int):
        """清空挂单号（交易所那笔已不存在时调用，下轮会重新挂）。"""
        self.conn.execute("UPDATE grid_levels SET order_id='' WHERE id=?", (row_id,))
        self.conn.commit()

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

    # ===== 逐笔持仓账本 =====
    def add_lot(self, symbol: str, price: float, qty: float, note: str = ""):
        """记录一笔买入（开仓/加仓）。"""
        self.conn.execute(
            "INSERT INTO grid_lots (symbol, ts, price, qty, remaining, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, datetime.now(timezone.utc).isoformat(),
             float(price), float(qty), float(qty), note))
        self.conn.commit()

    def get_lots(self, symbol: str, only_open: bool = True) -> list[dict]:
        sql = ("SELECT id, symbol, ts, price, qty, remaining, note FROM grid_lots "
               "WHERE symbol=?" + (" AND remaining > 0.0000001" if only_open else "")
               + " ORDER BY id")
        cols = ["id", "symbol", "ts", "price", "qty", "remaining", "note"]
        return [dict(zip(cols, r)) for r in self.conn.execute(sql, (symbol,)).fetchall()]

    def lots_qty_cost(self, symbol: str) -> tuple[float, float]:
        """账本里的持仓数量与成本（唯一真源）。"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(remaining), 0), COALESCE(SUM(remaining * price), 0) "
            "FROM grid_lots WHERE symbol=?", (symbol,)).fetchone()
        return float(row[0] or 0), float(row[1] or 0)

    def consume_lots(self, symbol: str, sell_price: float, amount: float) -> list[tuple]:
        """卖出一笔，按「低于卖价者中价格最高优先」核销买入批次。

        这样每笔卖出核销的就是「离它最近、且在它下方」的那一批买入 —— 正是
        网格里下一档的买入，因此单笔盈亏必然为正。返回 [(买入价, 核销数量)]，
        账本不足时返回部分，由调用方按持仓均价兜底。
        """
        left = float(amount)
        out: list[tuple] = []
        if left <= 0:
            return out
        rows = self.conn.execute(
            "SELECT id, price, remaining FROM grid_lots "
            "WHERE symbol=? AND remaining > 0.0000001 AND price < ? "
            "ORDER BY price DESC, id DESC", (symbol, float(sell_price))).fetchall()
        for lot_id, lp, rem in rows:
            if left <= 1e-9:
                break
            take = min(float(rem), left)
            self.conn.execute("UPDATE grid_lots SET remaining = remaining - ? WHERE id=?",
                              (take, lot_id))
            self.conn.execute("DELETE FROM grid_lots WHERE id=? AND remaining <= 0.0000001",
                              (lot_id,))
            out.append((float(lp), take))
            left -= take
        self.conn.commit()
        return out

    def reset_lots(self, symbol: str, qty: float, price: float, note: str = "对账重建"):
        """把账本重建成一笔（对账/迁移用）。"""
        self.conn.execute("DELETE FROM grid_lots WHERE symbol=?", (symbol,))
        if qty > 0 and price > 0:
            self.conn.execute(
                "INSERT INTO grid_lots (symbol, ts, price, qty, remaining, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, datetime.now(timezone.utc).isoformat(),
                 float(price), float(qty), float(qty), note))
        self.conn.commit()

    def clear_lots(self, symbol: str):
        self.conn.execute("DELETE FROM grid_lots WHERE symbol=?", (symbol,))
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
            """INSERT INTO dca_records (ts, symbol, price, amount, cost, fee, dry_run, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.get("ts") or datetime.now(timezone.utc).isoformat(), r["symbol"],
             float(r.get("price", 0)), float(r.get("amount", 0)), float(r.get("cost", 0)),
             float(r.get("fee", 0)), 1 if r.get("dry_run") else 0, r.get("note", "")))
        self.conn.commit()

    def get_dca_records(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, ts, symbol, price, amount, cost, fee, dry_run, note
               FROM dca_records ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        cols = ["id", "ts", "symbol", "price", "amount", "cost", "fee", "dry_run", "note"]
        return [dict(zip(cols, r)) for r in rows]

    # ===== 备份 / 检查点 =====
    def health_check(self) -> tuple[bool, str]:
        """轻量自检：连接是否还能读，以及 WAL 文件是否还在。

        WAL 文件被外部删掉（例如另一个实例启动时的损坏恢复流程）时，本进程
        持有的连接会与磁盘**脱钩**：写进的是已删除的 inode，磁盘上的库主文件
        却停在旧状态 —— 表现为「程序自己看着一切正常，新连接却报
        database disk image is malformed」。实测发生过一次，必须能自查出来。
        """
        try:
            self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()
        except Exception as e:
            return False, f"数据库查询失败: {e}"
        if self._wal:
            p = self.db_path + "-wal"
            if not os.path.exists(p):
                return False, (f"WAL 文件 {p} 已消失 —— 连接与磁盘脱钩，"
                               f"继续写入会得到不一致的库，必须重启程序")
        return True, ""

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
