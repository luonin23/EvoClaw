"""EvoClaw v2.0 — 策略引擎。

主循环每 loop_interval 秒执行一次：
  1. 从 DB 重载配置（改配置即时生效）
  2. 同步 dry_run 到客户端
  3. 按 mode 调度对应策略（off / grid / dca）
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from strategies.grid import GridStrategy
from strategies.dca import DCAStrategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, client, db):
        self.client = client
        self.db = db
        self.config = db.load_config()
        self.running = False
        self.paused = bool(db.get_runtime_stat("paused", 0))
        self._last_mode = None
        self._last_ok = 0.0
        self.last_error = ""
        self.tick = 0

    def _reload(self):
        try:
            self.config = self.db.load_config()
        except Exception as e:
            log.error(f"Config reload failed: {e}")

    def _strategy(self):
        mode = self.config.get("mode", "off")
        if mode == "grid":
            return GridStrategy(self.client, self.db, self.config)
        if mode == "dca":
            return DCAStrategy(self.client, self.db, self.config)
        return None

    def set_paused(self, paused: bool):
        self.paused = paused
        self.db.set_runtime_stat("paused", 1 if paused else 0)
        log.warning(f"引擎 {'已暂停' if paused else '已恢复'}")

    async def run(self):
        self.running = True
        log.info(f"引擎启动 (mode={self.config.get('mode')}, dry_run={self.config.get('dry_run')}, paused={self.paused})")
        while self.running:
            interval = max(3, int(self.config.get("loop_interval", 10) or 10))
            try:
                if self.paused:
                    if self.tick % 30 == 0:
                        log.info("已暂停 — 跳过本轮")
                    self.tick += 1
                    await asyncio.sleep(interval)
                    continue

                self._reload()
                # 配置里的 dry_run 实时同步到客户端
                self.client.dry_run = bool(self.config.get("dry_run", True))
                self.client.min_order_notional = float(self.config.get("min_order_notional", 5) or 5)
                self.client.notional_safety = float(self.config.get("order_notional_safety", 1.02) or 1.02)

                mode = self.config.get("mode", "off")
                if mode != self._last_mode:
                    log.info(f"模式切换 → {mode}")
                    self._last_mode = mode

                strat = self._strategy()
                if strat is not None:
                    await asyncio.wait_for(strat.step(), timeout=60)

                self._last_ok = time.monotonic()
                self.last_error = ""
                self.tick += 1

                if self.tick % 60 == 0:
                    self.db.checkpoint()
                if self.tick % 30 == 0:
                    # 数据库自检：WAL 被外部删掉时，进程会「看着一切正常、
                    # 其实已与磁盘脱钩」。宁可停下不交易，也不要在一个
                    # 不一致的库上继续下单。
                    try:
                        ok, why = self.db.health_check()
                    except Exception as e:
                        ok, why = False, str(e)
                    if not ok:
                        log.error(f"数据库自检失败：{why} → 已暂停交易，请重启程序")
                        self.paused = True

            except asyncio.TimeoutError:
                self.last_error = "策略执行超时(60s)"
                log.error(f"策略执行超时，跳过本轮 (mode={self.config.get('mode')})")
            except Exception as e:
                self.last_error = str(e)
                log.error(f"引擎异常: {e}")

            await asyncio.sleep(interval)
        log.info("引擎停止")

    def stop(self):
        self.running = False

    async def health(self) -> dict:
        age = time.monotonic() - self._last_ok if self._last_ok else None
        return {
            "running": self.running,
            "paused": self.paused,
            "mode": self.config.get("mode", "off"),
            "dry_run": bool(self.config.get("dry_run", True)),
            "tick": self.tick,
            "last_ok_seconds_ago": round(age, 1) if age is not None else None,
            "last_error": self.last_error,
        }
