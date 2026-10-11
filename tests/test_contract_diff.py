"""The two scripts in ``tools/``: ``contract_diff`` and ``rederive_fixture``.

Both are offline by construction - they read a dataset directory and never open
a socket - so they are cheap to test and the failure they guard against (a live
platform quietly dropping a field) is exactly the one the fixtures exist for.

``contract_diff.compare`` is a pure function over two summaries, so most of this
file feeds it hand-built dicts: that pins the threshold semantics (``fixture``
coverage >= 50 % and ``fresh`` below half of it) and, just as importantly, pins
what must *not* count as drift.  A canary that cries wolf every day gets ignored,
and then it is worse than no canary.

``rederive_fixture`` is exercised on ``shutil.copytree`` copies only; the
committed fixtures are never written to.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PLAYLIST_FILE = "playlist.json"


def load_tool(name: str) -> types.ModuleType:
    """Import ``tools/<name>.py`` without making ``tools`` a package.

    Reading and exec'ing the source keeps ``tools/__pycache__`` untouched, so a
    test run leaves no artefacts anywhere but the temp dirs.
    """
    path = REPO / "tools" / f"{name}.py"
    module = types.ModuleType(f"tools_{name}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


contract_diff = load_tool("contract_diff")
rederive_fixture = load_tool("rederive_fixture")


def summary(**changes) -> dict:
    """A well-formed summary; every test below changes exactly one shape."""
    base = {
        "dir": "/tmp/x",
        "schema": "mpa/playlist@1",
        "provider": "netease",
        "mode": "direct-http",
        "adapter_version": "2.0.0",
        "ok_tracks": 10,
        "tracks": 10,
        "ledger": {"declared": 10, "fetched": 10, "ok": 10, "unavailable": 0, "gap": 0},
        "track_keys": {"index": 10, "status": 10, "title": 10,
                       "duration_ms": 10, "released_at": 10},
        "extra_keys": {"popularity": 10},
        "field_sources": {f: {"platform": 10}
                          for f in ("duration_ms", "language", "released_at",
                                    "collected_at")},
        "parts": ["playlist_detail_v3", "song_detail_000"],
        "warnings": [],
    }
    base.update(changes)
    return base


class TestSummarize(unittest.TestCase):
    def test_it_describes_the_recorded_fixture(self):
        directory = S.fixture_dir("netease")
        got = contract_diff.summarize(directory)
        self.assertEqual(got["dir"], str(directory))
        self.assertEqual(got["provider"], "netease")
        self.assertEqual(got["schema"], "mpa/playlist@1")
        self.assertEqual(got["mode"], S.EXPECTED["netease"]["mode"])
        self.assertEqual(got["adapter_version"], "2.0.0")
        self.assertEqual(got["ok_tracks"], 200)
        self.assertEqual(got["tracks"], 200)
        self.assertEqual(got["ledger"], S.playlist("netease").ledger.to_obj())
        self.assertEqual(got["track_keys"]["index"], 200)
        self.assertEqual(got["track_keys"]["extra"], 200)
        self.assertEqual(got["extra_keys"],
                         {"popularity": 200, "alias": 39, "trans_name": 17},
                         "曲目级 extra 的存在与覆盖数：alias/trans_name 只覆盖一部分曲目，"
                         "漂移检测只看这个键有没有整批消失")
        self.assertEqual(got["field_sources"]["duration_ms"], {"platform": 200})
        self.assertEqual(got["parts"], sorted(S.EXPECTED["netease"]["parts"]))
        self.assertEqual(got["parts"],
                         sorted({r["name"] for r in S.fetch_log("netease")["requests"]}))
        self.assertEqual(got["warnings"], [])

    def test_every_fixture_can_be_summarised(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                got = contract_diff.summarize(S.fixture_dir(provider))
                self.assertEqual(got["provider"], provider)
                self.assertEqual(got["ok_tracks"], S.EXPECTED[provider]["ok"])
                self.assertEqual(got["ledger"]["declared"], S.EXPECTED[provider]["declared"])

    def test_a_directory_without_a_playlist_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                contract_diff.summarize(Path(tmp))

    def test_a_missing_fetch_log_is_tolerated(self):
        """A hand-made directory still summarises; the mode comes from the playlist."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = S.fixture_dir("netease") / PLAYLIST_FILE
            shutil.copy(source, directory / PLAYLIST_FILE)
            got = contract_diff.summarize(directory)
            self.assertEqual(got["mode"], S.playlist("netease").source.mode)
            self.assertEqual(got["parts"], [])


