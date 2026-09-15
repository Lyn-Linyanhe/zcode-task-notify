# -*- coding: utf-8 -*-
"""approve_flow 的 AskUserQuestion 多选卡单测 —— 零第三方依赖。

对应 2026-09-15 的协议实测：AskUserQuestion 触发 PermissionRequest，payload 的
tool_input.questions[].options[] 带完整选项；工具的 answers（键=问题文本，值=选项
label）由"permission component"收集——手机卡片就是这个组件。回注协议：
behavior=allow + updatedInput → ZCode 按 modify 放行（"Allowed with modified input"）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import approve_flow as af  # noqa: E402

QUESTION = "摘要请求超时目前 10 秒，要调到 15 秒吗？"


def _ask_payload(multi_select=False, n_questions=1):
    q = {"question": QUESTION, "header": "摘要超时", "multiSelect": multi_select,
         "options": [
             {"label": "调到 15 秒", "description": "更宽松"},
             {"label": "维持 10 秒", "description": "最快"},
             {"label": "关掉 LLM 摘要", "description": "不管了"},
         ]}
    questions = [q] + [{"question": f"问题{i}？", "header": "h", "multiSelect": False,
                        "options": [{"label": "x"}, {"label": "y"}]}
                       for i in range(1, n_questions)]
    return {
        "hookEventName": "PermissionRequest",
        "toolName": "AskUserQuestion", "tool_name": "AskUserQuestion",
        "requestId": "req-test-1",
        "session_id": "sess_unit",
        "riskLevel": "low",
        "reason": "Tool AskUserQuestion requires user interaction",
        "toolInput": {"questions": questions},
    }


class ApproveFlowTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zcode_af_")
        os.makedirs(os.path.join(self.tmp, "decisions"), exist_ok=True)
        self.patches = [
            mock.patch.object(af, "BASE", self.tmp),
            mock.patch.object(af, "_cfg", lambda: {"card_timeout_sec": 2, "local_port": 17899}),
            mock.patch.object(af, "_health", lambda port, timeout=2: True),
            mock.patch.object(af, "_user_active", lambda threshold=60: False),
        ]
        self.posted = []

        def fake_post(port, body, timeout=5):
            self.posted.append(body)
            return True

        self.patches.append(mock.patch.object(af, "_post_card", fake_post))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decide(self, task_id, data):
        path = os.path.join(self.tmp, "decisions", f"zc-{task_id}.json")
        data = dict(data, ts=time.time())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _path(self, task_id="req-test-1"):
        return os.path.join(self.tmp, "decisions", f"zc-{task_id}.json")


class TestAskCardSpec(ApproveFlowTestBase):
    def test_ask_question_builds_multi_choice_card(self):
        af.run(_ask_payload())
        self.assertEqual(len(self.posted), 1)
        body = self.posted[0]
        self.assertIn("· 请选择", body["title"], "标题带会话段与「请选择」")
        self.assertIn(QUESTION[:50], body["desc"])
        self.assertIn("A=调到 15 秒", body["desc"], "完整选项映射放描述行")
        opts = body["options"]
        self.assertEqual([o["key"] for o in opts], ["A", "B", "C"])
        self.assertEqual([o["text"] for o in opts], ["A", "B", "C"], "按钮只放短标签")
        self.assertEqual(opts[1]["label"], "维持 10 秒", "label 存完整文案供回流")

    def test_choice_is_returned_as_updated_input(self):
        result = {}

        def post_and_decide(port, body, timeout=5):
            self.posted.append(body)
            self._decide("req-test-1", {"choice": "维持 10 秒", "key": "B"})
            return True

        with mock.patch.object(af, "_post_card", post_and_decide):
            result = af.run(_ask_payload())
        self.assertEqual(result["behavior"], "allow")
        ti = result["updatedInput"]
        self.assertEqual(ti["answers"], {QUESTION: "维持 10 秒"},
                         "answers 键=问题文本，值=选项 label")
        self.assertEqual(ti["questions"][0]["options"][0]["label"], "调到 15 秒",
                         "questions 原样保留，ZCode 按 modify 放行")

    def test_deny_decision_passes_through(self):
        def post_and_decide(port, body, timeout=5):
            self._decide("req-test-1", {"decision": "deny"})
            return True

        with mock.patch.object(af, "_post_card", post_and_decide):
            result = af.run(_ask_payload())
        self.assertEqual(result["behavior"], "deny")
        self.assertIn("拒绝", result["message"])

    def test_multiselect_or_multi_question_fall_back_to_binary(self):
        for payload in (_ask_payload(multi_select=True), _ask_payload(n_questions=2)):
            af.run(payload)
            self.assertNotIn("options", self.posted[-1],
                             "多选/多问题不适合手机多选卡，应降级为二选一（交桌面更稳）")

    def test_non_ask_tool_still_binary(self):
        payload = {"hookEventName": "PermissionRequest", "toolName": "Bash",
                   "tool_name": "Bash", "requestId": "req-test-1", "session_id": "sess_unit",
                   "toolInput": {"command": "git push"}}
        af.run(payload)
        self.assertNotIn("options", self.posted[-1])
        self.assertIn("工具：Bash", self.posted[-1]["desc"])

    def test_connector_down_is_fallback(self):
        with mock.patch.object(af, "_health", lambda port, timeout=2: False):
            self.assertEqual(af.run(_ask_payload()), "fallback")
        self.assertEqual(self.posted, [])


if __name__ == "__main__":
    unittest.main()
