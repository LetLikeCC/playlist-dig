#!/usr/bin/env python3
"""Regenerate the numbers in README.md from the recorded fixtures.

The README claims things like "QQ 的语种字段 76/76". Claims like that rot the
moment somebody touches an adapter, so they are generated from
``tests/fixtures/*/playlist.json`` (real recorded fetches) instead of typed by
hand. Run without arguments to rewrite README.md in place::

    python3 tools/gen_readme_numbers.py

``--check`` compares instead of writing and exits 1 on drift; that is what CI
runs, so a stale table fails the build instead of quietly misleading a reader.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
README = ROOT / "README.md"

BEGIN = "<!-- coverage:begin -->"
END = "<!-- coverage:end -->"

#: field -> how to describe it in the table
FIELD_LABELS = {
    "duration_ms": "`duration_ms` 时长",
    "language": "`language` 语种",
    "released_at": "`released_at` 发行日期",
    "collected_at": "`collected_at` 收藏时间",
}
#: shown in this order
FIELD_ORDER = ["duration_ms", "language", "released_at", "collected_at"]

#: columns in the same order as the README's platform table, not alphabetically;
#: a platform missing from a fixture is simply not a column.
PROVIDER_ORDER = ["netease", "qq", "kuwo", "kugou", "qishui"]


def _load(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def collect() -> list[dict]:
    """One row per fixture, read from the fixtures themselves (no network).

    覆盖统计直接用 ``mpa.coverage``（和 ``mpa doctor --live`` 同一套口径），
    所以 README 这张表、doctor 的自检、契约测试说的永远是同一件事。
    """
    sys.path.insert(0, str(ROOT))
    from mpa.coverage import coverage, extra_coverage, extra_keys  # noqa: E402
    from mpa.model import Playlist, TRACK_FIELDS  # noqa: E402

    rows = []
    for directory in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        playlist_path = directory / "playlist.json"
        provenance_path = directory / "provenance.json"
        if not playlist_path.is_file():
            continue
        playlist = Playlist.from_obj(_load(playlist_path))
        tracks = playlist.tracks_ok()
        playlist_extra = {key: {"value": field.value}
                          for key, field in sorted(playlist.playlist.extra.items())
                          if field.known}
        rows.append({
            "provider": playlist.source.provider,
            "fixture": directory.name,
            "kind": _load(provenance_path).get("fixture_kind", "unknown")
            if provenance_path.is_file() else "unknown",
            "input": playlist.source.input,
            "playlist_id": playlist.playlist.id,
            "declared": playlist.ledger.declared,
            "fetched": playlist.ledger.fetched,
            "ok": playlist.ledger.ok,
            "unavailable": playlist.ledger.unavailable,
            "mode": playlist.source.mode,
            "coverage": {field: coverage(tracks, field) for field in TRACK_FIELDS},
            "extras": {key: {"n": extra_coverage(playlist, key)["n"]}
                       for key in extra_keys(playlist)},
            "playlist_extra": playlist_extra,
        })
    rows.sort(key=lambda r: (PROVIDER_ORDER.index(r["provider"])
                             if r["provider"] in PROVIDER_ORDER else len(PROVIDER_ORDER),
                             r["provider"]))
    return rows


def render(rows: list[dict]) -> str:
    fields: list[str] = [f for f in FIELD_ORDER]
    for row in rows:
        for field in row["coverage"]:
            if field not in fields:
                fields.append(field)

    out: list[str] = []
    out.append(BEGIN)
    out.append("<!-- 由 tools/gen_readme_numbers.py 生成，请勿手改；CI 会检查是否过期 -->")
    out.append("")
    header = ["字段", *[f"`{r['provider']}`" for r in rows]]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join(["---"] * len(header)) + " |")
    for field in fields:
        cells = []
        for row in rows:
            cov = row["coverage"].get(field)
            if not cov or not cov["of"]:
                cells.append("—")
            else:
                cells.append(f"{cov['n']}/{cov['of']}")
        out.append("| " + " | ".join([FIELD_LABELS.get(field, f"`{field}`"), *cells]) + " |")
    out.append("")
    declared = ["声明 / 实取", *[
        f"{r['declared'] if r['declared'] is not None else '?'} / {r['fetched']}"
        + ("" if r["declared"] == r["fetched"] else " **对不上**")
        for r in rows]]
    out.append("| " + " | ".join(declared) + " |")
    out.append("| " + " | ".join(["---"] * len(declared)) + " |")
    out.append("| 可用 / 不可用 | " + " | ".join(
        f"{r['ok']} / {r['unavailable']}" for r in rows) + " |")
    out.append("| 抓取方式 | " + " | ".join(
        f"`{r['mode']}`" for r in rows) + " |")
    out.append("")

    # `extra` 是平台专属字段（`Track.extra`），不在 `TRACK_FIELDS` 里，但它同样
    # 是「平台给了、我们有没有拿」的问题：netease 的 popularity、kugou 的 fileid、
    # 汽水的曲风都住在这里。手写这张表必然烂掉，所以一起生成。
    extra_keys = sorted({k for r in rows for k in r["extras"]})
    if extra_keys:
        out.append("平台专属字段（`Track.extra`，分母是该平台实取到的曲目数）：")
        out.append("")
        header = ["`extra` 键", *[f"`{r['provider']}`" for r in rows]]
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * len(header)) + " |")
        for key in extra_keys:
            cells = []
            for row in rows:
                entry = row["extras"].get(key)
                cells.append(f"{entry['n']}/{row['fetched']}" if entry else "—")
            out.append("| " + " | ".join([f"`{key}`", *cells]) + " |")
        out.append("")

    # 歌单级 extra（`playlist.extra`）：作用域是整张歌单，不是每首歌。有值的
    # 直接把值列出来（列表用「、」连接），没有的平台画「—」——「没有这个字段」
    # 和「这次没给」在这张表里一眼可分。
    playlist_keys = sorted({k for r in rows for k in r["playlist_extra"]})
    if playlist_keys:
        out.append("歌单级字段（`playlist.extra`，描述的是歌单本身，不是每首歌）：")
        out.append("")
        header = ["`playlist.extra` 键", *[f"`{r['provider']}`" for r in rows]]
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * len(header)) + " |")
        for key in playlist_keys:
            cells = []
            for row in rows:
                entry = row["playlist_extra"].get(key)
                if not entry:
                    cells.append("—")
                    continue
                value = entry["value"]
                if isinstance(value, list):
                    value = "、".join(str(v) for v in value)
                text = str(value)
                cells.append(text if len(text) <= 44 else text[:41] + "…")
            out.append("| " + " | ".join([f"`{key}`", *cells]) + " |")
        out.append("")

    out.append("上面每一列都对应仓库里的一份真实抓取记录：`tests/fixtures/<平台>/`，"
               "里面同时存了 `raw/`（原始响应）、`playlist.json`（推导结果）和 "
               "`provenance.json`（出处）。任何人都可以用 `mpa verify tests/fixtures/<平台>` "
               "在断网环境下重新算一遍，对不上就会失败。")
    out.append(END)
    return "\n".join(out)


def splice(text: str, block: str) -> str:
    if BEGIN not in text or END not in text:
        raise SystemExit(f"README.md 里找不到 {BEGIN} / {END} 标记")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return head + block + tail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只检查 README.md 是否与 fixtures 一致")
    args = parser.parse_args()

    block = render(collect())
    current = README.read_text(encoding="utf-8")
    updated = splice(current, block)
    if args.check:
        if updated != current:
            print("README.md 的覆盖表已经过期：请运行 python3 tools/gen_readme_numbers.py",
                  file=sys.stderr)
            return 1
        print("README.md 覆盖表与 fixtures 一致")
        return 0
    if updated == current:
        print("README.md 无需更新")
        return 0
    README.write_text(updated, encoding="utf-8")
    print("已更新 README.md 的覆盖表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
