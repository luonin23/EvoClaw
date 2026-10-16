"""参数注册表：类型转换、范围校验、哨兵语义、补齐逻辑。"""
from __future__ import annotations

import unittest

import params
from main import fill_missing_defaults
from tests.harness import temp_db


class TestRegistry(unittest.TestCase):
    def test_keys_unique_and_sections_known(self):
        keys = [p.key for p in params.PARAMS]
        self.assertEqual(len(keys), len(set(keys)), "注册表里有重复的键")
        gids = {g[0] for g in params.GROUPS}
        for p in params.PARAMS:
            self.assertIn(p.group, gids, f"{p.key} 的分类 {p.group} 不在 GROUPS 里")

    def test_every_default_config_key_is_registered(self):
        """DEFAULT_CONFIG 里的每个键都必须能追溯到一条注册项。"""
        from config import DEFAULT_CONFIG
        for k, v in DEFAULT_CONFIG.items():
            if isinstance(v, dict):
                for sub in v:
                    self.assertIn(f"{k}.{sub}", params.by_key(), f"{k}.{sub} 没注册")
            else:
                self.assertIn(k, params.by_key(), f"{k} 没注册")

    def test_numeric_default_within_range(self):
        """数值参数的默认值必须落在自己声明的 [min, max] 里。

        这条是为一次性抓出一类错误而写的：`scale=100` 的参数如果把 min/max
        按**显示单位**填（比如 max=50 想表达 50%，但校验针对的是原始值 0.5），
        默认值就会立刻越界。哨兵参数的 0 是合法特例。
        """
        for p in params.PARAMS:
            if p.type not in ("int", "float") or p.min is None:
                continue
            if p.sentinel and p.default == 0:
                continue                       # 0 = 自动/跳过/无上限，合法
            self.assertGreaterEqual(p.default, p.min, f"{p.key} 默认值低于 min")
            self.assertLessEqual(p.default, p.max, f"{p.key} 默认值高于 max")

    def test_range_bounds_use_raw_units(self):
        """带 scale 的参数，min/max 必须是**原始值**，不能是显示值。"""
        for p in params.PARAMS:
            if p.scale == 1.0 or p.max is None:
                continue
            # 0.5 (50%) / 1.0 (100%) / 2.0 (200%) —— 显示值会是 50/100/200
            self.assertLessEqual(p.max, 2.0,
                                 f"{p.key} 的 max={p.max} 看着像显示单位，应为原始值")

    def test_percent_params_are_scaled_not_raw(self):
        """百分比类参数：注册表存小数，UI 显示时 ×100 —— 别把 0.035 存成 3.5。"""
        for key in ("grid.short_stop_pct", "grid.min_tp_ratio", "grid.entry_pos_max"):
            p = params.by_key()[key]
            self.assertEqual(p.widget, "percent", key)
            self.assertEqual(p.scale, 100, key)
            self.assertLess(p.default, 1.0, f"{key} 的默认值看起来已经是百分数了")


class TestCoerce(unittest.TestCase):
    def ok(self, key, value):
        ok, out, msg = params.validate(key, value)
        self.assertTrue(ok, f"{key}={value!r} 应通过，却报：{msg}")
        return out

    def bad(self, key, value):
        ok, _, msg = params.validate(key, value)
        self.assertFalse(ok, f"{key}={value!r} 应被拒绝")
        return msg

    def test_type_conversion(self):
        self.assertEqual(self.ok("grid.leverage", "5"), 5)
        self.assertIsInstance(self.ok("grid.leverage", 5), int)
        self.assertIs(self.ok("dry_run", "true"), True)
        self.assertIs(self.ok("dry_run", False), False)
        self.assertAlmostEqual(self.ok("grid.short_stop_pct", 0.035), 0.035)

    def test_range_enforced(self):
        self.assertIn("不能小于", self.bad("grid.leverage", -1))
        self.assertIn("不能大于", self.bad("grid.leverage", 999))
        self.bad("grid.short_stop_pct", 0)          # 无哨兵 → 0 也不能低于 min

    def test_enum_enforced(self):
        self.assertEqual(self.ok("mode", "grid"), "grid")
        self.assertIn("只能是", self.bad("mode", "垃圾"))

    def test_list_normalised(self):
        self.assertEqual(self.ok("grid.blacklist", ["zecusdt", " arb "]), ["ZECUSDT", "ARB"])
        self.bad("grid.blacklist", "ZECUSDT")       # 不是列表
        self.bad("grid.blacklist", [""])            # 空元素

    def test_short_name_lookup(self):
        """config 表里 grid 的子键是短名，接口收到的是短名。"""
        self.assertFalse(params.validate("leverage", -1)[0])
        self.assertEqual(params.validate("leverage", 7)[1], 7)

    def test_unknown_key_passes_through(self):
        """grid_selection 这类运行时状态不在注册表里，绝不能拦。"""
        ok, val, _ = params.validate("grid_selection", [{"symbol": "X"}])
        self.assertTrue(ok)
        self.assertEqual(val, [{"symbol": "X"}])

    def test_sentinel_value_allowed_below_min(self):
        """哨兵值（0 = 自动）必须放行，哪怕它低于 min。"""
        p = params.Param("x.y", "float", 5.0, "global", "测试", min=1.0, sentinel="auto")
        params._INDEX[p.key] = p
        try:
            self.assertEqual(params.coerce(p.key, 0), 0)
            self.assertEqual(params.coerce(p.key, 0.0), 0.0)
            with self.assertRaises(ValueError):
                params.coerce(p.key, 0.5)           # 非 0 的值仍受 min 约束
        finally:
            del params._INDEX[p.key]


class TestFillMissingDefaults(unittest.TestCase):
    """补齐缺失键时，带哨兵语义的参数必须**故意跳过**。"""

    def setUp(self):
        self.db, self.path = temp_db()
        self.log = type("L", (), {"info": lambda *a, **k: None})()

    def tearDown(self):
        self.db.close()
        import os
        if os.path.exists(self.path):
            os.remove(self.path)

    def _config(self):
        return {k: v for k, v in self.db.load_config().items()}

    def test_fills_plain_param_without_touching_others(self):
        self.db.save_config({
            "grid": {"leverage": 7},                       # 缺 short_stop_pct
            "grid_selection": [{"symbol": "ZECUSDT"}],     # 运行时状态
            "mode": "grid",
        })
        fill_missing_defaults(self.db, None, self.log)
        cfg = self._config()
        self.assertEqual(cfg["grid"]["leverage"], 7, "已有的值不能被改")
        self.assertEqual(cfg["grid"]["short_stop_pct"], 0.035, "缺失的普通参数应被补齐")
        self.assertEqual(cfg["mode"], "grid")
        self.assertEqual(cfg["grid_selection"], [{"symbol": "ZECUSDT"}],
                         "运行时状态必须原样保留")

    def test_skips_sentinel_params(self):
        self.db.save_config({"grid": {}})          # size_usdt / auto_select 全缺
        fill_missing_defaults(self.db, None, self.log)
        g = self._config()["grid"]
        self.assertNotIn("size_usdt", g, "size_usdt 缺 = 跳过该标的，补 20 会变成真下单")
        self.assertNotIn("auto_select", g, "auto_select 缺 = 手动模式，补 True 会换一批币")
        self.assertIn("short_stop_pct", g, "普通参数照补")
        self.assertIn("refresh_min", g, "refresh_min 已无哨兵语义（废弃的小时级字段已删）")


if __name__ == "__main__":
    unittest.main()
