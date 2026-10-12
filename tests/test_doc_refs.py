"""`tools/check_doc_refs.py`：文档里提到的命令与文件必须真的存在。

这个守卫存在的理由是一次真实翻车：README 里长期写着 `mpa facts`、`mpa analyze` 和一个
`features/heuristics.py`，退出码表里还列过一个从没发布过的 `diff` 子命令——而 README 的
卖点恰恰是「每句话都能用命令验证」。一份连命令都敲不出来的文档，是这个项目最不该有的东西。

测试分两半：

* **守卫本身在真实仓库上必须是绿的**（不然 CI 会一直红，大家就会去关掉它）；
* **四类问题各自都能被抓到**（子命令、`.py` 路径、markdown 链接、章节号）——用合成文本喂
  `check_text()`，不往仓库里塞脏东西。

`doc-refs: allow-synthetic` ⬇ 这个文件**故意**写着不存在的命令、路径与章节号（那是测试数据），
所以对守卫豁免自己；豁免是显式的一行，grep 得到，也不会让守卫学会忽略整个目录。
"""

from __future__ import annotations

import argparse
import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def load_tool(name: str) -> types.ModuleType:
    """Import ``tools/<name>.py`` without making ``tools`` a package."""
    path = REPO / "tools" / f"{name}.py"
    module = types.ModuleType(f"tools_{name}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


doc_refs = load_tool("check_doc_refs")
from mpa.cli import SUBCOMMANDS, build_parser  # noqa: E402


class TestSubcommandsStayInSync(unittest.TestCase):
    def test_the_constant_matches_the_parser(self):
        """守卫只认 `SUBCOMMANDS`；它和解析器分叉了，守卫就会开始说谎。"""
        action = next(a for a in build_parser()._actions
                      if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(tuple(action.choices), SUBCOMMANDS)

    def test_the_documented_commands_are_the_real_ones(self):
        self.assertEqual(SUBCOMMANDS, ("fetch", "verify", "export", "doctor", "adapters"))


class TestGuardOnThisRepo(unittest.TestCase):
    def test_the_repository_is_clean(self):
        problems: list[str] = []
        for path in doc_refs.repo_files():
            problems.extend(doc_refs.check_text(path, path.read_text(encoding="utf-8"),
                                                SUBCOMMANDS))
        self.assertEqual(problems, [], "文档引用了不存在的东西：\n" + "\n".join(problems))

    def test_it_reads_the_docs_we_hold_to_this_standard(self):
        files = {p.relative_to(REPO).as_posix() for p in doc_refs.repo_files()}
        for expected in ("README.md", "SKILL.md", "docs/schema.md",
                         "docs/references/qishui.md", "tests/README.md"):
            with self.subTest(file=expected):
                self.assertIn(expected, files)


class TestItCatchesEachKind(unittest.TestCase):
    def check(self, text: str) -> list[str]:
        return doc_refs.check_text(REPO / "README.md", text, SUBCOMMANDS)

    def test_an_unknown_subcommand(self):
        got = self.check("跑 `mpa analyze --by decade` 看分组。")
        self.assertTrue(any("不存在的子命令 `mpa analyze`" in p for p in got), got)

    def test_a_module_form_command(self):
        got = self.check("python3 -m mpa facts ./out")
        self.assertTrue(any("`mpa facts`" in p for p in got), got)

    def test_a_real_command_is_not_flagged(self):
        self.assertEqual(self.check("python3 -m mpa fetch x --out y\n`mpa verify y`"), [])

    def test_a_py_path_that_does_not_exist(self):
        got = self.check("猜语种在 `mpa/features/heuristics.py` 里。")
        self.assertTrue(any("不存在的文件" in p for p in got), got)

    def test_a_py_path_that_exists(self):
        self.assertEqual(self.check("入口在 `mpa/cli.py`，导出在 `mpa/export.py`。"), [])

    def test_a_link_to_a_missing_file(self):
        got = self.check("见 [分析层](docs/analysis.md) 的说明。")
        self.assertTrue(any("链接指向不存在的文件" in p for p in got), got)

    def test_a_section_that_does_not_exist(self):
        got = self.check("理由见 `docs/schema.md` 第 99 节。")
        self.assertTrue(any("第 99 节" in p for p in got), got)

    def test_a_section_that_exists(self):
        self.assertEqual(self.check("契约见 `docs/schema.md` 第 11 节。"), [])

    def test_a_section_ref_that_means_this_document(self):
        self.assertEqual(doc_refs.resolve_target(REPO / "docs" / "schema.md",
                                                 "见第 8.1 节。", 1),
                         (REPO / "docs" / "schema.md").resolve())

    def test_a_python_comment_defaults_to_the_schema(self):
        target = doc_refs.resolve_target(REPO / "mpa" / "model.py", "# 见第 11 节。", 3)
        self.assertEqual(target, (REPO / "docs" / "schema.md").resolve())


if __name__ == "__main__":
    unittest.main()
