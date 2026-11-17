"""通知质量回归：模拟时钟、状态和发送端，不触发真实通知。"""
import os
import sys
import unittest
from unittest import mock

SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import notify as nt
import heartbeat as hb
from test_heartbeat import FakeTime


class TestSummaryAlignment(unittest.TestCase):
    def test_filtered_opening_does_not_shift_artifact_heading(self):
        text = ("好的，我来处理。\n**交付物：年度市场分析报告.xlsx**\n"
                "后续说明文字足够长，包含一些不应优先的过程。")
        self.assertIn("年度市场分析报告.xlsx", nt.make_summary(text))

    def test_filtered_code_does_not_shift_artifact_heading(self):
        text = ("const x = 1;\n**交付物：年度市场分析报告.xlsx**\n"
                "后续说明文字足够长，包含一些不应优先的过程。")
        self.assertIn("年度市场分析报告.xlsx", nt.make_summary(text))


class TestLiveHeartbeatState(unittest.TestCase):
    def run_loop(self, state_at, finish_at, desktop_alive=True):
        clock = FakeTime()
        sent = []

        def load(path, default=None):
            if path == hb.STATE_PATH:
                return state_at(clock.t)
            return {"webhook": "https://example.invalid/hook"}

        def progress(turn, start, now):
            return (True, None) if now >= finish_at else (False, 1)

        def send(*args):
            sent.append(clock.t)
            return True, "test", "ok"

        with mock.patch.object(hb, "time", clock), \
                mock.patch.object(hb, "load_json", side_effect=load), \
                mock.patch.object(hb, "is_bot_session", return_value=False), \
                mock.patch.object(hb, "is_background_session", return_value=False), \
                mock.patch.object(hb, "acquire_lock", return_value=True), \
                mock.patch.object(hb, "hb_log"), \
                mock.patch.object(hb, "get_session_title", return_value="test"), \
                mock.patch.object(hb, "turn_progress", side_effect=progress), \
                mock.patch.object(hb, "zcode_alive", return_value=desktop_alive), \
                mock.patch.object(hb, "send_notification", side_effect=send), \
                mock.patch.object(sys, "argv", ["heartbeat.py", "test", "--turn-id", "turn_test", "--interval-sec", "20"]):
            self.assertEqual(hb.main(), 0)
        return sent

    def test_mute_after_start_suppresses_progress_and_resume_keeps_interval(self):
        def state(now):
            return {"enabled": not (1010 <= now < 1050)}
        self.assertEqual(self.run_loop(state, 1070), [1060])

    def test_timed_mute_expires_while_heartbeat_keeps_tracking(self):
        def state(now):
            return {"enabled": True, "mute_until": 1050 if now >= 1010 else 0}
        self.assertEqual(self.run_loop(state, 1070), [1060])

    def test_mute_during_shutdown_check_suppresses_abort(self):
        self.assertEqual(self.run_loop(lambda now: {"enabled": now < 1015}, 2000, False), [])

    def test_turn_finishes_during_shutdown_check_no_abort(self):
        self.assertEqual(self.run_loop(lambda now: {"enabled": True}, 1020, False), [])


if __name__ == "__main__":
    unittest.main()
