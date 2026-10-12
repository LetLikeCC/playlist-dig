#!/usr/bin/env python3
"""Check that the docs only mention things that exist.

Why this file exists: the README once described `mpa facts`, `mpa analyze` and a
module `features/heuristics.py` months after they were gone, and the exit-code
table listed a `diff` subcommand that had never shipped. The README's whole pitch
is "every claim can be checked with a command", so a claim that cannot even be
typed is the worst kind of rot - and nobody notices it by reading.

What it checks (all in-tree, no network, no third-party imports):

1. ``mpa <word>`` / ``python3 -m mpa <word>`` -> ``<word>`` must be a real
   subcommand (``mpa.cli.SUBCOMMANDS``);
2. ```xxx.py`` -> a backticked ``.py`` path must exist in the repo;
3. relative markdown links -> the target file must exist;
4. ``第 N 节`` / ``第 N.M 节`` -> that heading must exist in the referenced file
   (or in the same file when there is no link right before it).

Exit code 0 when everything resolves, 1 with a list otherwise. Run it from the
repo root; CI runs it next to ``gen_readme_numbers.py --check``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: docs we hold to this standard (the review file in the workspace is not ours)
DOC_GLOBS = ("README.md", "SKILL.md", "CHANGELOG.md", "docs/**/*.md", "tests/README.md")
#: Python files are checked for section references only - they cite the schema a lot
CODE_GLOBS = ("mpa/**/*.py", "tools/*.py", "tests/*.py")

#: `mpa fetch`, `python3 -m mpa export`; `mpa/...` paths are not commands
CMD = re.compile(r"(?:python3?\s+-m\s+mpa|\bmpa)\s+([a-z][a-z-]+)")
#: backticked paths that look like repo files
PY_PATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)`")
#: markdown links to relative files
MD_LINK = re.compile(r"\]\(([^)#\s]+\.(?:md|py|json|yml|toml))\)")
#: 第 11 节 / 第 11.5 节
SECTION_REF = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*节")
#: a document named right before a section ref, in any of the notations we use:
#: `[x](y.md) 第 3 节` / `` `docs/schema.md` 第 11 节 `` / `schema.md 第 8.1 节`
#: only ``.md`` names count: 「第 8.3 节」 never points into a Python file
HINT_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
HINT_TICK = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.md)`")
HINT_BARE = re.compile(r"(?<![`\w/])((?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.md)")
HEADING = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+)?)[.\s、]")

#: things that look like `.py` files but are not in this repo (historical mentions)
PY_ALLOWLIST: set[str] = set()

#: this file has to name the patterns it looks for, so it would always flag itself
SELF = Path(__file__).resolve()

#: 文件里带这一行就整份跳过。只给「故意写错示例」的测试文件用——它是显式的、
#: 能被 grep 到的豁免，而不是让守卫学会忽略某一类目录。
OPT_OUT = "doc-refs: allow-synthetic"


def repo_files() -> list[Path]:
    out: list[Path] = []
    for pattern in DOC_GLOBS:
        out.extend(sorted(ROOT.glob(pattern)))
    return [p for p in out if p.is_file()]


def code_files() -> list[Path]:
    out: list[Path] = []
    for pattern in CODE_GLOBS:
        out.extend(sorted(ROOT.glob(pattern)))
    return [p for p in out if p.is_file()]


def headings(path: Path) -> set[str]:
    if not path.is_file() or path.suffix != ".md":
        return set()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING.match(line)
        if m:
            found.add(m.group(1))
    return found


def resolve_target(path: Path, text: str, start: int) -> Path | None:
    """Which document does a ``第 N 节`` at ``start`` point at?

    Looks backwards on the same line for the nearest document name (markdown
    link, backticked path, or a bare ``schema.md``). No hint means "this file";
    in Python source there are no headings, so the default is ``docs/schema.md``
    - that is the document the code cites.
    """
    line_start = text.rfind("\n", 0, start) + 1
    window = text[line_start:start]
    best: tuple[int, str] | None = None
    for pattern in (HINT_LINK, HINT_TICK, HINT_BARE):
        for m in pattern.finditer(window):
            if best is None or m.start() > best[0]:
                best = (m.start(), m.group(1))
    if best is None:
        if path.suffix == ".py":
            return ROOT / "docs" / "schema.md"
        return path
    token = best[1]
    candidates = [path.parent / token, ROOT / "docs" / token, ROOT / token]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return path.parent / token


def check_sections(path: Path, text: str) -> list[str]:
    """「第 N 节」必须能在它指向的文档里找到。"""
    problems: list[str] = []
    where = path.relative_to(ROOT)
    for m in SECTION_REF.finditer(text):
        number = m.group(1)
        target = resolve_target(path, text, m.start())
        if number not in headings(target):
            shown = target.relative_to(ROOT) if str(target).startswith(str(ROOT)) else target
            problems.append(f"{where}: 引用了 {shown} 里不存在的「第 {number} 节」")
    return problems


def check_text(path: Path, text: str, subcommands: tuple[str, ...]) -> list[str]:
    if OPT_OUT in text:
        return []
    problems: list[str] = []
    where = path.relative_to(ROOT)

    for m in CMD.finditer(text):
        word = m.group(1)
        if word not in subcommands:
            problems.append(f"{where}: 提到了不存在的子命令 `mpa {word}`")

    for m in PY_PATH.finditer(text):
        candidate = m.group(1)
        if candidate in PY_ALLOWLIST:
            continue
        if not any((ROOT / base / candidate).exists() or (ROOT / candidate).exists()
                   for base in ("", "mpa", "tools", "tests", "docs")):
            problems.append(f"{where}: 引用了不存在的文件 `{candidate}`")

    for m in MD_LINK.finditer(text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / target).exists():
            problems.append(f"{where}: 链接指向不存在的文件 `{target}`")

    for m in SECTION_REF.finditer(text):
        number = m.group(1)
        target = resolve_target(path, text, m.start())
        if number not in headings(target):
            shown = target.relative_to(ROOT) if str(target).startswith(str(ROOT)) else target
            problems.append(f"{where}: 引用了 {shown} 里不存在的「第 {number} 节」")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from mpa.cli import SUBCOMMANDS  # noqa: E402  (path set up above)

    problems: list[str] = []
    for path in repo_files():
        problems.extend(check_text(path, path.read_text(encoding="utf-8"), SUBCOMMANDS))
    for path in code_files():
        if path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8")
        if OPT_OUT in text:
            continue
        problems.extend(check_sections(path, text))

    if problems:
        print("文档里引用了不存在的东西：", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        print("改文档，或者把缺的东西补上；这一关不允许带病通过。", file=sys.stderr)
        return 1
    if not args.quiet:
        print("文档引用全部指向真实存在的东西")
    return 0


if __name__ == "__main__":
    sys.exit(main())
