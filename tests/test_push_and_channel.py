# -*- coding: utf-8 -*-
"""push_worker 的状态等待 + 通道降级提示单测 —— 零第三方依赖。

这两个用例各自对应一个真实故障（2026-09-14）：
- 耗时/❌ 从不出现：Stop hook 触发时 turn_usage 行还没写入，轮询只判 running 会一次都不等。
- 降级到群机器人后毫无提示：群机器人是单向的，用户会拿它当智能机器人去回复指令。
"""
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import notify as nt          # noqa: E402
import push_worker as pw     # noqa: E402


class TestTurnStatusWait(unittest.TestCase):
    def test_waits_until_row_appears_then_returns_duration(self):
        """行先缺失（None）后出现 completed —— 必须继续等，不能一次都不等。"""
        seq = [(None, None, None), (None, None, None), ("completed", None, 581306)]
        calls = []

        def fake(turn_id):
            calls.append(turn_id)
            return seq[min(len(calls) - 1, len(seq) - 1)]

        with mock.patch.object(pw, "_query_turn", fake), mock.patch("time.sleep"):
            status, _err, dur = pw.turn_status("turn_x", wait_s=5)
        self.assertEqual(status, "completed")
        self.assertEqual(dur, 581306)
        self.assertGreaterEqual(len(calls), 3, "应在行出现前持续轮询")

    def test_missing_row_with_zero_wait_returns_immediately(self):
        with mock.patch.object(pw, "_query_turn", lambda _t: (None, None, None)):
            t0 = time.time()
            status, _err, dur = pw.turn_status("turn_x", wait_s=0)
        self.assertIsNone(status)
        self.assertIsNone(dur)
        self.assertLess(time.time() - t0, 1.0, "wait_s=0 不应等待")

    def test_terminal_row_is_not_polled_twice(self):
        calls = []

        def fake(turn_id):
            calls.append(turn_id)
            return ("error", "UNKNOWN_ERROR", 46247)

        with mock.patch.object(pw, "_query_turn", fake), mock.patch("time.sleep"):
            status, err, dur = pw.turn_status("turn_x", wait_s=5)
        self.assertEqual(status, "error")
        self.assertEqual(err, "UNKNOWN_ERROR")
        self.assertEqual(len(calls), 1, "已终态不该继续轮询")

    def test_empty_turn_id_short_circuits(self):
        with mock.patch.object(pw, "_query_turn", side_effect=AssertionError("不该查库")):
            self.assertEqual(pw.turn_status("", wait_s=5), (None, None, None))


class TestFormatDuration(unittest.TestCase):
    def test_too_short_is_hidden(self):
        self.assertEqual(pw.format_duration(5162), "")      # 5 秒，不值得标
        self.assertEqual(pw.format_duration(None), "")
        self.assertEqual(pw.format_duration("bad"), "")

    def test_ten_seconds_shows_seconds(self):
        self.assertEqual(pw.format_duration(12000), "12 秒")

    def test_minutes_and_hours(self):
        self.assertEqual(pw.format_duration(581306), "10 分钟")
        self.assertEqual(pw.format_duration(2468176), "41 分钟")
        self.assertEqual(pw.format_duration(17465712), "4 小时 51 分钟")


