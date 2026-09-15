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


@unittest.skipIf(ac is None, _SKIP)
class TestResultCardKeepsTitle(unittest.TestCase):
    """原位更新=整卡替换：结果卡必须带回原标题（含会话名），否则决定后看不出是哪件事；
    多选卡还要把按钮 key 还原成完整选项文案写进决定文件。"""

    def setUp(self):
        self._decisions = ac.DECISIONS_DIR
        self._log = ac.log
        self._meta = dict(ac.TASK_META)
        self.tmp = tempfile.mkdtemp(prefix="zcode_title_")
        ac.DECISIONS_DIR = self.tmp
        self.logs = []
        ac.log = self.logs.append
        self.updates = []

        async def fake_update(frame, card):
            self.updates.append(card)

        self._update = ac.ws.update_template_card
        ac.ws.update_template_card = fake_update

    def tearDown(self):
        ac.DECISIONS_DIR = self._decisions
        ac.log = self._log
        ac.TASK_META.clear()
        ac.TASK_META.update(self._meta)
        ac.ws.update_template_card = self._update
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _click(self, task, key):
        frame = {"headers": {"req_id": "r"},
                 "body": {"event": {"template_card_event": {"event_key": key, "task_id": task}}}}
        asyncio.run(ac.on_card_click(frame))

    def test_binary_card_result_keeps_original_title(self):
        ac.TASK_META["zc-t1"] = {"title": "写周报 · 请求确认", "options": None}
        self._click("zc-t1", "approve")
        self.assertEqual(len(self.updates), 1)
        card = self.updates[0]
        self.assertEqual(card["main_title"]["title"], "写周报 · 请求确认",
                         "结果卡标题必须保留原标题（会话名）")
        self.assertIn("✅ 已批准", card["main_title"]["desc"], "决定文案挪到 desc 行")
        self.assertEqual(card["card_type"], "button_interaction")

    def test_multi_choice_restores_full_option_text(self):
        ac.TASK_META["zc-t2"] = {"title": "选课 · 请求确认",
                                 "options": {"opt_a": "继续收尾", "opt_b": "做新功能"}}
        self._click("zc-t2", "opt_b")
        with open(os.path.join(self.tmp, "zc-t2.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["choice"], "做新功能", "决定文件要记完整选项文案，不是光秃秃的 B")
        self.assertEqual(d["key"], "opt_b")
        self.assertIn("🔘 已选择：做新功能", self.updates[0]["main_title"]["desc"])

    def test_dup_click_recycles_meta(self):
        ac.TASK_META["zc-t3"] = {"title": "x", "options": None}
        self._click("zc-t3", "approve")
        self._click("zc-t3", "deny")   # 重复点击
        self.assertNotIn("zc-t3", ac.TASK_META, "重复点击也要回收元数据，防内存残留")
        with open(os.path.join(self.tmp, "zc-t3.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["decision"], "allow", "第一次决定保持有效")


if __name__ == "__main__":
    unittest.main()
