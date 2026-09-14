# -*- coding: utf-8 -*-
"""aibot_connector 的指令处理与卡片点击幂等单测。

依赖 wecom-aibot-python-sdk 与本机 scripts/aibot_config.json（即装了「手机批准」才有）；
两者缺一就整类跳过，不影响其它测试。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认测仓库里的 scripts/；想对着真实安装测（SDK 与 aibot_config.json 都在那边）：
#   PowerShell: $env:ZCODE_NOTIFY_SCRIPTS="C:\Users\<you>\.zcode\task-notify"
# 测试会把 DECISIONS_DIR 与 log 换成临时/打桩版本，不会产生生产副作用。
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

_IMPORT_ERR = None
try:
    import aibot_connector as ac
except Exception as e:  # 缺 SDK 或缺 aibot_config.json
    ac = None
    _IMPORT_ERR = e

_SKIP = f"需要 aibot SDK 与 scripts/aibot_config.json（{_IMPORT_ERR}）"


@unittest.skipIf(ac is None, _SKIP)
class TestCommands(unittest.TestCase):
    def test_status_replies_with_usage(self):
        reply = ac.handle_command("状态")
        self.assertIsNotNone(reply)
        self.assertIn("可用指令", reply)
        self.assertIn("帮助", reply)

    def test_status_alias_matches_chinese(self):
        self.assertEqual(ac.handle_command("status"), ac.handle_command("状态"))

    def test_non_command_is_silent(self):
        self.assertIsNone(ac.handle_command("你好"))

    def test_help_lists_commands_and_scope_boundary(self):
        reply = ac.handle_command("帮助")
        for token in ("状态", "静默", "最近", "远程控制页面"):
            self.assertIn(token, reply)

    def test_recent_reads_push_log(self):
        reply = ac.handle_command("最近")
        self.assertTrue(reply.startswith("🕘"), reply)


@unittest.skipIf(ac is None, _SKIP)
class TestCardClickIdempotency(unittest.TestCase):
    """结果卡按钮仍可再点，因此同一 task_id 只认第一次决定。"""

    def setUp(self):
        self._decisions_dir = ac.DECISIONS_DIR
        self._log = ac.log
        self.tmp = tempfile.mkdtemp(prefix="zcode_idem_")
        ac.DECISIONS_DIR = self.tmp
        self.logs = []
        ac.log = self.logs.append
        self.task = "zc-unit-test"
        self.frame = {"headers": {"req_id": "req_unit"},
                      "body": {"event": {"template_card_event": {
                          "event_key": "approve", "task_id": self.task}}}}

    def tearDown(self):
        ac.DECISIONS_DIR = self._decisions_dir
        ac.log = self._log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _click(self, frame=None):
        asyncio.run(ac.on_card_click(frame or self.frame))

    def _path(self, task=None):
        return os.path.join(self.tmp, (task or self.task) + ".json")

    def test_first_click_writes_decision(self):
        self._click()
        self.assertTrue(os.path.exists(self._path()))
        with open(self._path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["decision"], "allow")
        self.assertTrue(any(e.get("decision") == "allow" for e in self.logs))

    def test_second_click_is_ignored(self):
        self._click()
        with open(self._path(), encoding="utf-8") as f:
            first = f.read()
        self.logs.clear()

        self._click()

        with open(self._path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), first, "重复点击不应改写决定文件")
        self.assertTrue(any(e.get("decision_dup") for e in self.logs), self.logs)
        self.assertFalse(any(e.get("decision") == "allow" for e in self.logs), self.logs)

    def test_deny_is_recorded(self):
        frame = {"headers": {"req_id": "req_unit"},
                 "body": {"event": {"template_card_event": {
                     "event_key": "deny", "task_id": "zc-unit-deny"}}}}
        self._click(frame)
        with open(self._path("zc-unit-deny"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["decision"], "deny")

    def test_foreign_task_id_is_ignored(self):
        frame = {"headers": {"req_id": "req_unit"},
                 "body": {"event": {"template_card_event": {
                     "event_key": "approve", "task_id": "not-ours"}}}}
        self._click(frame)
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