class TestDegradedChannelNotice(unittest.TestCase):
    def test_webhook_fallback_states_reason_and_one_way(self):
        sent = {}

        def fake_wecom(webhook, title, summary):
            sent["webhook"], sent["title"], sent["summary"] = webhook, title, summary
            return True, '{"errcode":0}'

        with mock.patch.object(nt, "_try_aibot", lambda t, s: (False, "连接器离线")), \
                mock.patch.object(nt, "send_wecom", fake_wecom):
            ok, channel, _ = nt.send_notification("https://example.invalid/hook", "✅ 任务完成", "正文内容")

        self.assertTrue(ok)
        self.assertEqual(channel, "webhook")
        self.assertIn("正文内容", sent["summary"], "降级提示不能吃掉原正文")
        self.assertIn("通道降级", sent["summary"])
        self.assertIn("连接器离线", sent["summary"], "要写明降级原因")
        self.assertIn("单向", sent["summary"], "要提醒群机器人回复无效")

    def test_unbound_user_reason_is_surfaced(self):
        captured = {}

        def fake_wecom(webhook, title, summary):
            captured["summary"] = summary
            return True, "{}"

        with mock.patch.object(nt, "_try_aibot",
                               lambda t, s: (False, "未绑定用户（先在微信里给机器人发一条消息）")), \
                mock.patch.object(nt, "send_wecom", fake_wecom):
            nt.send_notification("https://example.invalid/hook", "t", "b")
        self.assertIn("未绑定用户", captured["summary"])

    def test_aibot_path_has_no_notice_and_no_webhook(self):
        with mock.patch.object(nt, "_try_aibot", lambda t, s: (True, "ok")), \
                mock.patch.object(nt, "send_wecom", side_effect=AssertionError("aibot 成功时不该走 webhook")):
            ok, channel, detail = nt.send_notification("https://example.invalid/hook", "t", "b")
        self.assertEqual((ok, channel, detail), (True, "aibot", "ok"))

    def test_no_channel_reports_failure(self):
        with mock.patch.object(nt, "_try_aibot", lambda t, s: (False, "连接器离线")), \
                mock.patch.object(nt, "log_failure"):
            ok, channel, _ = nt.send_notification("", "t", "b")
        self.assertFalse(ok)
        self.assertEqual(channel, "none")


