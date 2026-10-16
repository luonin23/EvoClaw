"""配置改动的审计、历史与一键回滚（实盘改错配置的救命通道）。"""
from __future__ import annotations

import json
import os
import unittest

from tests.harness import temp_db
from web_server import WebServer


class TestDiff(unittest.TestCase):
    def test_reports_nested_and_top_level_changes(self):
        old = {"mode": "grid", "grid": {"leverage": 3, "size_usdt": 20}, "dca": {}}
        new = {"mode": "dca", "grid": {"leverage": 5, "size_usdt": 20}, "dca": {}}
        got = WebServer._diff_changed(old, new)
        self.assertEqual(got, {"grid.leverage": [3, 5], "mode": ["grid", "dca"]})

    def test_never_leaks_api_secret_into_audit(self):
        old = {"exchange_kwargs": {"apiKey": "AAA"}, "mode": "grid"}
        new = {"exchange_kwargs": {"apiKey": "BBB"}, "mode": "grid"}
        self.assertEqual(WebServer._diff_changed(old, new), {},
                         "密钥绝不能进审计明文")


class TestNoSecretLeak(unittest.TestCase):
    """密钥绝不能从任何只读接口漏出去。"""

    def setUp(self):
        self.db, self.path = temp_db()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_history_listing_carries_no_secrets(self):
        self.db.save_config({"mode": "grid",
                             "exchange_kwargs": {"apiKey": "SECRETKEY", "secret": "S3CR3T"}})
        self.db.save_config_audited({"mode": "off",
                                     "exchange_kwargs": {"apiKey": "SECRETKEY",
                                                         "secret": "S3CR3T"}},
                                    {"mode": ["grid", "off"]})
        blob = json.dumps(self.db.config_history(), ensure_ascii=False)
        self.assertNotIn("S3CR3T", blob)
        self.assertNotIn("SECRETKEY", blob)

    def test_rollback_keeps_working_after_restoring(self):
        """回滚要能把密钥一起还原（所以历史里必须留着），但接口不能回传。"""
        self.db.save_config({"mode": "grid", "exchange_kwargs": {"apiKey": "K1"}})
        self.db.save_config_audited({"mode": "off", "exchange_kwargs": {"apiKey": "K2"}}, {})
        target = self.db.config_rollback()
        self.assertEqual(target["exchange_kwargs"]["apiKey"], "K1", "回滚要能还原密钥")
        self.assertEqual(self.db.load_config()["exchange_kwargs"]["apiKey"], "K1")


class TestSectionMerge(unittest.TestCase):
    """保存一个键不能把同段其它键冲掉 —— 实盘踩过。"""

    def test_unknown_new_section_is_merged_not_replaced(self):
        cfg = {"profit_dca": {"enabled": True, "step_usdt": 20, "ratio": 0.3,
                              "symbols": ["DOGEUSDT"], "max_total": 0}}
        body = {"profit_dca": {"step_usdt": 50}}
        WebServer._merge_sections(cfg, body)
        got = body["profit_dca"]
        self.assertEqual(got["step_usdt"], 50)
        for k in ("enabled", "ratio", "symbols", "max_total"):
            self.assertIn(k, got, f"{k} 被整体替换冲掉了")
        self.assertEqual(got["ratio"], 0.3)

    def test_merge_list_comes_from_registry(self):
        """段名必须覆盖注册表里的所有段；写死清单会漏掉新段。"""
        import params
        want = {p.section for p in params.PARAMS if p.section}
        for sec in want:
            cfg = {sec: {"a": 1, "b": 2}}
            body = {sec: {"a": 9}}
            WebServer._merge_sections(cfg, body)
            self.assertEqual(body[sec].get("b"), 2, f"{sec} 段没有走字段级合并")


class TestHistoryAndRollback(unittest.TestCase):
    def setUp(self):
        self.db, self.path = temp_db()

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_audited_save_records_previous_config(self):
        self.db.save_config({"mode": "grid", "grid": {"leverage": 3},
                             "grid_selection": [{"symbol": "ZECUSDT"}]})
        self.db.save_config_audited({"mode": "dca", "grid": {"leverage": 5},
                                     "grid_selection": [{"symbol": "ZECUSDT"}]},
                                    {"grid.leverage": [3, 5]}, source="1.2.3.4")
        hist = self.db.config_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["changed"], {"grid.leverage": [3, 5]})
        self.assertEqual(hist[0]["source"], "1.2.3.4")
        self.assertEqual(self.db.load_config()["mode"], "dca", "新配置应已生效")

    def test_rollback_restores_previous_config(self):
        self.db.save_config({"mode": "grid", "grid": {"leverage": 3}})
        self.db.save_config_audited({"mode": "off", "grid": {"leverage": 99}},
                                    {"grid.leverage": [3, 99]})
        self.assertEqual(self.db.load_config()["mode"], "off")

        self.db.config_rollback()
        cfg = self.db.load_config()
        self.assertEqual(cfg["mode"], "grid", "回滚应恢复改动前的配置")
        self.assertEqual(cfg["grid"]["leverage"], 3)

    def test_rollback_is_itself_recorded(self):
        """回滚也要留痕，否则「回滚完又想滚回去」就没路了。"""
        self.db.save_config({"mode": "grid"})
        self.db.save_config_audited({"mode": "off"}, {"mode": ["grid", "off"]})
        self.db.config_rollback()
        hist = self.db.config_history()
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["source"], "rollback")

    def test_runtime_state_survives_audited_save_and_rollback(self):
        """grid_selection 是运行时状态，住在同一张表里，任何路径都不能弄丢它。"""
        self.db.save_config({"mode": "grid", "grid_selection": [{"symbol": "ENAUSDT"}]})
        self.db.save_config_audited({"mode": "off",
                                     "grid_selection": [{"symbol": "ENAUSDT"}]}, {})
        self.assertEqual(self.db.load_config()["grid_selection"], [{"symbol": "ENAUSDT"}])
        self.db.config_rollback()
        self.assertEqual(self.db.load_config()["grid_selection"], [{"symbol": "ENAUSDT"}])

    def test_history_is_capped(self):
        self.db.save_config({"mode": "grid"})
        for i in range(30):
            self.db.save_config_audited({"mode": "grid", "n": i}, {}, keep=5)
        self.assertLessEqual(len(self.db.config_history(100)), 5)

    def test_rollback_without_history_returns_none(self):
        self.db.save_config({"mode": "grid"})
        self.assertIsNone(self.db.config_rollback())


if __name__ == "__main__":
    unittest.main()
