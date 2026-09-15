# -*- coding: utf-8 -*-
"""install.py 的安装校验与幂等逻辑单测 —— 零第三方依赖。

两条都是真事故换来的：
- webhook 抄错（少前缀/粘成别的地址）会一路静默失败，装完才发现；
- 换目录或重新下载后再装，旧注册若认不出来就会两条 hook 同时跑：
  一条正常推送、另一条静默失败，现象是"通知时有时无"（2026-09-14 实测踩到）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import install  # noqa: E402

GOOD = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=11112222-3333-4444-5555-666677778888"


class TestWebhookValidation(unittest.TestCase):
    def test_accepts_real_wecom_webhook(self):
        self.assertTrue(install.looks_like_webhook(GOOD))
        self.assertTrue(install.looks_like_webhook("  " + GOOD + "  "), "两侧空白应被容忍")

    def test_rejects_bot_id_or_bare_key(self):
        self.assertFalse(install.looks_like_webhook("aib4Xk2pQ9wZ"))
        self.assertFalse(install.looks_like_webhook("11112222-3333-4444-5555-666677778888"))
        self.assertFalse(install.looks_like_webhook(""))
        self.assertFalse(install.looks_like_webhook(None))

    def test_rejects_wrong_host_or_missing_key(self):
        self.assertFalse(install.looks_like_webhook(
            "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc12345678"), "必须 https")
        self.assertFalse(install.looks_like_webhook(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"))
        self.assertFalse(install.looks_like_webhook(
            "https://example.com/cgi-bin/webhook/send?key=abc12345678"))


class TestIsOurGroup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zcode_inst_")
        self.here = os.path.join(self.tmp, "here", "scripts")
        self.other = os.path.join(self.tmp, "other", "scripts")
        os.makedirs(self.here)
        os.makedirs(self.other)
        # 本工具的签名文件：push_worker.py + heartbeat_spawn.py
        for d in (self.here, self.other):
            open(os.path.join(d, "push_worker.py"), "w").close()
            open(os.path.join(d, "heartbeat_spawn.py"), "w").close()
            open(os.path.join(d, "notify.py"), "w").close()

    def _group(self, path):
        return {"hooks": [{"type": "process", "command": "python", "args": [path]}]}

    def test_recognizes_current_install(self):
        self.assertTrue(install.is_our_group(self._group(os.path.join(self.here, "notify.py")), self.here))

    def test_recognizes_another_copy_of_this_tool(self):
        """另一份拷贝（换目录/重新下载）也必须认出来，否则会两条 hook 同时跑。"""
        self.assertTrue(install.is_our_group(
            self._group(os.path.join(self.other, "heartbeat_spawn.py")), self.here))

    def test_ignores_unrelated_hooks(self):
        plugin = {"hooks": [{"type": "process", "command": "node",
                             "args": [os.path.join(self.tmp, "plugin-stop.js")]}]}
        self.assertFalse(install.is_our_group(plugin, self.here))
        # 同名文件但没有同目录签名文件 → 不是本工具
        lone = os.path.join(self.tmp, "random", "notify.py")
        os.makedirs(os.path.dirname(lone), exist_ok=True)
        open(lone, "w").close()
        self.assertFalse(install.is_our_group(self._group(lone), self.here))

    def test_tolerates_malformed_groups(self):
        for bad in (None, {}, {"hooks": None}, {"hooks": [None]}, {"hooks": [{"args": None}]}):
            self.assertFalse(install.is_our_group(bad, self.here))

    def test_group_hook_args_collects_paths(self):
        g = {"hooks": [{"args": ["a"]}, None, {"args": ["b", "c"]}]}
        self.assertEqual(install.group_hook_args(g), ["a", "b", "c"])


class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zcode_uninst_")
        self.target = os.path.join(self.tmp, "home")
        self.cfg = os.path.join(self.target, ".zcode", "cli", "config.json")
        self.scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(self.scripts)
        for n in ("push_worker.py", "heartbeat_spawn.py", "notify.py"):
            open(os.path.join(self.scripts, n), "w").close()
        self.foreign = os.path.join(self.tmp, "old-copy", "scripts")
        os.makedirs(self.foreign)
        for n in ("push_worker.py", "heartbeat_spawn.py", "notify.py"):
            open(os.path.join(self.foreign, n), "w").close()

    def _write_cfg(self, data):
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_removes_current_and_foreign_registrations_keeps_others(self):
        other_hook = {"hooks": [{"type": "process", "command": "node", "args": ["C:/x/plugin.js"]}]}
        self._write_cfg({"hooks": {"enabled": True, "events": {
            "Stop": [{"hooks": [{"args": [os.path.join(self.scripts, "notify.py")]}]},
                     {"hooks": [{"args": [os.path.join(self.foreign, "notify.py")]}]},
                     other_hook],
        }}})
        with mock.patch.object(install, "SCRIPTS_DIR", self.scripts):
            self.assertEqual(install.uninstall(self.target), 0)
        with open(self.cfg, encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        self.assertEqual(hooks["events"]["Stop"], [other_hook], "别人的 hook 必须留着")
        backups = [n for n in os.listdir(os.path.dirname(self.cfg))
                   if n.startswith("config.json.bak-")]
        self.assertTrue(backups, "卸载前必须留一份带时间戳的备份")

    def test_disables_hooks_when_nothing_left(self):
        self._write_cfg({"hooks": {"enabled": True, "events": {
            "Stop": [{"hooks": [{"args": [os.path.join(self.scripts, "notify.py")]}]}]}}})
        with mock.patch.object(install, "SCRIPTS_DIR", self.scripts):
            install.uninstall(self.target)
        hooks = json.load(open(self.cfg, encoding="utf-8"))["hooks"]
        self.assertNotIn("events", hooks)
        self.assertFalse(hooks["enabled"], "没有别的 hook 了就该把开关关掉")

    def test_missing_config_is_not_an_error(self):
        with mock.patch.object(install, "SCRIPTS_DIR", self.scripts):
            self.assertEqual(install.uninstall(self.target), 0)


if __name__ == "__main__":
    unittest.main()
