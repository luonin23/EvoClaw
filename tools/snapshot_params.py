#!/usr/bin/env python3
"""L0 行为快照 —— 冻结「当前代码下每个参数的实际生效值」。

用途：参数中心重构（params.py 注册表）前后，用它做逐键比对，确保
「只是把定义挪了个地方」，没有偷偷改变任何真实交易行为。

    python3 tools/snapshot_params.py            # 生成 data/param-baseline.json
    python3 tools/snapshot_params.py --diff data/param-baseline.json   # 与基准比对

只读：连库一律用只读 URI。**绝不要**用项目里的 Database 类 —— 它的 close()
会做 wal_checkpoint(TRUNCATE)，可能把正在运行的 bot 的 -wal 删掉并毁库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from glob import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import DEFAULT_CONFIG                      # noqa: E402
from strategies.grid import GridStrategy               # noqa: E402

DB = os.path.join(ROOT, "data", "evoclaw-v2.db")
OUT = os.path.join(ROOT, "data", "param-baseline.json")        # 实盘快照（本地产物）
LOCK = os.path.join(ROOT, "tests", "baseline_defaults.json")   # 默认值锁（要提交）


# ---------- 配置读取（只读，不碰 Database 类） ----------
def read_db_config() -> dict:
    if not os.path.exists(DB):
        return {}
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        out = {}
        for k, v in c.execute("SELECT key, value FROM config"):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        return out
    finally:
        c.close()


def registry_keys(cfg: dict) -> dict[str, str]:
    """返回 {点分键: 所属区}。区 = 顶层标量 / grid / dca。"""
    keys = {}
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            for kk in v:
                keys[f"{k}.{kk}"] = k
        else:
            keys[k] = ""
    for k in cfg:                       # DB 里有、默认里没有的（如 exchange_kwargs）
        if k not in DEFAULT_CONFIG and not isinstance(cfg[k], dict):
            keys[k] = ""
    return keys


def dotted_get(cfg: dict, dotted: str):
    """按点分路径取值；不存在则抛 KeyError。"""
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        return (cfg.get(head) or {})[tail]
    return cfg[dotted]


# ---------- 源码扫描：找出所有硬编码兜底字面量 ----------
GET_RE = re.compile(r'\.get\(\s*"([a-z_]+)"\s*,\s*([^)]+)\)')


def scan_fallbacks(keys: dict[str, str]) -> dict[str, list[dict]]:
    """扫出 `cfg.get("键", 字面量)` 的每一处，作为「重构要消灭的清单」。"""
    names = {k.split(".")[-1] for k in keys}
    sites: dict[str, list[dict]] = {}
    files = sorted(glob(os.path.join(ROOT, "*.py"))) + \
        sorted(glob(os.path.join(ROOT, "strategies", "*.py")))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            for name, literal in GET_RE.findall(line):
                if name in names:
                    sites.setdefault(name, []).append(
                        {"site": f"{rel}:{lineno}", "literal": literal.strip()})
    return dict(sorted(sites.items()))


# ---------- 用 stdlib 直接执行策略里的纯函数，拿到派生值 ----------
class _Stub(GridStrategy):
    """只为调用那些只依赖 self.cfg 的纯方法，不碰网络/数据库。

    继承真类（而不是另写一个）是为了**调用真实的推导函数** —— 快照的意义
    就在于记录真实代码算出来的值，重写一份等于什么都没冻结。
    绕过 __init__：它会建 client / 拿 db，这里都不需要。
    """

    def __init__(self, config: dict):
        self.config = config
        self._selection = []
        self._sel_ts = 0.0


def derived_values(cfg: dict) -> dict:
    g = _Stub(cfg)
    picks = [{"range": 0.16}, {"range": 0.06}, {"range": 0.25}]
    out = {}
    for rng in (0.06, 0.16, 0.25):
        out[f"grids(振幅{rng})"] = GridStrategy._derive_grids(g, rng)
        out[f"band(振幅{rng})"] = round(GridStrategy._band_pct(g, rng), 8)
    for rng in (0.03, 0.16, 0.30):
        out[f"symbol_range_pct(pick.range={rng})"] = round(
            GridStrategy._symbol_range_pct(g, {"range": rng}), 8)
    out["symbol_range_pct(无range)"] = round(GridStrategy._symbol_range_pct(g, {}), 8)
    for eq in (10.0, 30.0, 100.0):
        out[f"auto_symbol_cap(权益{eq})"] = GridStrategy._auto_symbol_cap(g, eq, picks)
    return out


def build() -> dict:
    db = read_db_config()
    merged = {}                                     # 合并后 config（不写回）
    for k, v in DEFAULT_CONFIG.items():
        merged[k] = dict(v) if isinstance(v, dict) else v
    for k, v in db.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v

    keys = registry_keys(merged)
    sites = scan_fallbacks(keys)

    effective = {}
    for dotted, section in sorted(keys.items()):
        try:
            val = dotted_get(merged, dotted)
            src = "config"
            where = "DB" if (section and dotted.split(".")[1] in (db.get(section) or {})) \
                else ("DB" if dotted in db else "DEFAULT_CONFIG")
        except KeyError:
            val, src, where = None, "fallback", ""
        effective[dotted] = {
            "value": val,
            "source": src,
            "from": where,
            "fallback_sites": [s["site"] for s in sites.get(dotted.split(".")[-1], [])]
            if src == "fallback" else [],
        }

    return {
        "db_config": db,
        "default_config": DEFAULT_CONFIG,
        "effective": effective,
        "derived": derived_values(merged),
        # 只由 DEFAULT_CONFIG 推出的派生值 —— 与实盘库无关，供单元测试锁定
        # 「代码里的默认值 + 推导公式」这一对没被改动。
        "derived_defaults": derived_values(DEFAULT_CONFIG),
        "fallback_sites": sites,
        "stats": {
            "参数总数": len(keys),
            "硬编码兜底处数": sum(len(v) for v in sites.values()),
            "缺键(走兜底)的参数": [k for k, v in effective.items() if v["source"] == "fallback"],
        },
    }


def diff(baseline: dict, current: dict) -> int:
    """只比「生效值」和「派生值」—— 这才是会影响真实下单的部分。"""
    bad = 0
    for part, label in (("effective", "参数生效值"), ("derived", "派生值")):
        b, c = baseline.get(part, {}), current.get(part, {})
        for k in sorted(set(b) | set(c)):
            bv = (b.get(k) or {}).get("value") if part == "effective" else b.get(k)
            cv = (c.get(k) or {}).get("value") if part == "effective" else c.get(k)
            if bv != cv:
                print(f"  ❌ {label} {k}: 基准={bv!r} → 现在={cv!r}")
                bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", metavar="BASELINE", help="与快照比对，不一致则退出码 1")
    ap.add_argument("--write-lock", action="store_true",
                    help="写 tests/baseline_defaults.json（默认值锁，随代码提交）")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    if args.write_lock:
        # 只导出与实盘库无关的部分：代码里的默认值 + 它们推导出的派生值。
        # data/ 是 gitignore 的，锁必须放在能提交的地方，否则换台机器就失效。
        lock = {"default_config": DEFAULT_CONFIG,
                "derived_defaults": derived_values(DEFAULT_CONFIG)}
        with open(LOCK, "w", encoding="utf-8") as f:
            json.dump(lock, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"已写入默认值锁 {os.path.relpath(LOCK, ROOT)}")
        return 0

    snap = build()
    st = snap["stats"]
    print(f"参数总数 {st['参数总数']}；硬编码兜底 {st['硬编码兜底处数']} 处；"
          f"缺键走兜底的参数 {len(st['缺键(走兜底)的参数'])} 个")

    if args.diff:
        with open(args.diff, encoding="utf-8") as f:
            baseline = json.load(f)
        bad = diff(baseline, snap)
        if st["缺键(走兜底)的参数"]:
            print(f"  ⚠ 实盘库缺键、正在走兜底：{st['缺键(走兜底)的参数']}")
            bad += len(st["缺键(走兜底)的参数"])
        print("✅ 生效值与派生值完全一致" if bad == 0 else f"❌ {bad} 处不一致")
        return 1 if bad else 0

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"已写入 {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
