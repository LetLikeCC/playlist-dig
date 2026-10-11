"""字段覆盖统计（`mpa/coverage.py`）：数一数每个字段有多少条有值、值是谁给的。

这个模块被 `mpa doctor --live` 与 `tools/gen_readme_numbers.py` 共用，所以它的口径必须和
`playlist.json` 完全一致：数字只从数据集里数出来，而且要能对上 README 里那张公开的覆盖表。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.coverage import coverage, extra_coverage, extra_keys, field_sources, values_of  # noqa: E402
from mpa.model import TRACK_FIELDS  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class TestCoverage(unittest.TestCase):
    def test_it_counts_only_known_values(self):
        tracks = S.playlist("qishui").tracks_ok()
        got = coverage(tracks, "duration_ms")
        self.assertEqual((got["n"], got["of"]), (65, 65))
        self.assertEqual(got["share"], 1.0)

    def test_a_field_the_platform_does_not_give_is_zero_not_omitted(self):
        """网易云没有语种：覆盖率是 0，但这一项仍然在表里（0 和「没这个字段」要分开说）。"""
        playlist = S.playlist("netease")
        got = coverage(playlist.tracks_ok(), "language")
        self.assertEqual((got["n"], got["of"], got["share"]), (0, 200, 0.0))
        self.assertEqual(field_sources(playlist.tracks_ok(), "language"),
                         {"unavailable": 200})

    def test_sources_add_up_to_the_track_count(self):
        for provider in S.PROVIDERS:
            playlist = S.playlist(provider)
            tracks = playlist.tracks_ok()
            for field in TRACK_FIELDS:
                with self.subTest(provider=provider, field=field):
                    self.assertEqual(sum(field_sources(tracks, field).values()), len(tracks))

    def test_extras_are_counted_against_the_ok_tracks(self):
        playlist = S.playlist("qishui")
        got = extra_coverage(playlist, "genre")
        self.assertEqual((got["n"], got["of"]), (65, 65))
        self.assertIs(got["declared"], True, "平台声明的字段要能看出来")
        self.assertIn("subgenre", extra_keys(playlist))
        self.assertNotIn("popularity", extra_keys(playlist), "网易云的字段不该出现在汽水这份里")

    def test_values_of_keeps_order_and_drops_missing(self):
        tracks = S.playlist("kugou").tracks_ok()
        values = values_of(tracks, "language")
        self.assertEqual(len(values), 50)
        self.assertEqual(values[:2], [tracks[0].language.value, tracks[1].language.value])

    def test_it_matches_the_readme_table(self):
        """README 那张公开的覆盖表就是这个模块生成的，两边必须逐格一致。"""
        text = (REPO / "README.md").read_text(encoding="utf-8")
        block = text.split("<!-- coverage:begin -->", 1)[1].split("<!-- coverage:end -->", 1)[0]
        header: list[str] = []
        rows: dict[str, dict[str, str]] = {}
        for line in block.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells[0] == "字段":
                header = [c.strip("`") for c in cells[1:]]
                continue
            name = cells[0].split()[0].strip("`")
            if name in TRACK_FIELDS:
                rows[name] = dict(zip(header, cells[1:]))
        for provider in S.PROVIDERS:
            playlist = S.playlist(provider)
            tracks = playlist.tracks_ok()
            for field in TRACK_FIELDS:
                got = coverage(tracks, field)
                want = rows[field][provider]
                with self.subTest(provider=provider, field=field):
                    self.assertEqual("—" if not got["of"] else f"{got['n']}/{got['of']}", want)


if __name__ == "__main__":
    unittest.main()
