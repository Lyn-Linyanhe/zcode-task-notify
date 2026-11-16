"""内部子会话静默，普通主会话和权限审批不受影响；测试不发送通知。"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)
import notify as nt
import push_worker as pw
import heartbeat_spawn as hs
import heartbeat as hb


class TestSessionFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "sessions.sqlite")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, task_type TEXT, parent_id TEXT)")
            con.executemany("INSERT INTO session VALUES (?,?,?)", [
                ("main", "interactive", None),
                ("branch", "interactive", "main"),
                ("side", "selection_side_chat", "main"),
                ("child", "subagent_child", "main"),
                ("workflow", "workflow_child", "main"),
            ])
        patch = mock.patch.object(nt, "SESSION_DB", self.db)
        patch.start()
        self.addCleanup(patch.stop)

    def test_type_not_parent_or_name_determines_filter(self):
        for sid in ("side", "child", "workflow", "sess_subagent_unknown"):
            self.assertTrue(nt.is_background_session(sid), sid)
        for sid in ("main", "branch", "missing", "", "session_desktop"):
            self.assertFalse(nt.is_background_session(sid), sid)

    def test_missing_db_is_not_created(self):
        path = os.path.join(self.tmp.name, "absent.sqlite")
        with mock.patch.object(nt, "SESSION_DB", path):
            self.assertFalse(nt.is_background_session("main"))
        self.assertFalse(os.path.exists(path))

    def test_old_schema_preserves_normal_notifications(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DROP TABLE session")
            con.execute("CREATE TABLE session (id TEXT)")
        self.assertFalse(nt.is_background_session("main"))
        self.assertTrue(nt.is_background_session("sess_subagent_legacy"))

    def test_stop_hook_exits_before_spawn(self):
        for key in ("session_id", "sessionId"):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    {"hookEventName": "Stop", key: "side"}))), \
                    mock.patch.object(nt, "log"), \
                    mock.patch.object(nt.os, "makedirs") as dirs, \
                    mock.patch.object(nt.subprocess, "Popen") as spawn:
                self.assertEqual(nt.main(), 0)
                spawn.assert_not_called()
                dirs.assert_not_called()

    def test_worker_filters_queued_stop_but_keeps_parent_and_permission(self):
        with mock.patch.object(pw, "load_json", return_value={"enabled": True, "webhook": "test"}), \
                mock.patch.object(pw, "bot_session_ids", return_value=set()), \
                mock.patch.object(pw, "turn_status", return_value=("completed", None, 60000)) as status, \
                mock.patch.object(pw, "get_session_title", return_value="Example"), \
                mock.patch.object(pw, "log"):
            self.assertEqual(pw.process_payload({"hookEventName": "Stop", "sessionId": "side"})["action"], "skip")
            status.assert_not_called()
            for sid in ("main", "branch"):
                self.assertEqual(pw.process_payload({"hookEventName": "Stop", "sessionId": sid})["action"], "push")
            for sid in ("child", "side", "sess_subagent_legacy"):
                self.assertEqual(pw.process_payload({"hookEventName": "PermissionRequest", "sessionId": sid})["action"], "push")

    def test_permission_hook_still_waits_for_explicit_decision(self):
        approval = mock.Mock()
        approval.run.return_value = {"behavior": "deny", "message": "rejected"}
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                {"hookEventName": "PermissionRequest", "sessionId": "child"}))), \
                mock.patch.dict(sys.modules, {"approve_flow": approval}), \
                mock.patch.object(nt, "_probe_permission_payload"), \
                mock.patch.object(nt, "load_json", return_value={"enabled": True}), \
                mock.patch.object(nt, "log"), mock.patch.object(sys, "stdout", output):
            self.assertEqual(nt.main(), 0)
        approval.run.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_child_prompt_keeps_maintenance_but_skips_heartbeat(self):
        with mock.patch.object(sys, "stdin", io.StringIO('{"sessionId":"side"}')), \
                mock.patch.object(hs, "self_heal_config") as heal, \
                mock.patch.object(hs, "ensure_connector") as connector, \
                mock.patch.object(hs.subprocess, "Popen") as spawn:
            self.assertEqual(hs.main(), 0)
            heal.assert_called_once_with("side")
            connector.assert_called_once_with()
            spawn.assert_not_called()

    def test_heartbeat_filter_with_valid_channel_and_turn(self):
        for sid, expected_lock_calls in (("side", 0), ("branch", 1)):
            with self.subTest(sid=sid), \
                    mock.patch.object(sys, "argv", ["heartbeat.py", sid, "--turn-id", "turn_test"]), \
                    mock.patch.object(hb, "load_json", return_value={"webhook": "test", "enabled": True}), \
                    mock.patch.object(hb, "is_bot_session", return_value=False), \
                    mock.patch.object(hb, "acquire_lock", return_value=False) as lock, \
                    mock.patch.object(hb, "hb_log"), \
                    mock.patch.object(hb, "send_notification") as send:
                self.assertEqual(hb.main(), 0)
                self.assertEqual(lock.call_count, expected_lock_calls)
                send.assert_not_called()

    def test_locked_database_recovers_without_cached_misclassification(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("BEGIN EXCLUSIVE")
            try:
                self.assertFalse(nt.is_background_session("side"))
            finally:
                con.rollback()
        self.assertTrue(nt.is_background_session("side"))

    def test_worker_rechecks_type_after_summary(self):
        payload = {"hookEventName": "Stop", "sessionId": "late", "responseText": "done"}
        path = os.path.join(self.tmp.name, "payload.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)

        def summary(*args):
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("INSERT INTO session VALUES ('late','selection_side_chat','main')")
            return "done"

        def load(path_arg, default):
            return payload if path_arg == path else {"enabled": True, "webhook": "test"}

        with mock.patch.object(sys, "argv", ["push_worker.py", path]), \
                mock.patch.object(pw, "load_json", side_effect=load), \
                mock.patch.object(pw, "bot_session_ids", return_value=set()), \
                mock.patch.object(pw, "turn_status", return_value=("completed", None, 10000)), \
                mock.patch.object(pw, "get_session_title", return_value="Example"), \
                mock.patch.object(pw, "llm_summary", side_effect=summary), \
                mock.patch.object(pw, "log"), \
                mock.patch.object(pw, "send_notification") as send:
            self.assertEqual(pw.main(), 0)
            send.assert_not_called()

    def test_heartbeat_rechecks_type_before_progress_or_abort(self):
        for alive in (True, False):
            with self.subTest(alive=alive), \
                    mock.patch.object(sys, "argv", ["heartbeat.py", "late", "--turn-id", "turn_test", "--interval-sec", "1"]), \
                    mock.patch.object(hb, "load_json", return_value={"enabled": True, "webhook": "test"}), \
                    mock.patch.object(hb, "is_bot_session", return_value=False), \
                    mock.patch.object(hb, "is_background_session", side_effect=[False, True]) as classify, \
                    mock.patch.object(hb, "acquire_lock", return_value=True), \
                    mock.patch.object(hb, "turn_progress", return_value=(False, 1)), \
                    mock.patch.object(hb, "zcode_alive", return_value=alive), \
                    mock.patch.object(hb, "time") as clock, \
                    mock.patch.object(hb, "hb_log"), \
                    mock.patch.object(hb, "send_notification") as send:
                clock.time.side_effect = [100, 102]
                self.assertEqual(hb.main(), 0)
                self.assertEqual(classify.call_count, 2)
                send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
