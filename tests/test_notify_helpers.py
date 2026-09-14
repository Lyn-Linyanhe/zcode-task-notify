# -*- coding: utf-8 -*-
"""notify.py 纯函数单测 —— 零第三方依赖，任何环境都能跑。

跑法: python -m unittest discover -s tests
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

from notify import _clean_line, make_summary  # noqa: E402


class TestCleanLine(unittest.TestCase):
    def test_strips_markdown_marks_and_collapses_space(self):
        self.assertEqual(_clean_line("## 结论   **完成**  `x`"), "结论 完成 x")

    def test_strips_blockquote_marker(self):
        self.assertEqual(_clean_line("> 14:20 · sess_x"), "14:20 · sess_x")


class TestMakeSummary(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(make_summary(""), "")
        self.assertEqual(make_summary("   \n\t\n  "), "")

    def test_conclusion_line_wins(self):
        text = "先交代一点背景。\n结论：报告已生成，共 12 个章节。\n后面还有细节"
        self.assertEqual(make_summary(text), "结论：报告已生成，共 12 个章节。")

    def test_filler_opening_is_skipped(self):
        text = "好的，我来处理。\n实际内容在这里，长度足够形成一条摘要。"
        self.assertNotIn("好的", make_summary(text))

    def test_heading_is_joined_with_body_when_too_short(self):
        text = "# 报告标题\n正文内容足够长足够长足够长足够长"
        got = make_summary(text)
        self.assertTrue(got.startswith("报告标题："), got)
        self.assertIn("正文内容", got)

    def test_long_line_is_truncated_with_ellipsis(self):
        got = make_summary("详" * 200)
        self.assertEqual(len(got), 121)
        self.assertTrue(got.endswith("…"))


if __name__ == "__main__":
    unittest.main()