class TestCompareIgnoresValues(unittest.TestCase):
    """The whole point: a live run that only changes numbers must stay green."""

    def test_the_same_summary_is_clean(self):
        one = summary()
        self.assertEqual(contract_diff.compare(one, dict(one)),
                         {"drift": [], "warnings": [], "notes": []})

    def test_a_fixture_compared_with_itself_is_clean(self):
        netease = S.fixture_dir("netease")
        result = contract_diff.compare(contract_diff.summarize(netease),
                                       contract_diff.summarize(netease))
        self.assertEqual(result["drift"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["notes"], [])

    def test_a_different_track_count_is_not_drift(self):
        result = contract_diff.compare(summary(), summary(ok_tracks=7, tracks=7))
        self.assertEqual(result["drift"], [])

    def test_changed_values_do_not_show_up_anywhere(self):
        """Values never enter the summary, so they cannot enter the diff."""
        fixture = contract_diff.summarize(S.fixture_dir("netease"))
        fresh = dict(fixture)
        self.assertEqual(contract_diff.compare(fixture, fresh)["drift"], [])

    def test_the_summary_carries_no_per_track_values(self):
        got = contract_diff.summarize(S.fixture_dir("qq"))
        blob = json.dumps(got, ensure_ascii=False)
        first_title = S.playlist("qq").tracks_ok()[0].title
        self.assertNotIn(first_title, blob,
                         "titles must not reach the summary or a rename reads as drift")


class TestCompareDrift(unittest.TestCase):
    def test_identical_shapes_produce_nothing(self):
        result = contract_diff.compare(summary(), summary())
        self.assertEqual(result, {"drift": [], "warnings": [], "notes": []})

    def test_a_dropped_track_key_is_drift(self):
        fresh = summary(track_keys={"index": 10, "status": 10, "title": 10,
                                    "duration_ms": 10})
        drift = contract_diff.compare(summary(), fresh)["drift"]
        self.assertTrue(any("'released_at' 整批消失" in d for d in drift), drift)

    def test_a_collapsed_field_coverage_is_drift(self):
        fresh = summary(field_sources={**summary()["field_sources"],
                                       "duration_ms": {"unavailable": 10}})
        drift = contract_diff.compare(summary(), fresh)["drift"]
        self.assertTrue(any("duration_ms 的平台覆盖率塌了" in d for d in drift), drift)
        self.assertTrue(any("100% -> 0%" in d for d in drift), drift)

    def test_a_declared_total_turning_into_null_is_drift(self):
        fresh = summary(ledger={"declared": None, "fetched": 10, "ok": 10,
                                "unavailable": 0, "gap": None})
        result = contract_diff.compare(summary(), fresh)
        self.assertTrue(any("ledger.declared 从数字变成 null" in d
                            for d in result["drift"]), result["drift"])
        self.assertTrue(any("没有声明总数" in w for w in result["warnings"]))

    def test_a_total_appearing_later_is_not_drift(self):
        fixture = summary(ledger={"declared": None, "fetched": 10, "ok": 10,
                                  "unavailable": 0, "gap": None})
        result = contract_diff.compare(fixture, summary())
        self.assertEqual(result["drift"], [])

    def test_an_extra_key_disappearing_is_drift(self):
        drift = contract_diff.compare(summary(), summary(extra_keys={}))["drift"]
        self.assertTrue(any("extra 里的平台附加字段 'popularity' 整批消失" in d
                            for d in drift), drift)

    def test_a_part_that_is_no_longer_requested_is_drift(self):
        fresh = summary(parts=["playlist_detail_v3"])
        drift = contract_diff.compare(summary(), fresh)["drift"]
        self.assertTrue(any("少了 part 'song_detail_000'" in d for d in drift), drift)

    def test_a_different_platform_is_drift(self):
        drift = contract_diff.compare(summary(), summary(provider="qq"))["drift"]
        self.assertTrue(any("平台不一致" in d for d in drift), drift)

    def test_an_unexpected_schema_is_drift(self):
        drift = contract_diff.compare(summary(), summary(schema="mpa/playlist@2"))["drift"]
        self.assertTrue(any("schema 变成了" in d for d in drift), drift)


class TestCompareWarningsAndNotes(unittest.TestCase):
    def test_a_route_change_is_only_a_warning(self):
        result = contract_diff.compare(summary(), summary(mode="node-service-external"))
        self.assertEqual(result["drift"], [])
        self.assertTrue(any("路线变了" in w for w in result["warnings"]))

    def test_a_gap_is_only_a_warning(self):
        fresh = summary(ledger={"declared": 10, "fetched": 8, "ok": 8,
                                "unavailable": 0, "gap": 2})
        result = contract_diff.compare(summary(), fresh)
        self.assertEqual(result["drift"], [])
        self.assertTrue(any("gap=2" in w for w in result["warnings"]), result["warnings"])

    def test_a_new_part_is_only_a_warning(self):
        fresh = summary(parts=summary()["parts"] + ["song_detail_001"])
        result = contract_diff.compare(summary(), fresh)
        self.assertEqual(result["drift"], [])
        self.assertTrue(any("多了 part 'song_detail_001'" in w for w in result["warnings"]))

    def test_a_growing_coverage_is_only_a_note(self):
        fresh = summary(field_sources={**summary()["field_sources"]})
        fixture = summary(field_sources={**summary()["field_sources"],
                                         "language": {"platform": 3, "unavailable": 7}})
        result = contract_diff.compare(fixture, fresh)
        self.assertEqual(result["drift"], [])
        self.assertTrue(any("language 的平台覆盖率涨了" in n for n in result["notes"]),
                        result)

    def test_new_track_and_extra_keys_are_only_notes(self):
        fresh = summary(track_keys={**summary()["track_keys"], "popularity_note": 4},
                        extra_keys={**summary()["extra_keys"], "mv_id": 2})
        result = contract_diff.compare(summary(), fresh)
        self.assertEqual(result["drift"], [])
        notes = "\n".join(result["notes"])
        self.assertIn("新增曲目键 'popularity_note'", notes)
        self.assertIn("extra 里新增字段 'mv_id'", notes)


class TestCoverageThreshold(unittest.TestCase):
    """``coverage_drop=0.5``: fixture >= 50 % and fresh below *half* of it."""

    def sources(self, platform: int, unavailable: int) -> dict:
        return {**summary()["field_sources"],
                "language": {"platform": platform, "unavailable": unavailable}}

    def test_a_fixture_below_half_coverage_never_counts_as_drift(self):
        fixture = summary(field_sources=self.sources(4, 6))
        for platform in (0, 2, 4):
            with self.subTest(platform=platform):
                fresh = summary(field_sources=self.sources(platform, 10 - platform))
                self.assertEqual(contract_diff.compare(fixture, fresh)["drift"], [])

    def test_dropping_to_exactly_half_of_the_fixture_is_not_drift(self):
        fixture = summary(field_sources=self.sources(8, 2))     # 80 %
        fresh = summary(field_sources=self.sources(4, 6))       # 40 % = 80 % / 2
        result = contract_diff.compare(fixture, fresh)
        self.assertEqual(result["drift"], [])
        self.assertTrue(any("language 的平台覆盖率变化" in w for w in result["warnings"]))

    def test_dropping_below_half_of_the_fixture_is_drift(self):
        fixture = summary(field_sources=self.sources(8, 2))     # 80 %
        fresh = summary(field_sources=self.sources(3, 7))       # 30 % < 40 %
        drift = contract_diff.compare(fixture, fresh)["drift"]
        self.assertTrue(any("language 的平台覆盖率塌了：80% -> 30%" in d for d in drift),
                        drift)

    def test_the_threshold_is_a_parameter(self):
        fixture = summary(field_sources=self.sources(8, 2))
        fresh = summary(field_sources=self.sources(6, 4))       # 60 %: no drift at 0.5
        self.assertEqual(contract_diff.compare(fixture, fresh)["drift"], [])
        strict = contract_diff.compare(fixture, fresh, coverage_drop=0.9)
        self.assertTrue(any("language" in d for d in strict["drift"]), strict["drift"])

    def test_a_field_the_fixture_never_had_is_not_drift(self):
        fixture = summary(field_sources={"duration_ms": {"platform": 10}})
        fresh = summary(field_sources={"duration_ms": {"platform": 10}})
        self.assertEqual(contract_diff.compare(fixture, fresh)["drift"], [])

    def test_an_empty_run_does_not_divide_by_zero(self):
        fixture = summary(ok_tracks=0, tracks=0, track_keys={}, extra_keys={},
                          field_sources={}, ledger={"declared": None, "fetched": 0,
                                                    "ok": 0, "unavailable": 0, "gap": None})
        fresh = summary(ok_tracks=0, tracks=0, track_keys={}, extra_keys={},
                        field_sources={}, ledger={"declared": None, "fetched": 0,
                                                  "ok": 0, "unavailable": 0, "gap": None})
        self.assertEqual(contract_diff.compare(fixture, fresh)["drift"], [])


class TestMainAndFiles(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def copy_fixture(self, provider: str) -> Path:
        dest = self.tmp / f"{provider}-copy"
        shutil.copytree(S.fixture_dir(provider), dest)
        return dest

    def edit_playlist(self, directory: Path, mutate) -> None:
        path = directory / PLAYLIST_FILE
        obj = json.loads(path.read_text(encoding="utf-8"))
        mutate(obj)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def run_main(self, argv, module=contract_diff):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def test_an_unchanged_copy_exits_zero(self):
        code, out, _ = self.run_main([S.fixture_dir("qq"), self.copy_fixture("qq")])
        self.assertEqual(code, 0)
        self.assertIn("形状一致", out)
        self.assertNotIn("[漂移]", out)

    def test_the_three_structural_changes_exit_one(self):
        fresh = self.copy_fixture("netease")

        def mutate(obj):
            for track in obj["tracks"]:
                track.pop("released_at", None)
                track.pop("extra", None)
            obj["ledger"]["declared"] = None

        self.edit_playlist(fresh, mutate)
        code, out, _ = self.run_main([S.fixture_dir("netease"), fresh])
        self.assertEqual(code, 1)
        self.assertIn("released_at 的平台覆盖率塌了", out)
        # A dropped *canonical* key is not "key missing" drift: Track.from_obj
        # rebuilds it as unavailable, so the coverage collapse is the signal.
        self.assertIn("'extra' 整批消失", out)
        self.assertIn("extra 里的平台附加字段 'popularity' 整批消失", out)
        self.assertIn("ledger.declared 从数字变成 null", out)
        self.assertIn("[漂移]", out)

    def test_value_only_edits_exit_zero(self):
        fresh = self.copy_fixture("netease")

        def mutate(obj):
            first = obj["tracks"][0]
            first["title"] = "换了一首歌"
            first["duration_ms"]["value"] += 1000
            obj["ledger"]["fetched"] = 199   # fewer fetched, same declared

        self.edit_playlist(fresh, mutate)
        code, out, _ = self.run_main([S.fixture_dir("netease"), fresh])
        self.assertEqual(code, 0, out)
        self.assertNotIn("[漂移]", out)

    def test_json_output_is_parseable_and_reports_the_same_verdict(self):
        fresh = self.copy_fixture("netease")
        self.edit_playlist(fresh, lambda obj: [t.pop("duration_ms", None)
                                               for t in obj["tracks"]])
        code, out, _ = self.run_main([S.fixture_dir("netease"), fresh, "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(sorted(payload) , sorted(["fixture", "fresh", "drift",
                                                   "warnings", "notes"]))
        self.assertEqual(payload["fresh"]["provider"], "netease")
        self.assertTrue(payload["drift"])

    def test_an_unreadable_directory_is_a_usage_error_exit_two(self):
        code, _, err = self.run_main([self.tmp / "nope", S.fixture_dir("qq")])
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)

    def test_a_corrupt_playlist_is_exit_two_not_a_traceback(self):
        fresh = self.copy_fixture("qq")
        (fresh / PLAYLIST_FILE).write_text("{ not json", encoding="utf-8")
        code, _, err = self.run_main([S.fixture_dir("qq"), fresh])
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_wrong_argument_count_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                contract_diff.main([str(S.fixture_dir("qq"))])
        self.assertEqual(caught.exception.code, 2)


class TestRederiveFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def copy_fixture(self, provider: str) -> Path:
        dest = self.tmp / provider
        shutil.copytree(S.fixture_dir(provider), dest)
        return dest

    def test_a_clean_copy_is_unchanged_and_verifies(self):
        directory = self.copy_fixture("kugou")
        before = (directory / PLAYLIST_FILE).read_bytes()
        result = rederive_fixture.rederive(directory)
        self.assertFalse(result["changed"])
        self.assertTrue(result["verify_ok"])
        self.assertEqual(result["diffs"], [])
        self.assertEqual(set(result["checks"]), {
            "schema_ok", "manifest_ok", "raw_digest_ok", "adapter_version_ok",
            "derive_deterministic"})
        self.assertTrue(all(result["checks"].values()), result["checks"])
        self.assertEqual(result["ledger_before"], result["ledger_after"])
        self.assertEqual(result["track_count_before"], result["track_count_after"])
        self.assertEqual((directory / PLAYLIST_FILE).read_bytes(), before,
                         "an unchanged re-derivation must not rewrite the file")

    def test_it_repairs_a_playlist_that_drifted_from_its_raw(self):
        directory = self.copy_fixture("kugou")
        expected = S.playlist("kugou").tracks_ok()[0].title
        path = directory / PLAYLIST_FILE
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["tracks"][0]["title"] = "被改坏的标题"
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

        result = rederive_fixture.rederive(directory)
        self.assertTrue(result["changed"])
        self.assertTrue(result["verify_ok"], result["diffs"])
        self.assertEqual(result["track_count_before"], result["track_count_after"])
        repaired = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["tracks"][0]["title"], expected)
        self.assertEqual(S.playlist("kugou").tracks_ok()[0].title, expected,
                         "the committed fixture must not have been touched")

    def test_an_extra_raw_file_breaks_the_verify_step(self):
        directory = self.copy_fixture("kugou")
        (directory / "raw" / "leftover.json").write_text("{}", encoding="utf-8")
        result = rederive_fixture.rederive(directory)
        self.assertFalse(result["verify_ok"])
        self.assertFalse(result["checks"]["manifest_ok"])
        self.assertTrue(result["diffs"], "a failed verify has to say what failed")

    def test_main_exit_codes(self):
        clean = self.copy_fixture("kugou")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rederive_fixture.main([str(clean)])
        self.assertEqual(code, 0)
        self.assertIn("未改写", out.getvalue())

        (clean / "raw" / "leftover.json").write_text("{}", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rederive_fixture.main([str(clean)]), 1)

    def test_json_output_lists_every_directory(self):
        one, two = self.copy_fixture("kugou"), self.copy_fixture("qishui")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rederive_fixture.main([str(one), str(two), "--json"])
        self.assertEqual(code, 0, out.getvalue())
        reports = json.loads(out.getvalue())
        self.assertEqual([r["provider"] for r in reports], ["kugou", "qishui"])
        self.assertTrue(all(r["verify_ok"] for r in reports))

    def test_a_directory_that_is_not_a_dataset_raises_usage_error(self):
        from mpa.errors import InputError
        with self.assertRaises(InputError):
            rederive_fixture.rederive(self.tmp / "nothing-here")


if __name__ == "__main__":
    unittest.main()