class TestMuteAndGate(unittest.TestCase):
    """「静默 30」这类定时静默与开关判定。"""

    def test_parse_mute_minutes(self):
        self.assertEqual(nt.parse_mute_minutes("静默"), 0)
        self.assertEqual(nt.parse_mute_minutes("静默 30"), 30)
        self.assertEqual(nt.parse_mute_minutes("静默30分钟"), 30)
        self.assertEqual(nt.parse_mute_minutes("静默  5 分"), 5)
        self.assertEqual(nt.parse_mute_minutes("静默 99999"), nt.MAX_MUTE_MINUTES, "要有上限防手误")
        self.assertIsNone(nt.parse_mute_minutes("取消静默"))
        self.assertIsNone(nt.parse_mute_minutes("状态"))
        self.assertIsNone(nt.parse_mute_minutes(""))

    def test_notifications_enabled_variants(self):
        now = 1000.0
        self.assertEqual(nt.notifications_enabled({"enabled": True}, now), (True, "on"))
        self.assertEqual(nt.notifications_enabled({"enabled": False}, now), (False, "switch off"))
        self.assertEqual(
            nt.notifications_enabled({"enabled": True, "mute_until": now + 60}, now), (False, "muted"))
        self.assertEqual(
            nt.notifications_enabled({"enabled": True, "mute_until": now - 1}, now), (True, "on"),
            "定时静默过期后应自动恢复，无需任何清理动作")

    def test_mute_remaining_minutes(self):
        now = 1000.0
        self.assertEqual(nt.mute_remaining_min({"enabled": True, "mute_until": now + 610}, now), 11)
        self.assertIsNone(nt.mute_remaining_min({"enabled": True, "mute_until": now - 1}, now))
        self.assertIsNone(nt.mute_remaining_min({"enabled": False, "mute_until": now + 600}, now),
                          "手动静默没有到期时间")

    def test_sender_allowed(self):
        self.assertTrue(nt.sender_allowed("u1", "u1", "single"))
        self.assertTrue(nt.sender_allowed("u1", "u1", "group"), "主人从群里发指令也算数")
        self.assertFalse(nt.sender_allowed("u2", "u1", "single"), "别人发的必须拒")
        self.assertTrue(nt.sender_allowed("u9", None, "single"), "未绑定时单聊首人可建立绑定")
        self.assertFalse(nt.sender_allowed("u9", None, "group"), "群聊无法确认归属，一律先拒")
        self.assertFalse(nt.sender_allowed("", "u1", "single"))

    def test_mute_for_minutes_keeps_enabled_true(self):
        """定时静默只写 mute_until，不能顺手把 enabled 置 false——否则会被当成手动静默，
        剩余时间拿不到、到点也不会自动恢复（2026-09-14 实测踩到）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"enabled": true}')
            with mock.patch.object(nt, "STATE_PATH", path):
                nt.mute_for_minutes(30, now=1000.0)
                state = json.load(open(path, encoding="utf-8"))
                self.assertTrue(state["enabled"], "enabled 必须保持 true")
                self.assertEqual(state["mute_until"], 1000.0 + 1800)
                self.assertEqual(nt.notifications_enabled(state, now=1000.0), (False, "muted"))
                self.assertEqual(nt.mute_remaining_min(state, now=1000.0), 30)
                # 到点自动恢复，无需任何清理
                self.assertEqual(nt.notifications_enabled(state, now=1000.0 + 1801), (True, "on"))


class TestSafeTaskId(unittest.TestCase):
    """task_id 直接拼进 decisions/<task_id>.json 落盘，旧代码只查前缀 → 路径穿越（审计 P1）。"""

    def test_accepts_normal_ids(self):
        self.assertEqual(nt.safe_task_id("zc-perm_d964066e-f806-4812"), "zc-perm_d964066e-f806-4812")
        self.assertEqual(nt.safe_task_id("zc-test-1"), "zc-test-1")

    def test_rejects_traversal_and_junk(self):
        for bad in ("zc-../../state", "zc-..\\..\\x", "zc-", "zc-" + "A" * 200,
                    "zc-a/b", "zc-a b", "notzc-x", "", None, 123):
            self.assertIsNone(nt.safe_task_id(bad), f"应拒绝 {bad!r}")


class TestAtomicWrite(unittest.TestCase):
    def test_replaces_file_without_leaving_partial(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            nt._atomic_write_json(path, {"enabled": True, "n": 1})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["n"], 1)
            nt._atomic_write_json(path, {"enabled": False, "n": 2})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["n"], 2)
            self.assertFalse(os.path.exists(path + ".tmp"), "临时文件应被 replace 掉")


class TestStopTitleNeutral(unittest.TestCase):
    """状态未确认时不得冒充"完成"（审计 P1：error 迟落库被报成 ✅ 恰是最需要通知的场景）。"""

    def _title_with_status(self, status, err=None, dur=60000):
        payload = {"hookEventName": "Stop", "session_id": "sess_x", "turnId": "turn_x",
                   "responsePreview": "结果内容"}
        with mock.patch.object(pw, "turn_status", lambda *a, **k: (status, err, dur)), \
                mock.patch.object(pw, "notifications_enabled", lambda s: (True, "on")), \
                mock.patch.object(pw, "load_json", lambda p, d=None: {"webhook": "x"}), \
                mock.patch.object(pw, "bot_session_ids", lambda: set()), \
                mock.patch.object(pw, "get_session_title", lambda s: s), \
                mock.patch.object(pw, "log", lambda e: None):
            return pw.process_payload(payload)

    def test_error_title_carries_error_code(self):
        title = self._title_with_status("error", "MODEL_ERROR")["title"]
        self.assertIn("❌", title)
        self.assertIn("MODEL_ERROR", title)

    def test_completed_title(self):
        self.assertIn("✅", self._title_with_status("completed")["title"])

    def test_unknown_status_is_neutral_not_completed(self):
        title = self._title_with_status(None)["title"]
        self.assertNotIn("✅", title)
        self.assertIn("未确认", title)

    def test_cancelled_skipped(self):
        self.assertEqual(self._title_with_status("cancelled")["action"], "skip")


class TestTryAibotReason(unittest.TestCase):
    """降级原因要区分"在线但拒绝"与"真离线"（审计 P2 文案错位），且必须带本地密钥头。"""

    def _cfg(self):
        return {"bot_id": "b", "target_userid": "u", "local_port": 17899, "local_token": "tk"}

    def _run(self, urlopen_side):
        with mock.patch.object(nt.os.path, "exists", lambda p: True), \
                mock.patch.object(nt, "load_json", lambda p, d=None: self._cfg()), \
                mock.patch.object(nt.urllib.request, "urlopen", urlopen_side):
            return nt._try_aibot("t", "s")

    def test_http_error_reports_online_but_rejected(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {},
                                         io.BytesIO(b'{"error": "ws offline"}'))

        ok, reason = self._run(boom)
        self.assertFalse(ok)
        self.assertIn("在线但未送达", reason)
        self.assertIn("ws offline", reason)

    def test_connection_error_reports_offline(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        self.assertEqual(self._run(boom), (False, "连接器离线"))

    def test_sends_token_header(self):
        seen = {}

        class FakeResp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            seen["token"] = req.get_header("X-zcn-token")
            return FakeResp()

        ok, _ = self._run(fake)
        self.assertTrue(ok)
        self.assertEqual(seen["token"], "tk", "必须带本地共享密钥头")


if __name__ == "__main__":
    unittest.main()
