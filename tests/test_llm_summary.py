# -*- coding: utf-8 -*-
"""LLM 摘要的超时与回退单测 —— 零第三方依赖。

摘要只影响通知的"可读性"，不该拖慢通知本身：旧实现超时 25 秒，实测 39 次尝试里 9 次
跑满 25 秒读超时后照样回退启发式摘要——等于每条这样的通知白等 25 秒。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("ZCODE_NOTIFY_SCRIPTS") or os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import push_worker as pw  # noqa: E402


def _cfg(**over):
    cfg = {"use_llm_summary": True, "llm_api_format": "openai", "llm_api_key": "k",
           "llm_base_url": "https://example.invalid", "llm_model": "m"}
    cfg.update(over)
    return cfg


class TestLlmTimeout(unittest.TestCase):
    def _run(self, cfg):
        seen = {}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "摘要"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        with mock.patch.object(pw.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(pw, "log", lambda entry: None):
            out = pw.llm_summary("正文内容", cfg)
        return out, seen

    def test_default_timeout_is_fifteen_seconds(self):
        out, seen = self._run(_cfg())
        self.assertEqual(out, "摘要")
        self.assertEqual(float(seen["timeout"]), 15,
                         "25 秒白等太久；10 秒会误杀 12 秒档的成功样本；经实测与用户确认定 15")

    def test_timeout_is_configurable(self):
        _out, seen = self._run(_cfg(llm_timeout_sec=4))
        self.assertEqual(seen["timeout"], 4)

    def test_success_logs_elapsed_ms(self):
        logs = []

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "摘要"}}]}).encode("utf-8")

        with mock.patch.object(pw.urllib.request, "urlopen",
                               lambda req, timeout=None: FakeResp()), \
                mock.patch.object(pw, "log", logs.append):
            pw.llm_summary("正文", _cfg())
        self.assertTrue(any("llm_ms" in e for e in logs), "成功也要记耗时，方便判断 10 秒够不够")

    def test_disabled_or_keyless_returns_none_without_request(self):
        with mock.patch.object(pw.urllib.request, "urlopen",
                               side_effect=AssertionError("不该发请求")):
            self.assertIsNone(pw.llm_summary("正文", _cfg(use_llm_summary=False)))
            self.assertIsNone(pw.llm_summary("正文", _cfg(llm_api_key="")))

    def test_timeout_falls_back_to_none_quietly(self):
        def boom(req, timeout=None):
            raise TimeoutError("read timed out")

        with mock.patch.object(pw.urllib.request, "urlopen", boom), \
                mock.patch.object(pw, "log", lambda entry: None):
            self.assertIsNone(pw.llm_summary("正文", _cfg()))


if __name__ == "__main__":
    unittest.main()
