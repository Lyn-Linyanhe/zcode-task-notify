# -*- coding: utf-8 -*-
"""heartbeat_spawn 的看门狗单测 —— 零第三方依赖。

对应 2026-09-16 实测到的真实故障：连接器进程活着、HTTP 端口正常响应，但 WebSocket
已经断开（服务器主动断线，SDK 未自动重连）。旧看门狗只判"HTTP 通不通"，于是永远
认为在线、永不重启 —— 现象是推送一路降级到群 webhook、卡片与微信指令全失效，
40 分钟无人自愈，只能手动重启。

看门狗必须同时看 /health 的 connected 字段，并在重启前清掉假死的旧进程
（否则新实例会因单例锁直接退出）。
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

import heartbeat_spawn as hs  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b


class TestEnsureConnector(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(HERE, "_tmp_spawn")
        os.makedirs(self.tmp, exist_ok=True)
        self._base = hs.BASE
        hs.BASE = self.tmp
        self.cfg_path = os.path.join(self.tmp, "aibot_config.json")
        self.venv = os.path.join(self.tmp, "venv", "python.exe")
        os.makedirs(os.path.dirname(self.venv), exist_ok=True)
        open(self.venv, "w").close()
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"local_port": 17899, "venv_python": self.venv}, f)
        self.logs = []
        self.spawned = []
        self.killed = []

    def tearDown(self):
        hs.BASE = self._base
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, health):
        import urllib.request as urlreq

        def fake_urlopen(url, timeout=None):
            if isinstance(health, Exception):
                raise health
            return _Resp(health)

        with mock.patch.object(hs, "log", self.logs.append), \
                mock.patch.object(hs.subprocess, "Popen",
                                  lambda *a, **k: self.spawned.append(a[0])), \
                mock.patch.object(hs, "_kill_stale_connector",
                                  lambda port: self.killed.append(port)), \
                mock.patch.object(urlreq, "urlopen", fake_urlopen):
            hs.ensure_connector()

    def test_healthy_connector_is_left_alone(self):
        """在线且已绑定 → 不重启、不杀进程。"""
        self._run({"ok": True, "connected": True, "target": True})
        self.assertEqual(self.spawned, [], "健康连接器不该被重启")
        self.assertEqual(self.killed, [], "健康连接器不该被杀")

    def test_ws_offline_triggers_restart(self):
        """HTTP 活着但 ws 掉线 —— 这是旧看门狗漏掉的关键场景。"""
        self._run({"ok": True, "connected": False, "target": True})
        self.assertEqual(len(self.spawned), 1, "ws 掉线必须重启连接器")
        self.assertEqual(self.killed, [17899], "重启前必须清掉假死的旧进程")

    def test_unbound_target_triggers_restart(self):
        """连上了但没捕获到绑定用户 → 也要重启（否则推送发不出去）。"""
        self._run({"ok": True, "connected": True, "target": False})
        self.assertEqual(len(self.spawned), 1)

    def test_port_down_triggers_restart(self):
        """端口不通（未运行）→ 拉起。"""
        self._run(OSError("connection refused"))
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.killed, [17899])

    def test_no_config_means_no_aibot(self):
        """未配置智能机器人（纯 webhook 用户）→ 什么都不做。"""
        os.remove(self.cfg_path)
        self._run({"ok": True, "connected": False})
        self.assertEqual(self.spawned, [], "未启用 aibot 的用户不该被拉起连接器")

    def test_missing_venv_is_skipped(self):
        """没装 SDK 环境 → 不尝试拉起（避免每次提交消息都失败一次）。"""
        os.remove(self.venv)
        self._run({"ok": True, "connected": False})
        self.assertEqual(self.spawned, [], "没有 venv 解释器时不该 spawn")

    def test_restart_is_throttled_within_grace(self):
        """刚重启过又判掉线 → 不再杀+拉。

        连接器从拉起→ws 认证完成实测约 10s，这期间 /health 必然仍是
        connected=false。不节流的话，连着提交几条 prompt 就会把正在启动的新实例
        反复杀掉（杀进程走 PowerShell，秒级），连接器永远起不来、每次提交都被拖慢。
        """
        health = {"ok": True, "connected": False, "target": True}
        self._run(health)
        self.assertEqual(len(self.spawned), 1, "首次掉线应拉起")
        self.assertEqual(self.killed, [17899], "首次掉线应清掉假死旧进程")

        self._run(health)  # 宽限窗内的第二次
        self.assertEqual(len(self.spawned), 1, "宽限窗内不该重复拉起")
        self.assertEqual(self.killed, [17899], "宽限窗内不该重复杀进程")
        self.assertTrue(any(e.get("connector_restart_throttled") for e in self.logs),
                        "被节流时应留下日志，便于事后排查")

    def test_restart_retried_after_grace(self):
        """宽限窗过期后仍掉线 → 允许再重启，节流不等于永久放弃自愈。"""
        with open(hs.restart_state_path(), "w", encoding="utf-8") as f:
            json.dump({"last_restart": time.time() - 3600}, f)
        self._run({"ok": True, "connected": False, "target": True})
        self.assertEqual(len(self.spawned), 1, "宽限窗过期后应重新拉起")
        self.assertEqual(self.killed, [17899], "过期后应重新清旧进程")

    def test_healthy_connector_writes_no_restart_state(self):
        """健康时不写节流状态——否则真掉线时会被上次留下的旧时间戳挡住。"""
        self._run({"ok": True, "connected": True, "target": True})
        self.assertFalse(os.path.exists(hs.restart_state_path()),
                         "健康路径不该产生节流状态文件")


class TestKillStale(unittest.TestCase):
    def test_kills_only_listed_pids(self):
        killed = []

        def fake_run(cmd, **kw):
            if cmd[0] == "powershell":
                class R:
                    stdout = "1234\n5678\n"
                return R()
            killed.append(cmd[2] if len(cmd) > 2 else None)
            return mock.MagicMock()

        with mock.patch.object(hs, "log", lambda e: None), \
                mock.patch.object(hs.subprocess, "run", fake_run), \
                mock.patch.object(hs.time, "sleep", lambda s: None):
            hs._kill_stale_connector(17899)
        self.assertEqual(killed, ["1234", "5678"])


if __name__ == "__main__":
    unittest.main()
