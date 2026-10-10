"""EvoClaw v2.0 — 入口。

双模式（低杠杆网格 / 现货定投）· dry-run 优先 · 独立数据库与端口，可与 v1 并存。
"""

import asyncio
import fcntl
import gc
import glob
import json
import logging
import logging.handlers
import os
import secrets
import signal
import sqlite3
import sys

from aiohttp import web

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("data", exist_ok=True)

from config import DEFAULT_CONFIG
from database import Database
from exchange_client import ExchangeClient
from engine import Engine
from web_server import WebServer

PORT = 8091                     # v1 用 8081，v2 用 8091，可并存
DB_PATH = "data/evoclaw-v2.db"
V1_DB_PATH = "data/evoclaw.db"  # 用于迁移 API key
PID_FILE = "data/evoclaw-v2.pid"


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        "data/v2.log", maxBytes=3 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    for f in sorted(glob.glob("data/v2.log.[0-9]*"))[:-5]:
        try:
            os.remove(f)
        except OSError:
            pass


def acquire_pid_lock():
    try:
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        return fd
    except (OSError, IOError):
        print("ERROR: 已有一个 EvoClaw v2 实例在运行，退出。", file=sys.stderr)
        sys.exit(1)


def release_pid_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def migrate_api_keys(db: Database, log):
    """v2 的 config 为空时，从 v1 数据库迁移 API key（用户：key 都在代码/库里）。"""
    cfg = db.load_config()
    if cfg.get("exchange_kwargs", {}).get("apiKey"):
        return
    if not os.path.exists(V1_DB_PATH):
        log.warning("未找到 v1 数据库，需要在配置页手动填写 API key")
        return
    try:
        c = sqlite3.connect(V1_DB_PATH)
        row = c.execute("SELECT value FROM config WHERE key='exchange_kwargs'").fetchone()
        c.close()
        if not row:
            return
        kw = json.loads(row[0])
        if kw.get("apiKey"):
            cfg["exchange_kwargs"] = {"apiKey": kw["apiKey"], "secret": kw.get("secret", "")}
            db.save_config(cfg)
            log.info("已从 v1 迁移 API key")
    except Exception as e:
        log.warning(f"API key 迁移失败（可手动填写）: {e}")


def verify_and_repair_db(log):
    """启动时检查数据库完整性；损坏则从最近的完好备份自动恢复。

    SQLite 索引损坏不会报错、不会崩溃，只会让主键约束悄悄失效
    （实测出现过同一 symbol 两行）。必须主动检测。
    """
    import shutil, glob as _glob

    def _ok(path):
        try:
            c = sqlite3.connect(path)
            r = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            return r == "ok"
        except Exception:
            return False

    if _ok(DB_PATH):
        return
    log.error("数据库完整性检查未通过，尝试从备份恢复…")
    for bak in sorted(_glob.glob(os.path.join("data", "backup-v2", "evoclaw-v2-*.db")), reverse=True):
        if _ok(bak):
            try:
                shutil.copyfile(DB_PATH, DB_PATH + ".corrupt")
                for suf in ("", "-wal", "-shm"):
                    p = DB_PATH + suf
                    if os.path.exists(p):
                        os.remove(p)
                shutil.copyfile(bak, DB_PATH)
                log.warning(f"已从备份恢复: {bak}（损坏库另存为 .corrupt）")
                return
            except Exception as e:
                log.error(f"从 {bak} 恢复失败: {e}")
    log.error("没有可用备份，将以当前库继续（风险自负）")


async def main():
    gc.collect()
    setup_logging()
    log = logging.getLogger("main")

    verify_and_repair_db(log)
    db = Database(DB_PATH)
    if db.seed_config(DEFAULT_CONFIG):
        log.info("已用默认配置初始化数据库")

    migrate_api_keys(db, log)

    # 首次启动生成随机网页密码
    cfg = db.load_config()
    if not cfg.get("web_password"):
        pwd = secrets.token_urlsafe(9)
        cfg["web_password"] = pwd
        db.save_config(cfg)
        log.warning(f"已生成网页管理密码（请记录）：{pwd}")

    cfg = db.load_config()

    # 启动备份
    try:
        path = db.backup_to("data/backup-v2", keep=14)
        if path:
            log.info(f"数据库备份：{path}")
    except Exception as e:
        log.warning(f"备份失败（非致命）: {e}")

    client = ExchangeClient(cfg)
    try:
        await client.load_markets()
    except Exception as e:
        log.warning(f"load_markets 失败（将重试）: {e}")

    engine = Engine(client, db)
    server = WebServer(client, db, engine)

    runner = web.AppRunner(server.app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Web 服务启动于 :{PORT}")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)

    try:
        await engine.run()
    except Exception as e:
        log.error(f"引擎致命错误: {e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass
        await runner.cleanup()
        db.close()
        log.info("已退出")


if __name__ == "__main__":
    fd = acquire_pid_lock()
    try:
        asyncio.run(main())
    finally:
        release_pid_lock(fd)
