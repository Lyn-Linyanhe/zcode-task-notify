# -*- coding: utf-8 -*-
"""heartbeat.py 单测 —— 零第三方依赖。

盯的是一条真实故障：turn_usage 的行是回合结束后才写入的，所以"行不存在"必须理解为
"回合还在跑"。旧实现按"该会话最新一行"判断，拿到的永远是上一个已结束的回合，
结果 52 次启动 0 次推送（⏳ 长任务提醒从未生效）。
"""
import os
import sys
import time as real_time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import heartbeat as hb  # noqa: E402


class FakeTime:
    """假时钟：sleep 即推进，让整个心跳循环能在毫秒内跑完。"""

    def __init__(self, t=1000.0):
        self.t = t
        self.slept = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s

    def strftime(self, fmt):
        return real_time.strftime(fmt)

    def localtime(self, *a):
        return real_time.localtime(*a)


class TestTurnProgress(unittest.TestCase):
    def test_row_missing_means_still_running(self):
        """行还没落库 = 回合在跑；用 spawn 时刻估算已运行时长。"""
        with mock.patch.object(hb, "turn_row", lambda _t: None):
            finished, elapsed = hb.turn_progress("turn_x", spawned_at=1000.0, now=1000.0 + 25 * 60)
        self.assertFalse(finished)
        self.assertEqual(elapsed, 25)

    def test_terminal_row_means_finished(self):
        with mock.patch.object(hb, "turn_row", lambda _t: ("completed", 1, 2)):
            finished, elapsed = hb.turn_progress("turn_x", 0.0, 100.0)
        self.assertTrue(finished)
        self.assertIsNone(elapsed)

    def test_running_row_uses_row_start_time(self):
        started_ms = 1_700_000_000_000
        now = started_ms / 1000 + 40 * 60
        with mock.patch.object(hb, "turn_row", lambda _t: ("running", started_ms, None)):
            finished, elapsed = hb.turn_progress("turn_x", spawned_at=now, now=now)
        self.assertFalse(finished)
        self.assertEqual(elapsed, 40, "行里有起点时应该用它，而不是 spawn 时刻")

    def test_completed_at_set_counts_as_finished(self):
        with mock.patch.object(hb, "turn_row", lambda _t: ("running", 1, 2)):
            self.assertTrue(hb.turn_progress("turn_x", 0.0, 1.0)[0])

    def test_elapsed_floor_is_one_minute(self):
        with mock.patch.object(hb, "turn_row", lambda _t: None):
            self.assertEqual(hb.turn_progress("turn_x", 0.0, 5.0)[1], 1)

    def test_unreadable_db_degrades_to_still_running(self):
        """库读不到（被占用/路径异常）时按"还在跑"处理，不会误判成回合已结束。"""
        with mock.patch.object(hb, "SESSION_DB", os.path.join(HERE, "no-such-dir", "nope.sqlite")):
            finished, elapsed = hb.turn_progress("turn_x", spawned_at=1000.0, now=1000.0 + 3 * 60)
        self.assertFalse(finished)
        self.assertEqual(elapsed, 3)


