"""Serialisation and the six export formats, driven by the recorded fixtures.

Two kinds of subject here: `canonical_json` (the bytes `mpa verify` compares, so
they must be stable and loss-free) and the human formats.  The checks that matter
are the boring ones - row count equals track count, an unknown field is empty
rather than guessed, and a comma in a title does not break the CSV.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.export import (FORMATS, canonical_json, export, export_to, provenance,  # noqa: E402
                        to_csv, to_jsonl, to_txt, write_json)
from mpa.model import (Artist, Album, Field, Ledger, Playlist, PlaylistMeta,  # noqa: E402
                       SourceInfo, TRACK_FIELDS, TRACK_OK, TRACK_UNAVAILABLE, Track)

ROW_FIELDS = ["index", "status", "title", "artists", "album", "duration_ms",
              "language", "released_at", "collected_at", "id", "extra", "reason",
              "provenance"]


def synthetic() -> Playlist:
    """A tiny playlist that exercises the awkward branches of every format."""
    tracks = [
        Track(index=0, title='He said "hi", then left', id="a1",
              artists=[Artist("A, B"), Artist("C")], album=Album(name="Al|bum"),
              duration_ms=Field.platform(185000), language=Field.platform("国语"),
              released_at=Field.platform("2020-01-02"),
              collected_at=Field.derived("2026-01-01", method="test")),
        Track(index=1, title=None, id="a2", status=TRACK_UNAVAILABLE,
              reason="平台已下架"),
    ]
    return Playlist(
        schema="mpa/playlist@1",
        source=SourceInfo(provider="toy", input="toy", adapter_version="9.9.9",
                          fetched_at="2026-01-01T00:00:00Z", mode="direct-http",
                          raw_saved=False),
        playlist=PlaylistMeta(id="1", title="Toy 列表", creator_name=" nobody "),
        ledger=Ledger(declared=2, fetched=2, ok=1, unavailable=1),
        capabilities={"duration_ms": True}, tracks=tracks)


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpa-export-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestCanonicalJson(ExportCase):
    def test_it_is_utf8_kept_indented_two_spaces_and_key_order_preserving(self):
        text = canonical_json({"b": [1, {"z": None}], "a": "中文"})
        self.assertEqual(text,
                         '{\n  "b": [\n    1,\n    {\n      "z": null\n    }\n  ],\n'
                         '  "a": "中文"\n}\n')
        self.assertNotIn("\\u", text)

    def test_it_round_trips_every_fixture_playlist(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                obj = S.playlist_obj(provider)
                self.assertEqual(json.loads(canonical_json(obj)), obj)

    def test_it_matches_the_bytes_on_disk_for_every_fixture(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(
                    canonical_json(S.playlist_obj(provider)),
                    (S.fixture_dir(provider) / "playlist.json").read_text(
                        encoding="utf-8"))

    def test_write_json_creates_the_parents_and_returns_the_path(self):
        target = self.tmp / "a" / "b" / "out.json"
        returned = write_json(target, {"x": 1})
        self.assertEqual(returned, target)
        self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "x": 1\n}\n')

    def test_a_newline_is_always_the_last_byte(self):
        for value in ({}, [], "", 0, None, "x"):
            with self.subTest(value=value):
                self.assertTrue(canonical_json(value).endswith("\n"))
                self.assertFalse(canonical_json(value).endswith("\n\n"))


class TestProvenance(ExportCase):
    def test_it_lists_every_canonical_field_in_contract_order(self):
        track = S.playlist("qishui").tracks[0]
        text = provenance(track)
        self.assertEqual([bit.split("=")[0] for bit in text.split(";")],
                         list(TRACK_FIELDS))
        self.assertTrue(text.endswith("collected_at=unavailable"), text)

    def test_every_fixture_row_can_explain_itself(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                playlist = S.playlist(provider)
                line = to_csv(playlist).splitlines()[1]
                row = next(csv.DictReader(io.StringIO(to_csv(playlist))))
                self.assertIn("=", row["provenance"])
                self.assertEqual(len(row["provenance"].split(";")), 4, line)


class TestJsonl(ExportCase):
    def test_one_line_per_track_and_nothing_else(self):
        playlist = S.playlist("qishui")
        text = to_jsonl(playlist)
        lines = text.splitlines()
        self.assertEqual(len(lines), len(playlist.tracks))
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_each_line_is_the_track_plus_a_playlist_header(self):
        playlist = S.playlist("kugou")
        for line, track in zip(to_jsonl(playlist).splitlines(), playlist.tracks):
            with self.subTest(index=track.index):
                obj = json.loads(line)
                self.assertEqual(obj["_playlist"], {
                    "provider": "kugou", "id": playlist.playlist.id,
                    "title": playlist.playlist.title})
                self.assertEqual({k: v for k, v in obj.items() if k != "_playlist"},
                                 track.to_obj())

    def test_an_empty_playlist_is_an_empty_string(self):
        empty = Playlist(source=SourceInfo(provider="toy", input="toy"))
        self.assertEqual(to_jsonl(empty), "")


class TestCsv(ExportCase):
    def test_the_header_is_fixed_and_the_rows_align_with_the_tracks(self):
        playlist = S.playlist("qq")
        parsed = list(csv.DictReader(io.StringIO(to_csv(playlist))))
        self.assertEqual(list(parsed[0]), ROW_FIELDS)
        self.assertEqual(len(parsed), len(playlist.tracks))
        self.assertEqual([r["index"] for r in parsed],
                         [str(t.index) for t in playlist.tracks])

    def test_unavailable_fields_are_empty_never_guessed(self):
        playlist = S.playlist("netease")
        rows = list(csv.DictReader(io.StringIO(to_csv(playlist))))
        self.assertTrue(rows)
        for row in rows:
            for name in ("language", "collected_at"):
                with self.subTest(field=name, index=row["index"]):
                    self.assertEqual(row[name], "")

    def test_a_known_duration_is_written_as_milliseconds(self):
        rows = list(csv.DictReader(io.StringIO(to_csv(S.playlist("kugou")))))
        self.assertEqual(rows[0]["duration_ms"],
                         str(S.playlist("kugou").tracks[0].duration_ms.value))

    def test_awkward_titles_albums_and_artists_survive_the_round_trip(self):
        text = to_csv(synthetic())
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[0]["title"], 'He said "hi", then left')
        self.assertEqual(rows[0]["artists"], "A, B / C")
        self.assertEqual(rows[0]["album"], "Al|bum")
        self.assertEqual(rows[0]["language"], "国语")
        self.assertEqual(rows[1]["title"], "")
        self.assertEqual(rows[1]["reason"], "平台已下架")
        self.assertEqual(rows[1]["status"], TRACK_UNAVAILABLE)

    def test_duration_is_empty_for_a_track_without_one(self):
        rows = list(csv.DictReader(io.StringIO(to_csv(synthetic()))))
        self.assertEqual(rows[1]["duration_ms"], "")
        self.assertEqual(rows[1]["released_at"], "")
        self.assertEqual(rows[0]["duration_ms"], "185000")

    def test_the_delimiter_is_a_comma_and_lines_end_with_lf(self):
        text = to_csv(synthetic())
        self.assertNotIn("\r\n", text)
        self.assertTrue(text.endswith("\n"))

    def test_an_empty_playlist_still_has_the_header(self):
        self.assertEqual(to_csv(Playlist(source=SourceInfo(provider="toy", input="toy"))),
                         ",".join(ROW_FIELDS) + "\n")

    def test_the_extra_column_is_the_same_column_on_every_platform(self):
        """表头固定是 CSV 的契约：平台不同、有没有 extra，都不改列名与列数。"""
        headers = set()
        for provider in S.PROVIDERS:
            headers.add(tuple(next(csv.DictReader(io.StringIO(to_csv(S.playlist(provider)))))))
        headers.add(tuple(next(csv.DictReader(io.StringIO(to_csv(synthetic()))))))
        self.assertEqual(headers, {tuple(ROW_FIELDS)})

    def test_the_extra_column_carries_the_platform_fields_sorted_by_key(self):
        rows = list(csv.DictReader(io.StringIO(to_csv(S.playlist("kugou")))))
        self.assertEqual(rows[0]["extra"], "bpm=95;fileid=847;remark=《星曜计划》音乐企划")
        qishui = list(csv.DictReader(io.StringIO(to_csv(S.playlist("qishui")))))[0]
        self.assertEqual(qishui["extra"],
                         "composers=JuggShots;genre=Pop;lyricists=JuggShots;"
                         "subgenre=Chinese Pop")

    def test_a_list_valued_extra_is_joined_with_slashes(self):
        playlist = S.playlist("netease")
        rows = list(csv.DictReader(io.StringIO(to_csv(playlist))))
        first_with_alias = next(r for r in rows if "alias=" in r["extra"])
        index = int(first_with_alias["index"])
        alias = playlist.tracks[index].extra["alias"].value
        self.assertEqual(first_with_alias["extra"],
                         "alias=" + "/".join(alias) + ";popularity=100.0")

    def test_a_track_without_extras_gets_an_empty_cell(self):
        rows = list(csv.DictReader(io.StringIO(to_csv(synthetic()))))
        for row in rows:
            self.assertEqual(row["extra"], "")

    def test_the_extra_cell_is_deterministic(self):
        playlist = S.playlist("kugou")
        first = [r["extra"] for r in csv.DictReader(io.StringIO(to_csv(playlist)))]
        second = [r["extra"] for r in csv.DictReader(io.StringIO(to_csv(playlist)))]
        self.assertEqual(first, second)

    def test_an_unavailable_extra_is_left_out_of_the_cell(self):
        """extra 里理论上不会有 unavailable，但真出现了也不能写成 ``key=None``。"""
        playlist = S.playlist("qq")          # 每次从磁盘重建，改它不会污染别的测试
        playlist.tracks[0].extra = {"gone": Field.unavailable("平台没给"),
                                    "kept": Field.platform("x")}
        row = next(csv.DictReader(io.StringIO(to_csv(playlist))))
        self.assertEqual(row["extra"], "kept=x")


class TestTxt(ExportCase):
    def test_ok_tracks_are_numbered_and_unavailable_ones_are_marked(self):
        text = to_txt(synthetic())
        self.assertEqual(text.splitlines(),
                         ['   1. A, B / C - He said "hi", then left',
                          "   2. [缺失] 平台已下架"])

    def test_a_known_track_without_artists_falls_back_to_a_question_mark(self):
        playlist = synthetic()
        playlist.tracks[0].artists = []
        self.assertIn("   1. ? - He said", to_txt(playlist))

    def test_a_missing_reason_still_prints_something(self):
        playlist = synthetic()
        playlist.tracks[1].reason = None
        self.assertIn("[缺失] 不可用", to_txt(playlist))

    def test_an_empty_playlist_is_an_empty_string(self):
        self.assertEqual(to_txt(Playlist(source=SourceInfo(provider="toy", input="toy"))), "")

    def test_no_line_is_blank(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                for line in to_txt(S.playlist(provider)).splitlines():
                    self.assertTrue(line.strip())


class TestExportDispatch(ExportCase):
    def test_the_format_list_is_exactly_what_is_implemented(self):
        self.assertEqual(FORMATS, ("json", "jsonl", "csv", "txt"),
                         "支持的导出格式就是这四种")
        playlist = S.playlist("qishui")
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                self.assertTrue(export(playlist, fmt))
                self.assertTrue(export(playlist, fmt.upper()), "format is case-folded")

    def test_json_is_the_canonical_form(self):
        playlist = S.playlist("qq")
        self.assertEqual(export(playlist, "json"), canonical_json(playlist.to_obj()))

    def test_an_unknown_format_is_a_plain_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            export(S.playlist("qishui"), "yaml")
        self.assertIn("unknown format: yaml", str(ctx.exception))
        self.assertIn("json, jsonl, csv, txt", str(ctx.exception),
                      "报错要说清有哪些格式可用")

    def test_dispatch_returns_the_same_text_as_the_specific_function(self):
        playlist = S.playlist("qq")
        self.assertEqual(export(playlist, "jsonl"), to_jsonl(playlist))
        self.assertEqual(export(playlist, "csv"), to_csv(playlist))
        self.assertEqual(export(playlist, "txt"), to_txt(playlist))


class TestExportTo(ExportCase):
    def test_without_an_out_path_it_only_returns_the_text(self):
        before = sorted(self.tmp.iterdir())
        text = export_to(S.playlist("qishui"), "csv", None)
        self.assertEqual(text, export(S.playlist("qishui"), "csv"))
        self.assertEqual(before, sorted(self.tmp.iterdir()))

    def test_an_empty_out_path_also_writes_nothing(self):
        text = export_to(S.playlist("qishui"), "csv", "")
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(text, to_csv(S.playlist("qishui")))

    def test_it_writes_utf8_and_makes_the_parent_directory(self):
        target = self.tmp / "deep" / "er" / "playlist.csv"
        text = export_to(S.playlist("kugou"), "csv", target)
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertEqual(text, to_csv(S.playlist("kugou")))

    def test_a_dash_means_stdout_and_writes_no_file(self):
        """Regression: ``--out -`` used to write a file literally called ``-``."""
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        text = export_to(S.playlist("qishui"), "txt", "-")
        self.assertIn("1.", text)
        self.assertEqual(text, to_txt(S.playlist("qishui")))
        self.assertEqual(list(self.tmp.iterdir()), [],
                         "「-」是 stdout 的写法，不该落盘成一个叫 - 的文件")


if __name__ == "__main__":
    unittest.main()
