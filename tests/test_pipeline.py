"""``mpa.pipeline``: fetch -> dataset directory, and offline ``verify``.

Two halves:

* ``fetch_playlist`` runs end to end against a fake adapter and a faked
  ``urlopen``, so the *real* HttpClient, RawStore, logging and exit-code paths
  execute - no network, no sleeps (``min_interval=0``, ``time.sleep`` mocked).
* ``verify_playlist`` runs against the recorded fixtures and against deliberately
  corrupted copies of them.  The corrupted cases are the interesting ones: a
  verifier only deserves trust if it fails on the things it claims to catch.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.adapters.base import Adapter
from mpa.errors import (EXIT_OK, EXIT_PARTIAL, DatasetError, InputError, MPError,
                        exit_code_for)
from mpa.adapters.base import build_ledger
from mpa.pipeline import (LOG_FILE, PLAYLIST_FILE, RAW_DIR, Reporter, _exit_for,
                          fetch_playlist, load_dataset, restore_fetch_meta,
                          verify_playlist)
from mpa.runtime import HttpClient

PAYLOAD = {
    "name": "玩具歌单",
    "count": 2,
    "songs": [{"id": 1, "title": "第一首"}, {"id": 2, "title": "第二首"}],
}


class ToyAdapter(S.ToyAdapterBase, Adapter):
    """The support-file adapter plus the shared ``Adapter.fetch`` machinery."""


class NoGhostAdapter(ToyAdapter):
    """Requests a part it does not hand to ``derive`` - the bug verify would find."""

    def collect(self, client, ref, ctx):
        payload = client.get_json("detail", f"http://toy.invalid/{ref.key}")
        client.get_json("unused", "http://toy.invalid/unused")
        return {"detail": payload}


class GhostPartAdapter(ToyAdapter):
    """Hands ``derive`` a part that was never recorded."""

    def collect(self, client, ref, ctx):
        payload = client.get_json("detail", f"http://toy.invalid/{ref.key}")
        return {"detail": payload, "ghost": {"not": "recorded"}}


class PartialAdapter(ToyAdapter):
    """Declares more tracks than it returns, i.e. a gap in the ledger."""

    def derive(self, parts, ctx):
        playlist = super().derive(parts, ctx)
        # 平台说 5 首、只返回 3 首：账本按「实际拿到的曲目 + 平台声明的总数」重算
        playlist.ledger = build_ledger(playlist.tracks, 5)
        return playlist


class NoTotalAdapter(ToyAdapter):
    """The platform answers, but never says how many tracks there should be."""

    def derive(self, parts, ctx):
        playlist = super().derive(parts, ctx)
        playlist.playlist.declared_count = None
        playlist.ledger = build_ledger(playlist.tracks, None)
        return playlist


class PipelineCase(unittest.TestCase):
    """A faked network, a temp output dir, and no real sleeping."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.out = self.tmp / "out"
        self.urlopen = S.install_urlopen(
            self, lambda req, n: S.json_response(PAYLOAD, url=req.full_url))
        self.sleep = mock.patch("time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def use_adapter(self, adapter):
        S.patch_attr(self, "mpa.adapters.all_adapters", lambda **kw: [adapter])
        S.patch_attr(self, "mpa.pipeline.find_adapter",
                     lambda raw, adapters=None: adapter)
        S.patch_attr(self, "mpa.pipeline.adapter_by_name",
                     lambda name, adapters=None: adapter)
        return adapter

    def fetch(self, adapter=None, **kwargs):
        self.use_adapter(adapter or ToyAdapter())
        kwargs.setdefault("min_interval", 0.0)
        kwargs.setdefault("reporter", Reporter(io.StringIO()))
        return fetch_playlist("toy:42", self.out, **kwargs)


# --- fetch ------------------------------------------------------------------

class TestFetchWritesADataset(PipelineCase):
    def test_the_four_artifacts_exist(self):
        result = self.fetch()
        self.assertTrue((self.out / PLAYLIST_FILE).is_file())
        self.assertTrue((self.out / LOG_FILE).is_file())
        self.assertTrue((self.out / RAW_DIR / "_manifest.json").is_file())
        self.assertTrue((self.out / RAW_DIR / "detail.json").is_file())
        self.assertEqual(result.outdir, self.out)

    def test_the_playlist_matches_the_response(self):
        pl = self.fetch().playlist
        self.assertEqual(pl.schema, "mpa/playlist@1")
        self.assertEqual(pl.source.provider, "toy")
        self.assertEqual(pl.source.mode, "toy-direct")
        self.assertEqual(pl.playlist.title, "玩具歌单")
        self.assertEqual([t.title for t in pl.tracks], ["第一首", "第二首"])
        self.assertEqual(pl.ledger.declared, 2)
        self.assertEqual((pl.ledger.fetched, pl.ledger.ok), (2, 2))
        self.assertEqual(pl.source.requests, 1)

    def test_source_accounting_describes_what_was_recorded(self):
        pl = self.fetch().playlist
        store = S.RawStore(self.out / RAW_DIR, write=False)
        store.entries = S.read_json(self.out / RAW_DIR / "_manifest.json")["requests"]
        self.assertTrue(pl.source.raw_saved)
        self.assertEqual(pl.source.raw_digest, store.digest())
        self.assertNotIn("upstream_commit", pl.to_obj()["source"],
                         "the toy adapter never sets ctx.upstream_commit")

    def test_the_log_says_how_it_was_fetched(self):
        self.fetch()
        log = S.read_json(self.out / LOG_FILE)
        stored = S.read_json(self.out / PLAYLIST_FILE)
        self.assertEqual(log["schema"], "mpa/fetchlog@1")
        self.assertEqual(log["provider"], "toy")
        self.assertEqual(log["input"], "toy:42")
        self.assertEqual(log["ref"], {"provider": "toy", "key": "42", "params": {}})
        self.assertEqual(log["mode"], "toy-direct")
        self.assertEqual(log["adapter_version"], "9.9.9")
        self.assertEqual(log["exit_code"], EXIT_OK)
        self.assertEqual(len(log["requests"]), 1)
        self.assertEqual(log["errors"], [])
        self.assertFalse(log["partial_ok"])
        self.assertNotIn("raw_cleanup", log)
        self.assertEqual(log["ctx"]["fetched_at"], log["started_at"])
        self.assertEqual(log["raw_digest"], stored["source"]["raw_digest"])

    def test_the_recorded_request_is_the_one_that_was_made(self):
        self.fetch()
        entry = S.read_json(self.out / LOG_FILE)["requests"][0]
        self.assertEqual(entry["name"], "detail")
        self.assertEqual(entry["url"], "http://toy.invalid/42")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["status"], 200)
        # bytes is the length of the body as it arrived on the wire (the fake
        # serialises with json.dumps defaults), not of the compact re-encoding
        # that RawStore writes to disk.
        wire = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
        self.assertEqual(entry["bytes"], len(wire))
        self.assertEqual((self.out / RAW_DIR / "detail.json").stat().st_size,
                         len(json.dumps(PAYLOAD, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")))

    def test_fetch_forwards_its_http_options_to_the_client(self):
        seen = []

        class Spy(HttpClient):
            def __init__(self, provider, store, **kw):
                seen.append(kw)
                super().__init__(provider, store, **kw)

        S.patch_attr(self, "mpa.pipeline.HttpClient", Spy)
        self.fetch(timeout=3.5, retries=7, min_interval=0.0)
        self.assertEqual(seen, [{"timeout": 3.5, "retries": 7, "min_interval": 0.0}])

    def test_the_timestamp_is_recorded_so_offline_derive_can_repeat_it(self):
        pl = self.fetch().playlist
        log = S.read_json(self.out / LOG_FILE)
        self.assertTrue(pl.source.fetched_at)
        self.assertEqual(pl.source.fetched_at, log["ctx"]["fetched_at"])
        self.assertEqual(pl.source.fetched_at, log["started_at"])

    def test_no_raw_leaves_no_raw_directory_and_no_digest(self):
        result = self.fetch(save_raw=False)
        self.assertFalse((self.out / RAW_DIR).exists())
        self.assertFalse(result.playlist.source.raw_saved)
        self.assertIsNone(result.playlist.source.raw_digest)
        log = S.read_json(self.out / LOG_FILE)
        self.assertFalse(log["raw_saved"])
        self.assertIsNone(log["raw_digest"])
        self.assertEqual(len(log["requests"]), 1, "the request is still logged")

    def test_provider_argument_selects_the_adapter_by_name(self):
        self.use_adapter(ToyAdapter())
        result = fetch_playlist("toy:42", self.out, provider="toy", min_interval=0.0,
                                reporter=Reporter(io.StringIO()))
        self.assertEqual(result.playlist.source.provider, "toy")

    def test_unknown_provider_is_a_usage_error_before_any_request(self):
        with self.assertRaises(InputError) as ctx:
            fetch_playlist("toy:42", self.out, provider="nope", min_interval=0.0)
        self.assertIn("未知的 provider: nope", ctx.exception.message)
        self.assertEqual(self.urlopen.requests, [])

    def test_a_bare_numeric_id_needs_a_provider(self):
        with self.assertRaises(InputError) as ctx:
            fetch_playlist("123456", self.out, min_interval=0.0)
        self.assertIn("--provider", ctx.exception.message)
        self.assertEqual(self.urlopen.requests, [])


class TestFetchReportsProgress(PipelineCase):
    def test_reporter_lines_tell_which_adapter_and_what_landed(self):
        stream = io.StringIO()
        self.fetch(reporter=Reporter(stream))
        text = stream.getvalue()
        self.assertIn("[toy] 使用适配器 9.9.9，模式 toy-direct", text)
        self.assertIn("[toy] 2 首，ledger=", text)

    def test_quiet_reporter_prints_nothing(self):
        stream = io.StringIO()
        self.fetch(reporter=Reporter(stream, quiet=True))
        self.assertEqual(stream.getvalue(), "")

    def test_a_reporter_without_a_stream_is_a_no_op(self):
        self.assertIsNone(Reporter()("ignored"))

    def test_partial_ok_is_a_log_fact_not_a_warning(self):
        stream = io.StringIO()
        result = self.fetch(PartialAdapter(), partial_ok=True, reporter=Reporter(stream))
        log = S.read_json(self.out / LOG_FILE)
        self.assertTrue(log["partial_ok"])
        self.assertEqual(log["extra"]["partial_ok"], True)
        self.assertEqual(result.playlist.warnings, [],
                         "warnings must stay reproducible from raw/")
        self.assertEqual(log["exit_code"], EXIT_PARTIAL,
                         "accepting a gap does not turn it into success")
        self.assertIn("--partial-ok", stream.getvalue())
        self.assertIn("缺口 3 首", stream.getvalue())

    def test_partial_ok_prints_nothing_when_there_is_no_gap(self):
        stream = io.StringIO()
        self.fetch(partial_ok=True, reporter=Reporter(stream))
        self.assertNotIn("--partial-ok", stream.getvalue())


class TestFetchCatchesInconsistentAdapters(PipelineCase):
    def test_a_part_that_was_recorded_but_not_derived_is_an_adapter_bug(self):
        with self.assertRaises(MPError) as ctx:
            self.fetch(NoGhostAdapter())
        self.assertIn("离线复算会失败", ctx.exception.message)
        self.assertIn("unused", ctx.exception.message)

    def test_a_part_handed_to_derive_but_never_recorded_is_an_adapter_bug(self):
        with self.assertRaises(MPError) as ctx:
            self.fetch(GhostPartAdapter())
        self.assertIn("离线复算会失败", ctx.exception.message)
        self.assertIn("ghost", ctx.exception.message)

    def test_the_check_is_skipped_when_nothing_is_written(self):
        result = self.fetch(NoGhostAdapter(), save_raw=False)
        self.assertEqual(len(result.playlist.tracks), 2,
                         "with --no-raw there is nothing to compare against")


class TestFetchIntoAnExistingDirectory(PipelineCase):
    def test_stale_responses_from_an_earlier_recording_are_removed_and_logged(self):
        stream = io.StringIO()
        self.fetch(reporter=Reporter(stream))
        self.assertTrue((self.out / RAW_DIR / "detail.json").is_file())

        class Renamed(ToyAdapter):
            def collect(self, client, ref, ctx):
                payload = client.get_json("detail_v2", f"http://toy.invalid/{ref.key}")
                return {"detail_v2": payload}

            def derive(self, parts, ctx):
                return super().derive({"detail": parts["detail_v2"]}, ctx)

        self.use_adapter(Renamed())
        result = fetch_playlist("toy:42", self.out, min_interval=0.0,
                                reporter=Reporter(stream))
        self.assertFalse((self.out / RAW_DIR / "detail.json").exists(),
                         "raw/ must describe this run and nothing else")
        self.assertEqual(result.log["raw_cleanup"], ["detail"])
        self.assertIn("清掉了 1 个上一次抓取留下的响应文件：detail", stream.getvalue())
        self.assertTrue(verify_playlist(self.out).ok)

    def test_a_second_identical_fetch_has_nothing_to_clean(self):
        self.fetch()
        result = self.fetch()
        self.assertNotIn("raw_cleanup", result.log)
        self.assertTrue(verify_playlist(self.out).ok)


class TestBrokenPipes(PipelineCase):
    """管道断掉不该改变命令的结果。

    真实翻过的一次车：`except BrokenPipeError: return EXIT_OK` 包着整个 `main()`，
    于是 `mpa fetch ... 2>&1 | head -1` 会因为 stderr 上的 EPIPE 中途中断抓取，
    却报退出码 0——半份数据 + 成功退出码，正是这个项目最不能出的错。
    """

    def test_the_reporter_swallows_a_broken_pipe_and_goes_quiet(self):
        stream = S.BrokenPipeStream()
        reporter = Reporter(stream)
        reporter("第一条")                        # 不许抛
        reporter("第二条")
        self.assertEqual(stream.writes, 1, "断管之后不再试第二次")
        self.assertIsNone(reporter.stream)

    def test_a_broken_stderr_does_not_abort_a_fetch(self):
        """抓取照常完成、文件照常落盘、退出码仍由账本决定。"""
        result = self.fetch(reporter=Reporter(S.BrokenPipeStream()))
        self.assertTrue((self.out / PLAYLIST_FILE).is_file())
        self.assertTrue((self.out / RAW_DIR / "_manifest.json").is_file())
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertTrue(verify_playlist(self.out).ok)


class TestExitCodes(unittest.TestCase):
    def test_a_complete_playlist_is_success(self):
        self.assertEqual(_exit_for(S.playlist("netease")), EXIT_OK)

    def test_a_gap_is_partial(self):
        pl = S.playlist("netease")
        pl.ledger.declared = pl.ledger.fetched + 1
        self.assertEqual(_exit_for(pl), EXIT_PARTIAL)

    def test_unavailable_tracks_alone_are_not_a_gap(self):
        pl = S.playlist("netease")
        pl.ledger.unavailable = 1
        self.assertEqual(pl.ledger.gap, 0)
        self.assertEqual(_exit_for(pl), EXIT_OK)

    def test_an_unknown_total_is_partial_even_when_nothing_is_missing(self):
        """"We got everything" is not a claim you can make without a total."""
        pl = S.playlist("netease")
        pl.ledger.declared = None
        self.assertIsNone(pl.ledger.gap)
        self.assertFalse(pl.ledger.complete)
        self.assertEqual(_exit_for(pl), EXIT_PARTIAL)


# --- a provider that never declares a total ---------------------------------

class TestUndeclaredTotals(PipelineCase):
    """``declared is None`` must be loud: warning + exit 1, never silently "ok".

    The adapters used to fall back to ``or len(...)``, which turned "the platform
    did not say" into "we got them all" - the exact failure mode this tool
    exists to prevent.
    """

    WARNING = ("平台没有给出曲目总数，只取到 {n} 首：这份数据无法证明完整"
               "（ledger.declared 为 null）")

    def fetch_without_a_total(self, **kwargs):
        payload = {"name": PAYLOAD["name"], "count": None, "songs": PAYLOAD["songs"]}
        S.install_urlopen(self, lambda req, n: S.json_response(payload, url=req.full_url))
        return self.fetch(NoTotalAdapter(), **kwargs)

    def test_the_fetch_records_the_unknown_total_and_exits_one(self):
        result = self.fetch_without_a_total()
        self.assertEqual(result.exit_code, EXIT_PARTIAL)
        self.assertIn(self.WARNING.format(n=2), result.playlist.warnings)
        self.assertEqual(result.playlist.ledger.to_obj(),
                         {"declared": None, "fetched": 2, "ok": 2, "unavailable": 0,
                          "gap": None})
        self.assertFalse(result.playlist.ledger.complete)

    def test_the_written_dataset_says_the_same_thing(self):
        self.fetch_without_a_total()
        stored = S.read_json(self.out / PLAYLIST_FILE)
        self.assertIsNone(stored["ledger"]["declared"])
        self.assertIsNone(stored["ledger"]["gap"])
        self.assertIn("gap", stored["ledger"], "null gap is serialised, not dropped")
        self.assertEqual(S.read_json(self.out / LOG_FILE)["exit_code"], EXIT_PARTIAL)

    def test_partial_ok_does_not_turn_an_unknown_total_into_success(self):
        result = self.fetch_without_a_total(partial_ok=True)
        self.assertEqual(result.exit_code, EXIT_PARTIAL)
        log = S.read_json(self.out / LOG_FILE)
        self.assertTrue(log["partial_ok"])
        self.assertEqual(log["extra"]["partial_ok"], True)


class TestAdaptersWithoutAFallbackTotal(unittest.TestCase):
    """Delete the declared total from a recorded response and re-derive.

    This is the offline equivalent of "the platform stopped sending the
    number": whatever the adapter does, the ledger has to admit it does not
    know, the warning has to appear, and the exit code has to become 1.  The
    fixtures on disk are never touched - ``load_all()`` hands out fresh dicts.
    """

    WARNING = ("平台没有给出曲目总数，只取到 {n} 首：这份数据无法证明完整"
               "（ledger.declared 为 null）")

    def rederive_without(self, provider, remove):
        parts = S.raw_store(provider).parts
        remove(parts)
        return S.adapter(provider).derive(parts, S.ctx(provider))

    def assert_undeclared(self, playlist, n, extra_warnings=()):
        self.assertEqual(playlist.ledger.to_obj(),
                         {"declared": None, "fetched": n, "ok": n, "unavailable": 0,
                          "gap": None})
        self.assertFalse(playlist.ledger.complete)
        self.assertEqual(_exit_for(playlist), EXIT_PARTIAL)
        self.assertIn(self.WARNING.format(n=n), playlist.warnings)
        for fragment in extra_warnings:
            self.assertTrue(any(fragment in w for w in playlist.warnings),
                            f"{fragment!r} not in {playlist.warnings}")

    def test_kugou_without_playlist_count(self):
        pl = self.rederive_without(
            "kugou", lambda parts: parts["playlist_detail"]["data"][0].pop("count"))
        self.assert_undeclared(pl, 50)

    def test_netease_without_trackcount(self):
        pl = self.rederive_without(
            "netease", lambda parts: parts["playlist_detail_v3"]["playlist"].pop("trackCount"))
        self.assert_undeclared(pl, 200, extra_warnings=("歌单声明 None 首",))

    def test_qq_without_total_song_num_or_songnum(self):
        def remove(parts):
            entry = parts["playlist_fcg"]["cdlist"][0]
            entry.pop("total_song_num")
            entry.pop("songnum")
        pl = self.rederive_without("qq", remove)
        self.assert_undeclared(pl, 76)

    def test_the_fixtures_on_disk_still_declare_their_total(self):
        """The mutations above must not have leaked into the recordings."""
        for provider, n in (("kugou", 50), ("netease", 200), ("qq", 76)):
            with self.subTest(provider=provider):
                self.assertEqual(S.playlist(provider).ledger.declared, n)


# --- load_dataset -----------------------------------------------------------

class TestLoadDataset(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)

    def test_a_missing_playlist_json_says_it_is_not_an_output_dir(self):
        with self.assertRaises(InputError) as ctx:
            load_dataset(self.dir)
        self.assertIn("playlist.json 不存在", ctx.exception.message)
        self.assertIn("这不是一个 playlist-dig 输出目录", ctx.exception.message)

    def test_a_missing_fetch_log_says_why_it_is_needed(self):
        shutil.copy(S.fixture_dir("kugou") / PLAYLIST_FILE, self.dir / PLAYLIST_FILE)
        with self.assertRaises(InputError) as ctx:
            load_dataset(self.dir)
        self.assertIn("fetch.log.json 不存在", ctx.exception.message)
        self.assertIn("无法知道当初是怎么取的", ctx.exception.message)

    def test_both_files_load_as_objects(self):
        src = S.fixture_dir("kugou")
        shutil.copy(src / PLAYLIST_FILE, self.dir / PLAYLIST_FILE)
        shutil.copy(src / LOG_FILE, self.dir / LOG_FILE)
        playlist, log = load_dataset(self.dir)
        self.assertEqual(playlist.source.provider, "kugou")
        self.assertEqual(log["provider"], "kugou")


# --- verify: the recorded fixtures ------------------------------------------

class TestVerifyFixtures(unittest.TestCase):
    def test_every_fixture_verifies_clean(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                result = verify_playlist(S.fixture_dir(provider))
                self.assertTrue(result.ok)
                self.assertEqual(result.diffs, [])
                self.assertEqual(result.checks, {
                    "schema_ok": True, "manifest_ok": True, "raw_digest_ok": True,
                    "adapter_version_ok": True, "derive_deterministic": True})
                self.assertEqual(result.required, ("schema_ok", "manifest_ok",
                                                   "raw_digest_ok",
                                                   "derive_deterministic"))

    def test_strict_verification_also_passes(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                result = verify_playlist(S.fixture_dir(provider), strict=True)
                self.assertTrue(result.ok, result.diffs)
                self.assertEqual(result.required,
                                 ("schema_ok", "manifest_ok", "raw_digest_ok",
                                  "derive_deterministic", "adapter_version_ok"))

    def test_verify_does_not_touch_the_fixture(self):
        before = {p: sorted(os.listdir(S.fixture_dir(p) / RAW_DIR))
                  for p in S.PROVIDERS}
        for provider in S.PROVIDERS:
            verify_playlist(S.fixture_dir(provider), strict=True)
        after = {p: sorted(os.listdir(S.fixture_dir(p) / RAW_DIR))
                 for p in S.PROVIDERS}
        self.assertEqual(before, after)


# --- verify: tampering ------------------------------------------------------

class TamperCase(unittest.TestCase):
    provider = "kugou"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name) / "copy"
        shutil.copytree(S.fixture_dir(self.provider), self.dir)

    def playlist_obj(self):
        return S.read_json(self.dir / PLAYLIST_FILE)

    def write_playlist(self, obj):
        (self.dir / PLAYLIST_FILE).write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def write_log(self, obj):
        (self.dir / LOG_FILE).write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def first_raw_file(self):
        names = sorted(p for p in (self.dir / RAW_DIR).glob("*.json")
                       if p.name != "_manifest.json")
        self.assertTrue(names, "the fixture has no recorded responses")
        return names[0].name


class TestLoadDatasetRefusesBrokenFiles(unittest.TestCase):
    """「文件不在」与「文件坏了」是两件事：前者退出码 4，后者 2（一行中文，不是 traceback）。

    这条规矩来自真实缺陷：`playlist.json` 被人改坏之后，当时的统计命令会直接抛
    `JSONDecodeError` 的 traceback——数据集坏了却说不出「哪儿坏了、怎么修」。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mpa-broken-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copytree(S.fixture_dir("kugou"), self.dir / "ds")
        self.ds = self.dir / "ds"

    def test_a_missing_directory_is_input_not_data(self):
        with self.assertRaises(InputError):
            load_dataset(self.dir / "nope")

    def test_a_corrupt_playlist_json_is_a_dataset_error_with_the_fix(self):
        (self.ds / "playlist.json").write_text("{ 坏", encoding="utf-8")
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(self.ds)
        self.assertIn("不是合法 JSON", ctx.exception.message)
        self.assertIn("rederive_fixture.py", ctx.exception.message)
        self.assertEqual(exit_code_for(ctx.exception), 2)

    def test_a_corrupt_fetch_log_is_a_dataset_error_too(self):
        (self.ds / "fetch.log.json").write_text("", encoding="utf-8")
        with self.assertRaises(DatasetError):
            load_dataset(self.ds)

    def test_a_json_array_at_the_top_level_is_refused(self):
        (self.ds / "playlist.json").write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(DatasetError) as ctx:
            load_dataset(self.ds)
        self.assertIn("顶层不是 JSON 对象", ctx.exception.message)

    def test_an_intact_dataset_still_loads(self):
        playlist, log = load_dataset(self.ds)
        self.assertEqual(len(playlist.tracks), 50)
        self.assertEqual(log["provider"], "kugou")


class TestVerifyTamperedPlaylist(TamperCase):
    def test_an_edited_field_fails_the_derive_check_with_a_line_number(self):
        obj = self.playlist_obj()
        obj["tracks"][3]["title"] = "被人改过的标题"
        self.write_playlist(obj)
        result = verify_playlist(self.dir)
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["derive_deterministic"])
        self.assertTrue(result.checks["manifest_ok"])
        self.assertEqual(len(result.diffs), 1)
        self.assertRegex(result.diffs[0], r"第 \d+ 行，第 \d+ 列")
        self.assertIn("被人改过的标题", result.diffs[0])

    def test_an_edited_ledger_fails_the_derive_check(self):
        obj = self.playlist_obj()
        obj["ledger"]["ok"] += 1
        self.write_playlist(obj)
        self.assertFalse(
            verify_playlist(self.dir).checks["derive_deterministic"])

    def test_a_changed_schema_is_reported_and_fails_by_default(self):
        obj = self.playlist_obj()
        obj["schema"] = "mpa/playlist@2"
        self.write_playlist(obj)
        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["schema_ok"])
        self.assertIn("schema_ok", result.required)
        self.assertFalse(result.ok)
        self.assertTrue(any("schema" in d for d in result.diffs), result.diffs)
        self.assertFalse(verify_playlist(self.dir, strict=True).ok)

    def test_an_unknown_provider_cannot_be_verified(self):
        obj = self.playlist_obj()
        obj["source"]["provider"] = "not-a-platform"
        self.write_playlist(obj)
        with self.assertRaises(InputError) as ctx:
            verify_playlist(self.dir)
        self.assertIn("未知的 provider: not-a-platform", ctx.exception.message)


class TestVerifyTamperedRaw(TamperCase):
    def test_an_edited_response_body_fails_the_manifest_check(self):
        name = self.first_raw_file()
        path = self.dir / RAW_DIR / name
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["_tampered"] = True
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        result = verify_playlist(self.dir)
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["manifest_ok"])
        self.assertIn(f"{name} 的 sha256 与 manifest 不符", result.diffs[0])

    def test_a_deleted_response_body_is_reported_as_a_broken_dataset(self):
        """A dataset missing a part must not escape as ``FileNotFoundError``.

        Regression: ``verify_playlist`` recorded ``manifest_ok = False`` (with
        the message asserted below) but then called ``RawStore.load_all()``,
        whose ``load_part`` raised a bare ``FileNotFoundError``.  ``mpa/cli.py``
        only catches ``MPError``, so ``mpa verify`` on such a directory printed a
        traceback instead of one line of Chinese and exit code 2.
        """
        name = self.first_raw_file()
        (self.dir / RAW_DIR / name).unlink()
        with self.assertRaises(DatasetError) as ctx:
            verify_playlist(self.dir)
        self.assertIn(name[: -len(".json")], str(ctx.exception))
        self.assertEqual(exit_code_for(ctx.exception), 2)
        store = S.RawStore(self.dir / RAW_DIR, write=False)
        ok, message = store.check_manifest()
        self.assertFalse(ok, "the missing file is detected by check_manifest()")
        self.assertIn("缺失（manifest 里有记录）", message)

    def test_an_extra_file_fails_the_manifest_check(self):
        (self.dir / RAW_DIR / "leftover_v1.json").write_text("{}", encoding="utf-8")
        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["manifest_ok"])
        self.assertTrue(any("manifest 未记录的文件：leftover_v1" in d
                            for d in result.diffs), result.diffs)
        self.assertFalse(result.ok)

    def test_a_hidden_file_is_not_a_leftover(self):
        (self.dir / RAW_DIR / ".DS_Store").write_text("junk", encoding="utf-8")
        result = verify_playlist(self.dir)
        self.assertTrue(result.ok, result.diffs)

    def test_a_rewritten_manifest_still_fails_on_its_own_digest(self):
        name = self.first_raw_file()
        path = self.dir / RAW_DIR / name
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["_tampered"] = True
        blob = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.write_bytes(blob)
        manifest_path = self.dir / RAW_DIR / "_manifest.json"
        manifest = S.read_json(manifest_path)
        for entry in manifest["requests"]:
            if entry["name"] == name[:-len(".json")]:
                entry["bytes"] = len(blob)
                entry["sha256"] = hashlib.sha256(blob).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["manifest_ok"])
        self.assertIn("manifest 的 digest 与各项 sha256 不一致", result.diffs[0])

    def test_a_missing_raw_directory_is_a_usage_error(self):
        shutil.rmtree(self.dir / RAW_DIR)
        with self.assertRaises(InputError) as ctx:
            verify_playlist(self.dir)
        self.assertIn("没有原始响应", ctx.exception.message)
        self.assertIn("--no-raw", ctx.exception.message)

    def test_an_empty_raw_directory_is_a_usage_error(self):
        shutil.rmtree(self.dir / RAW_DIR)
        (self.dir / RAW_DIR).mkdir()
        with self.assertRaises(InputError) as ctx:
            verify_playlist(self.dir)
        self.assertIn("没有原始响应", ctx.exception.message)


class TestVerifyDigestSemantics(TamperCase):
    def test_a_forged_raw_digest_fails_even_without_strict(self):
        """A digest that does not match disk is a failure, not a footnote."""
        obj, log = self.playlist_obj(), S.read_json(self.dir / LOG_FILE)
        obj["source"]["raw_digest"] = "0" * 64
        log["raw_digest"] = "0" * 64
        self.write_playlist(obj)
        self.write_log(log)
        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["raw_digest_ok"])
        self.assertIn("raw_digest_ok", result.required)
        self.assertTrue(result.checks["manifest_ok"])
        self.assertTrue(result.checks["derive_deterministic"])
        self.assertTrue(any("raw 摘要不一致" in d for d in result.diffs), result.diffs)
        self.assertFalse(result.ok)
        self.assertFalse(verify_playlist(self.dir, strict=True).ok)

    def test_reordering_the_requests_is_not_silently_accepted(self):
        manifest = S.read_json(self.dir / RAW_DIR / "_manifest.json")
        manifest["requests"] = list(reversed(manifest["requests"]))
        for i, entry in enumerate(manifest["requests"], start=1):
            entry["seq"] = i
        (self.dir / RAW_DIR / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["raw_digest_ok"],
                         "the stored digest was computed over a different seq order")


class TestVerifyAdapterVersion(TamperCase):
    def test_a_version_bump_makes_the_rebuild_differ(self):
        """Only the playlist's version changed: the *data* no longer rebuilds.

        ``verify`` restores ``source.adapter_version`` from the log before
        comparing, so this is really a playlist-vs-log disagreement.
        """
        obj = self.playlist_obj()
        obj["source"]["adapter_version"] = "1.0.0"
        self.write_playlist(obj)
        result = verify_playlist(self.dir)
        self.assertTrue(any("适配器版本不同" in d for d in result.diffs), result.diffs)
        self.assertFalse(result.checks["adapter_version_ok"])
        self.assertFalse(result.checks["derive_deterministic"])
        self.assertFalse(result.ok)

    def test_both_sides_agreeing_on_an_old_version_is_only_a_warning(self):
        """Data fetched by an older adapter stays verifiable; --strict tightens it."""
        obj, log = self.playlist_obj(), S.read_json(self.dir / LOG_FILE)
        self.assertEqual(log["adapter_version"], "2.0.0")
        obj["source"]["adapter_version"] = "1.9.9"
        log["adapter_version"] = "1.9.9"
        self.write_playlist(obj)
        self.write_log(log)

        result = verify_playlist(self.dir)
        self.assertFalse(result.checks["adapter_version_ok"])
        self.assertNotIn("adapter_version_ok", result.required)
        self.assertTrue(result.checks["derive_deterministic"], result.diffs)
        self.assertTrue(result.ok, result.diffs)
        self.assertEqual(result.diffs,
                         ["适配器版本不同：数据由 1.9.9 生成，当前是 2.0.0"
                          "（默认仍然算通过，--strict 下算失败）"])

        strict = verify_playlist(self.dir, strict=True)
        self.assertIn("adapter_version_ok", strict.required)
        self.assertFalse(strict.ok)

    def test_a_fixture_rebuilt_by_the_current_adapter_reports_no_warning(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                dir_ = Path(self.tmpdir.name) / provider
                shutil.copytree(S.fixture_dir(provider), dir_)
                result = verify_playlist(dir_)
                self.assertTrue(result.checks["adapter_version_ok"])
                self.assertEqual(result.diffs, [])


class TestRestoreFetchMeta(unittest.TestCase):
    """``restore_fetch_meta`` is what lets ``verify`` ask "did the data change?"

    ``raw_saved`` / ``raw_digest`` / ``requests`` / ``adapter_version`` describe
    the *fetch*; a re-derivation in memory cannot know them.  If this function
    ever drifts from what ``verify`` compares, every fixture starts failing for
    the wrong reason - so the last test here re-derives each recording, wipes
    those four fields, restores them from ``fetch.log.json`` and demands the
    whole playlist come back identical.
    """

    def playlist(self):
        return S.playlist("kugou")

    def test_it_puts_the_four_fetch_fields_back(self):
        playlist = self.playlist()
        log = {"raw_saved": True, "raw_digest": "abc123",
               "requests": [{}, {}, {}], "adapter_version": "9.9.9"}
        returned = restore_fetch_meta(playlist, log)
        self.assertIs(returned, playlist)
        self.assertTrue(playlist.source.raw_saved)
        self.assertEqual(playlist.source.raw_digest, "abc123")
        self.assertEqual(playlist.source.requests, 3)
        self.assertEqual(playlist.source.adapter_version, "9.9.9")

    def test_raw_saved_is_a_boolean_not_the_log_value(self):
        playlist = self.playlist()
        restore_fetch_meta(playlist, {"raw_saved": 0})
        self.assertIs(playlist.source.raw_saved, False)
        restore_fetch_meta(playlist, {"raw_saved": "yes"})
        self.assertIs(playlist.source.raw_saved, True)
        restore_fetch_meta(playlist, {})
        self.assertIs(playlist.source.raw_saved, False)

    def test_a_missing_request_list_means_zero_requests(self):
        playlist = self.playlist()
        restore_fetch_meta(playlist, {})
        self.assertEqual(playlist.source.requests, 0)
        restore_fetch_meta(playlist, {"requests": None})
        self.assertEqual(playlist.source.requests, 0)

    def test_the_adapter_version_falls_back_to_the_derived_one(self):
        playlist = self.playlist()
        playlist.source.adapter_version = "2.0.0"
        restore_fetch_meta(playlist, {"adapter_version": None})
        self.assertEqual(playlist.source.adapter_version, "2.0.0")
        restore_fetch_meta(playlist, {})
        self.assertEqual(playlist.source.adapter_version, "2.0.0")

    def test_a_missing_digest_is_restored_as_none(self):
        playlist = self.playlist()
        playlist.source.raw_digest = "stale"
        restore_fetch_meta(playlist, {})
        self.assertIsNone(playlist.source.raw_digest)

    def test_it_does_not_touch_the_log(self):
        playlist = self.playlist()
        log = {"raw_saved": True, "raw_digest": "abc", "requests": [{}],
               "adapter_version": "2.0.0"}
        before = json.loads(json.dumps(log))
        restore_fetch_meta(playlist, log)
        self.assertEqual(log, before)

    def test_restoring_a_fresh_derive_reproduces_every_recording(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                playlist = S.adapter(provider).derive(S.raw_store(provider).parts,
                                                      S.ctx(provider))
                playlist.source.raw_saved = False
                playlist.source.raw_digest = None
                playlist.source.requests = 0
                restore_fetch_meta(playlist, S.fetch_log(provider))
                self.assertEqual(playlist.to_obj(), S.playlist(provider).to_obj())


if __name__ == "__main__":
    unittest.main()
