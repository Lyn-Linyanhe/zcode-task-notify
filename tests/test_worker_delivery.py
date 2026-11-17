"""worker 实际组装链的离线回归，网络和发送端均被拦截。"""
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
import push_worker as pw  # noqa: E402


class TestWorkerDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payload_path = os.path.join(self.tmp.name, "payload.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.write_json(self.config_path, {
            "webhook": "https://example.invalid/hook", "use_llm_summary": True,
            "llm_api_format": "openai", "llm_api_key": "test-key",
            "llm_base_url": "https://example.invalid", "llm_model": "test",
        })
        self.write_json(self.state_path, {"enabled": True})
        self.requests = []

    def write_json(self, path, value):
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)

    def run_worker(self, payload, model_content="已完成报告", after_model=None, after_wait=None,
                   after_title=None):
        self.write_json(self.payload_path, payload)

        def respond(request, timeout=None):
            self.requests.append(json.loads(request.data))
            if after_model:
                after_model()
            response = mock.Mock()
            response.read.return_value = json.dumps({
                "choices": [{"message": {"content": model_content}}]
            }).encode("utf-8")
            return response

        def turn_status(*args, **kwargs):
            if after_wait:
                after_wait()
            return "completed", None, 120000

        def title(*args):
            if after_title:
                after_title()
            return "通知测试"

        with mock.patch.object(sys, "argv", ["push_worker.py", self.payload_path]), \
                mock.patch.object(pw, "CONFIG_PATH", self.config_path), \
                mock.patch.object(pw, "STATE_PATH", self.state_path), \
                mock.patch.object(pw, "bot_session_ids", return_value=set()), \
                mock.patch.object(pw, "is_background_session", return_value=False), \
                mock.patch.object(pw, "turn_status", side_effect=turn_status), \
                mock.patch.object(pw, "get_session_title", side_effect=title), \
                mock.patch.object(pw, "log"), \
                mock.patch.object(pw.urllib.request, "urlopen", side_effect=respond), \
                mock.patch.object(pw, "send_notification", return_value=(True, "test", "ok")) as send:
            self.assertEqual(pw.main(), 0)
        return send

    def test_complete_reply_is_used_instead_of_preview(self):
        text = "已完成论文精读，产出精读版PPT和拓展版PPT。"
        send = self.run_worker({
            "hookEventName": "Stop", "sessionId": "test", "turnId": "turn_test",
            "responsePreview": "先检查论文结构。", "responseText": text,
        }, model_content="")
        body = send.call_args.args[2]
        self.assertIn("精读版PPT", body)
        self.assertIn("拓展版PPT", body)
        self.assertIn(text, self.requests[0]["messages"][0]["content"])
        self.assertIn("通知测试", send.call_args.args[1])
        self.assertIn("耗时 2 分钟", send.call_args.args[1])

    def test_preview_remains_fallback_when_complete_reply_blank(self):
        text = "已生成年度报告.xlsx。"
        send = self.run_worker({
            "hookEventName": "Stop", "sessionId": "test", "turnId": "turn_test",
            "responsePreview": text, "responseText": " \n ",
        }, model_content="")
        self.assertIn("年度报告.xlsx", send.call_args.args[2])
        self.assertIn(text, self.requests[0]["messages"][0]["content"])

    def test_mute_while_generating_summary_prevents_delivery(self):
        for state in ({"enabled": False}, {"enabled": True, "mute_until": time.time() + 3600}):
            with self.subTest(state=state):
                self.write_json(self.state_path, {"enabled": True})
                send = self.run_worker({
                    "hookEventName": "Stop", "sessionId": "test", "turnId": "turn_test",
                    "responseText": "已生成年度报告.xlsx。",
                }, after_model=lambda: self.write_json(self.state_path, state))
                send.assert_not_called()

    def test_mute_during_turn_status_wait_prevents_delivery(self):
        send = self.run_worker({
            "hookEventName": "Stop", "sessionId": "test", "turnId": "turn_test",
            "responseText": "已生成年度报告.xlsx。",
        }, after_wait=lambda: self.write_json(self.state_path, {"enabled": False}))
        send.assert_not_called()

    def test_permission_fallback_checks_mute_before_delivery(self):
        send = self.run_worker({"hookEventName": "PermissionRequest", "sessionId": "test"},
                               after_title=lambda: self.write_json(self.state_path, {"enabled": False}))
        send.assert_not_called()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
