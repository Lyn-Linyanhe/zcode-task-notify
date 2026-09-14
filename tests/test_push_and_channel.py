# -*- coding: utf-8 -*-
"""push_worker 的状态等待 + 通道降级提示单测 —— 零第三方依赖。

这两个用例各自对应一个真实故障（2026-09-14）：
- 耗时/❌ 从不出现：Stop hook 触发时 turn_usage 行还没写入，轮询只判 running 会一次都不等。
- 降级到群机器人后毫无提示：群机器人是单向的，用户会拿它当智能机器人去回复指令。
"""
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


if __name__ == "__main__":
    unittest.main()
