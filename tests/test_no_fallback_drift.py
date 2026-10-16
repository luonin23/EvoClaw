"""兜底字面量守卫 —— 防止「改了 params.py 却没改代码里的字面量」。

这是本次重构要根治的那类事故：同一参数在三处各有默认值，改一处漏两处。
现在注册表是唯一真源，代码里残留的 `cfg.get("键", 字面量)` 必须满足：

  · 普通参数  → 字面量必须**等于注册表里的默认值**
  · 哨兵参数  → 字面量必须是**哨兵值**（0 / False），代表「自动/跳过/回退」，
                与默认值不同是**故意的**（size_usdt=20 是「每格 20U」，
                而缺失时的 0 是「跳过该标的」——两码事）

谁要是把 `cfg.get("short_stop_pct", 0.03)` 这类过期字面量留在代码里，这条测试
会立刻指名道姓地报出来。
"""
from __future__ import annotations

import glob
import os
import re
import unittest

import params
from tests.harness import ROOT

GET_RE = re.compile(r'\.get\(\s*"([a-z_]+)"\s*,\s*([^)]+)\)')
NUM_RE = re.compile(r"^-?[\d_]+(?:\.[\d_]+)?$")
SENTINEL_VALUE = {"auto": 0, "unlimited": 0, "skip": 0, "fallback": 0, "off": False}


def _param(name: str):
    if name in params._INDEX:
        return params._INDEX[name]
    hits = [p for p in params.PARAMS if p.key.endswith("." + name)]
    return hits[0] if len(hits) == 1 else None


def _literal(raw: str):
    raw = raw.strip()
    if raw in ("True", "False"):
        return raw == "True"
    if NUM_RE.match(raw):
        return float(raw.replace("_", ""))
    return None                     # 列表/字符串/表达式等，不参与本次比对


def scan():
    files = sorted(glob.glob(os.path.join(ROOT, "*.py"))) + \
        sorted(glob.glob(os.path.join(ROOT, "strategies", "*.py")))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("tests") or rel.startswith("tools"):
            continue
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            for name, raw in GET_RE.findall(line):
                p = _param(name)
                lit = _literal(raw)
                if p is None or lit is None:
                    continue
                yield rel, lineno, name, lit, p


class TestFallbackLiteralsMatchRegistry(unittest.TestCase):
    def test_plain_params_fall_back_to_their_registry_default(self):
        bad = [f"{rel}:{ln} cfg.get(\"{n}\", {lit}) 应为 {p.default!r}"
               for rel, ln, n, lit, p in scan()
               if not p.sentinel and lit != p.default]
        self.assertEqual(bad, [], "这些字面量兜底已经和注册表默认值脱节了：\n  " + "\n  ".join(bad))

    def test_sentinel_params_fall_back_to_the_sentinel_value(self):
        bad = []
        for rel, ln, n, lit, p in scan():
            if not p.sentinel:
                continue
            want = SENTINEL_VALUE[p.sentinel]
            if lit != want:
                bad.append(f"{rel}:{ln} cfg.get(\"{n}\", {lit}) 哨兵 {p.sentinel} 的值应为 {want!r}")
        self.assertEqual(bad, [], "哨兵参数的兜底值写错了：\n  " + "\n  ".join(bad))

    def test_scanner_actually_finds_something(self):
        """守卫自己也要有守卫 —— 正则失效会让上面两条静默通过。"""
        self.assertGreater(len(list(scan())), 50, "扫描不到兜底字面量，正则可能失效了")


if __name__ == "__main__":
    unittest.main()