class TestMainLoop(unittest.TestCase):
    """端到端跑一遍循环：间隔之前绝不推、到点才推、回合结束即退出。"""

    def _run(self, interval_sec=60, finish_at=1190.0, max_minutes=240):
        clock = FakeTime(1000.0)
        pushes = []
        calls = {"n": 0}

        def fake_progress(_turn_id, spawned_at, now):
            calls["n"] += 1
            if now >= finish_at:
                return True, None
            return False, max(1, int((now - spawned_at) / 60))

        def fake_send(webhook, title, summary):
            pushes.append((clock.time(), title))
            return True, "aibot", "ok"

        argv = ["heartbeat.py", "sess_unittest_hb", "--turn-id", "turn_unittest",
                "--interval-sec", str(interval_sec), "--max-minutes", str(max_minutes)]
        with mock.patch.object(hb, "time", clock), \
                mock.patch.object(hb, "load_json", lambda path, default=None: {"webhook": "http://x"}), \
                mock.patch.object(hb, "acquire_lock", lambda *a: True), \
                mock.patch.object(hb, "hb_log", lambda entry: None), \
                mock.patch.object(hb, "zcode_alive", lambda: True), \
                mock.patch.object(hb, "turn_progress", fake_progress), \
                mock.patch.object(hb, "send_notification", fake_send), \
                mock.patch.object(sys, "argv", argv):
            rc = hb.main()
        return rc, pushes, clock

    def test_first_push_waits_for_full_interval(self):
        """回归守门：旧实现对每条消息都立刻推 ⏳（间隔形同虚设）。"""
        _rc, pushes, _clock = self._run()
        self.assertTrue(pushes, "应当在超过间隔后推送")
        self.assertGreaterEqual(pushes[0][0], 1060.0,
                                "第一次推送必须等满一个间隔（60s），不能一提问就推")
        self.assertIn("⏳ 任务仍在运行", pushes[0][1])

    def test_pushes_repeat_at_interval(self):
        _rc, pushes, _clock = self._run()
        times = [t for t, _ in pushes]
        self.assertEqual(times, [1060.0, 1120.0, 1180.0], f"应按间隔重复推送，实际 {times}")

    def test_exits_when_turn_finishes(self):
        rc, _pushes, _clock = self._run()
        self.assertEqual(rc, 0)

    def test_stops_at_max_duration(self):
        """回合一直不结束（行始终不落库）→ 到上限自灭，不会无限跟踪。"""
        rc, pushes, clock = self._run(interval_sec=60, finish_at=1e12, max_minutes=3)
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(pushes), 4, "到时长上限后应停止推送并退出")
        self.assertLess(clock.t, 1000 + 300, "应在 3 分钟内退出，而不是继续空转")

    def test_desktop_gone_notifies_abnormal_stop(self):
        """回合未结束而桌面端进程消失 = 异常中止：推 🛑 告知再退出。
        Stop hook 在进程死亡时不会触发，这是用户能收到"任务没跑完就没了"的唯一机会。"""
        clock = FakeTime(1000.0)
        pushes = []
        argv = ["heartbeat.py", "sess_x", "--turn-id", "turn_y", "--interval-sec", "60"]

        def capture(webhook, title, summary):
            pushes.append((title, summary))
            return True, "aibot", "ok"

        with mock.patch.object(hb, "time", clock), \
                mock.patch.object(hb, "load_json", lambda path, default=None: {"webhook": "http://x"}), \
                mock.patch.object(hb, "acquire_lock", lambda *a: True), \
                mock.patch.object(hb, "hb_log", lambda entry: None), \
                mock.patch.object(hb, "zcode_alive", lambda: False), \
                mock.patch.object(hb, "turn_progress", lambda *a: (False, 30)), \
                mock.patch.object(hb, "get_session_title", lambda s: "测试会话"), \
                mock.patch.object(hb, "send_notification", capture), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(hb.main(), 0)
        self.assertEqual(len(pushes), 1, "应恰好推送一条异常中止告知")
        title, summary = pushes[0]
        self.assertIn("🛑 桌面端已关闭", title)
        self.assertIn("中止时已运行 30 分钟", summary)

    def test_desktop_transient_blip_keeps_tracking(self):
        """桌面端短暂消失又恢复（如快速重启）→ 不推中止、继续跟踪。"""
        clock = FakeTime(1000.0)
        alive = {"calls": 0}

        def zcode():
            alive["calls"] += 1
            return alive["calls"] > 1   # 第一次 tick 掉线，之后恢复

        progress = {"n": 0}

        def fake_progress(*a):
            progress["n"] += 1
            if progress["n"] >= 3:
                return True, None          # 第 3 拍回合结束
            return False, 5 * progress["n"]  # 仍在跑（finished=False 必须给分钟数）

        argv = ["heartbeat.py", "sess_x", "--turn-id", "turn_y", "--interval-sec", "60"]
        with mock.patch.object(hb, "time", clock), \
                mock.patch.object(hb, "load_json", lambda path, default=None: {"webhook": "http://x"}), \
                mock.patch.object(hb, "acquire_lock", lambda *a: True), \
                mock.patch.object(hb, "hb_log", lambda entry: None), \
                mock.patch.object(hb, "zcode_alive", zcode), \
                mock.patch.object(hb, "turn_progress", fake_progress), \
                mock.patch.object(hb, "send_notification",
                                  side_effect=AssertionError("瞬时抖动不该推中止")), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(hb.main(), 0)

    def test_finished_turn_not_reported_as_aborted_even_if_desktop_gone(self):
        """检查顺序守门：回合已结束 + 桌面端恰好也关了 → 正常退出，
        绝不能误报"中止"（结束检查必须先于存活检查）。"""
        clock = FakeTime(1000.0)
        argv = ["heartbeat.py", "sess_x", "--turn-id", "turn_y", "--interval-sec", "60"]
        with mock.patch.object(hb, "time", clock), \
                mock.patch.object(hb, "load_json", lambda path, default=None: {"webhook": "http://x"}), \
                mock.patch.object(hb, "acquire_lock", lambda *a: True), \
                mock.patch.object(hb, "hb_log", lambda entry: None), \
                mock.patch.object(hb, "zcode_alive", lambda: False), \
                mock.patch.object(hb, "turn_progress", lambda *a: (True, None)), \
                mock.patch.object(hb, "send_notification",
                                  side_effect=AssertionError("已完成的回合不该报中止")), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(hb.main(), 0)

    def test_missing_turn_id_exits_quietly(self):
        """payload 里没有回合号 → 宁可不推，也不拿上一轮的状态骗人。"""
        argv = ["heartbeat.py", "sess_x"]
        with mock.patch.object(hb, "load_json", lambda path, default=None: {"webhook": "http://x"}), \
                mock.patch.object(hb, "hb_log", lambda entry: None), \
                mock.patch.object(hb, "acquire_lock",
                                  side_effect=AssertionError("没有回合号不应走后续流程")), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(hb.main(), 0)


if __name__ == "__main__":
    unittest.main()
