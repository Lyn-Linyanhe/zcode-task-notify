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


class TestSummaryIgnoresCode(unittest.TestCase):
    """代码块绝不进摘要。

    实测事故：agent 回复里有一行 JS，摘要把整条通知变成了
    「关键找到了：：t.last_assistant_message = e.responseText ?? ...」——
    用户看到的就是一句没人看得懂的代码。agent 的回复大量含代码，这类内容
    占满篇幅却零摘要价值，必须在选行之前剔除。
    """

    def test_fenced_code_block_is_dropped(self):
        text = (
            "关键找到了：\n\n"
            "```js\ncase Tl.Stop:\n  t.last = e.responseText;\n```\n\n"
            "已完成通知摘要的修复，产出新的摘要规则。"
        )
        got = make_summary(text)
        self.assertNotIn("responseText", got)
        self.assertNotIn("```", got)
        self.assertIn("完成", got)

    def test_code_shaped_lines_are_dropped(self):
        for line in (
            "const x = 1;",
            "return foo();",
            "if (a) { b(); }",
            "def handler():",
            "import os",
        ):
            got = make_summary(f"{line}\n已完成环境配置，产出 19/19 检查全过。")
            self.assertIn("完成", got, f"结果句应胜出，而不是代码行 {line!r}")
            self.assertNotIn("()", got, f"代码行漏进了摘要: {line!r}")

    def test_inline_backticks_keep_the_word(self):
        """行内反引号里常是命令名/文件名，去掉反引号但保留词本身。"""
        got = make_summary("已更新 `config.json` 里的超时配置。")
        self.assertIn("config.json", got)
        self.assertNotIn("`", got)

    def test_all_code_falls_back_to_empty(self):
        self.assertEqual(make_summary("```py\nprint(1)\n```"), "")


class TestSummaryPrefersResult(unittest.TestCase):
    """摘要只写「结果 + 产出物名称」，不复述过程。"""

    def test_result_sentence_beats_process(self):
        text = (
            "先读了 README，然后跑了测试，发现有 3 个失败。\n"
            "修复了 python3 指向问题。\n"
            "已完成插件环境修复，产出 19/19 检查全过。"
        )
        got = make_summary(text)
        self.assertIn("19/19", got)
        self.assertNotIn("README", got, "过程不该进摘要")

    def test_output_name_is_kept(self):
        got = make_summary("已完成论文精读，产出 18 页精读版 PPT 和 28 页拓展版。")
        self.assertIn("18 页精读版 PPT", got)

    def test_process_reason_is_dropped(self):
        """「为什么」不属于结果，压掉它。"""
        got = make_summary("因为报告过期了，所以重跑了一遍。已完成最终交付。")
        self.assertNotIn("过期", got)
        self.assertIn("完成", got)


    def test_multiple_deliverables_in_one_line_are_kept(self):
        got = make_summary("已生成年度报告.xlsx。已生成预算明细.csv。")
        self.assertIn("年度报告.xlsx", got)
        self.assertIn("预算明细.csv", got)

    def test_generic_final_sentence_does_not_hide_deliverables(self):
        for separator in ("。", "\n"):
            with self.subTest(separator=separator):
                text = separator.join(("已产出18页精读版PPT", "已产出28页拓展版PPT", "已完成全部工作"))
                got = make_summary(text)
                self.assertIn("18页精读版PPT", got)
                self.assertIn("28页拓展版PPT", got)

    def test_additional_deliverable_is_not_lost(self):
        got = make_summary("已完成交付。产出年度报告.xlsx。另生成预算明细.csv。")
        self.assertIn("年度报告.xlsx", got)
        self.assertIn("预算明细.csv", got)

    def test_result_and_additional_deliverable_are_both_kept(self):
        got = make_summary("已完成报告，产出精读版PPT。另生成拓展版PPT。")
        self.assertIn("精读版PPT", got)
        self.assertIn("拓展版PPT", got)

    def test_repeated_deliverable_is_not_repeated_in_summary(self):
        got = make_summary("已生成年度报告.xlsx。\n已生成年度报告.xlsx。\n已生成预算明细.csv。")
        self.assertEqual(got.count("年度报告.xlsx"), 1)
        self.assertIn("预算明细.csv", got)


if __name__ == "__main__":
    unittest.main()
