# -*- coding: utf-8 -*-
"""「在跑」功能单测：running_sessions() 读心跳锁 + 指令文案。零第三方依赖。

对应真实需求（2026-09-14）：用户在微信发一个词，回"目前有哪几个会话在跑、各跑了多久"。
数据源是心跳锁（进程活着=回合还在跑），不能用 turn_usage——那表只有回合结束后才写入。
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import notify  # noqa: E402


class TestRunningSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(HERE, "_tmp_locks")
        os.makedirs(self.tmp, exist_ok=True)
        self._real_base = notify.BASE
        notify.BASE = self.tmp

    def tearDown(self):
        notify.BASE = self._real_base
        for n in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, n))
        os.rmdir(self.tmp)

    def _lock(self, name, data):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_lists_only_live_new_format_locks(self):
        now = time.time()
        self._lock("hb_lock_a.lock", {"pid": 111, "started": now - 3600,
                                      "session_id": "sess_a", "turn_id": "turn_a"})
        self._lock("hb_lock_b.lock", {"pid": 222, "started": now - 120,
                                      "session_id": "sess_b", "turn_id": "turn_b"})
        self._lock("hb_lock_dead.lock", {"pid": 333, "started": now - 60,
                                         "session_id": "sess_dead", "turn_id": "t"})
        self._lock("hb_lock_old.lock", {"pid": 444, "started": now})   # 旧格式：无 session_id
        self._lock("unrelated.txt", {"session_id": "x"})               # 非锁文件

        alive = {111, 222}
        with mock.patch.object(notify, "_pid_alive", lambda pid: pid in alive), \
                mock.patch.object(notify, "get_session_title", lambda sid: sid + "-标题"):
            rows = notify.running_sessions()

        ids = [r["session_id"] for r in rows]
        self.assertEqual(ids, ["sess_a", "sess_b"], "只列活进程的新格式锁，按已跑时长降序")
        self.assertEqual(rows[0]["elapsed_min"], 60)     # 3600s
        self.assertEqual(rows[1]["elapsed_min"], 2)      # 120s
        self.assertEqual(rows[0]["title"], "sess_a-标题")

    def test_empty_when_no_locks(self):
        self.assertEqual(notify.running_sessions(), [])

    def test_elapsed_floor_one_minute(self):
        now = time.time()
        self._lock("hb_lock_x.lock", {"pid": 1, "started": now - 5,
                                      "session_id": "sess_x", "turn_id": "t"})
        with mock.patch.object(notify, "_pid_alive", lambda pid: True), \
                mock.patch.object(notify, "get_session_title", lambda sid: sid):
            rows = notify.running_sessions()
        self.assertEqual(rows[0]["elapsed_min"], 1, "刚起步不足一分钟也显示 1 分钟")

    def test_corrupt_lock_is_skipped_not_fatal(self):
        with open(os.path.join(self.tmp, "hb_lock_bad.lock"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        with mock.patch.object(notify, "_pid_alive", lambda pid: True):
            self.assertEqual(notify.running_sessions(), [])


@unittest.skipUnless(
    os.environ.get("ZCODE_NOTIFY_SCRIPTS"),
    "「在跑」指令文案需要真实安装（含 SDK）")
class TestRunningCommand(unittest.TestCase):
    def test_running_text_lists_sessions(self):
        import aibot_connector as ac
        rows = [{"session_id": "s1", "title": "写周报", "elapsed_min": 5, "turn_id": "t"},
                {"session_id": "s2", "title": "跑数据", "elapsed_min": 75, "turn_id": "t2"}]
        with mock.patch.object(ac, "running_sessions", lambda: rows):
            text = ac._running_text()
        self.assertIn("正在运行 2 个会话", text)
        self.assertIn("写周报（已跑 5 分钟）", text)
        self.assertIn("跑数据（已跑 1 小时 15 分）", text)

    def test_running_text_empty_case(self):
        import aibot_connector as ac
        with mock.patch.object(ac, "running_sessions", lambda: []):
            self.assertIn("没有在运行", ac._running_text())

    def test_command_aliases_route_to_running(self):
        import aibot_connector as ac
        with mock.patch.object(ac, "running_sessions", lambda: []):
            for alias in ("在跑", "运行中", "正在跑", "有哪些会话", "running"):
                self.assertIn("没有在运行", ac.handle_command(alias), alias)

    def test_help_mentions_running(self):
        import aibot_connector as ac
        self.assertIn("在跑", ac.handle_command("帮助"))


if __name__ == "__main__":
    unittest.main()
