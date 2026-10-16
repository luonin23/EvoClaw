"""默认值锁：`DEFAULT_CONFIG` 与它推导出的派生值，一个字节都不许悄悄变。

背景：参数中心的改造（params.py 注册表）要把 `DEFAULT_CONFIG` 从手写字面量
改成从注册表派生。这个测试保证「只是把定义挪了个地方」，没有顺手改掉任何
一个默认值 —— 改默认值会直接影响真实下单。

故意改默认值时：重新生成基准（`python3 tools/snapshot_params.py`），
并在提交说明里写清楚改了哪个、为什么。
"""
from __future__ import annotations

import json
import os
import unittest

from config import DEFAULT_CONFIG
from tests.harness import ROOT
from tools.snapshot_params import derived_values

BASELINE = os.path.join(ROOT, "tests", "baseline_defaults.json")


class TestDefaultsLocked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BASELINE):
            raise unittest.SkipTest(
                f"缺少默认值锁 {BASELINE}（跑 python3 tools/snapshot_params.py --write-lock）")
        with open(BASELINE, encoding="utf-8") as f:
            cls.base = json.load(f)

    def test_default_config_unchanged(self):
        want = self.base["default_config"]
        got = DEFAULT_CONFIG
        self.assertEqual(set(want), set(got), "顶层键发生了变化")

        for key in sorted(want):
            if isinstance(want[key], dict):
                self.assertEqual(set(want[key]), set(got[key]), f"{key} 的子键发生了变化")
                for sub in sorted(want[key]):
                    self.assertEqual(
                        want[key][sub], got[key][sub],
                        f"默认值变了：{key}.{sub} {want[key][sub]!r} → {got[key][sub]!r}")
            else:
                self.assertEqual(want[key], got[key],
                                 f"默认值变了：{key} {want[key]!r} → {got[key]!r}")

    def test_derived_values_unchanged(self):
        """档位数 / 档距 / 区间宽度 / 同时跑几个标的 —— 推导公式变了要立刻暴露。"""
        want = self.base["derived_defaults"]
        got = derived_values(DEFAULT_CONFIG)
        self.assertEqual(want, got, "推导公式或它依赖的默认值变了")


if __name__ == "__main__":
    unittest.main()
