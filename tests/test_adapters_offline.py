"""Adapters, offline: ``derive(parts, ctx)`` against the recorded fixtures.

This is the strongest test in the suite.  For each recorded live run it reloads
``raw/`` from disk and re-runs the pure half of the adapter, then demands the
result be *byte identical* to the ``playlist.json`` that run produced.  Anything
the adapter does that is not a function of (parts, ctx) - a clock read, a file
read, a dict-iteration-order dependency - shows up here as a diff.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.adapters import adapter_by_name, all_adapters  # noqa: E402
from mpa.errors import KIND_UPSTREAM_CHANGED, UpstreamChanged, exit_code_for  # noqa: E402
from mpa.export import canonical_json  # noqa: E402
from mpa.model import ADAPTER_VERSION, SRC_PLATFORM, SRC_UNAVAILABLE, TRACK_FIELDS  # noqa: E402

CANON = ("duration_ms", "language", "released_at", "collected_at")


def recorded_text(provider: str, name: str) -> str:
    return (S.fixture_dir(provider) / name).read_text(encoding="utf-8")


def rebuilt_text(provider: str, parts=None) -> str:
    """What `mpa verify` compares against playlist.json, as written to disk."""
    playlist = S.adapter(provider).derive(parts if parts is not None
                                          else S.raw_store(provider).parts,
                                          S.ctx(provider))
    log = S.fetch_log(provider)
    playlist.source.raw_saved = bool(log["raw_saved"])
    playlist.source.raw_digest = log["raw_digest"]
    playlist.source.requests = len(log["requests"])
    return canonical_json(playlist.to_obj())


class _NoClockDatetime:
    """``mpa.timeutil``'s datetime module, minus the ways to read a clock.

    Only ``now``/``today``/``utcnow`` are booby trapped.  ``fromtimestamp`` with
    an explicit timezone is a pure normalisation of a value that is already in
    ``parts`` - it must keep working, or the adapter could not turn a recorded
    timestamp into a date at all.
    """

    def __getattr__(self, name):
        if name in ("now", "today", "utcnow"):
            raise AssertionError(f"derive() read the clock: datetime.{name}")
        return getattr(datetime, name)


class TestDeriveReproducesTheRecording(unittest.TestCase):
    def test_every_fixture_rebuilds_byte_for_byte(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(rebuilt_text(provider),
                                 recorded_text(provider, "playlist.json"))

    def test_derive_is_a_pure_function(self):
        """No clock, no filesystem, no network - twice, with all three deadly."""
        boom = AssertionError("derive() is not pure")
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                parts, ctx = S.raw_store(provider).parts, S.ctx(provider)
                adapter = S.adapter(provider)
                with mock.patch("time.time", side_effect=boom), \
                        mock.patch("time.time_ns", side_effect=boom), \
                        mock.patch("time.monotonic", side_effect=boom), \
                        mock.patch("mpa.timeutil._dt", _NoClockDatetime()), \
                        mock.patch("mpa.adapters.base.now_iso", side_effect=boom), \
                        mock.patch("urllib.request.urlopen", side_effect=boom), \
                        mock.patch("builtins.open", side_effect=boom):
                    first = canonical_json(adapter.derive(parts, ctx).to_obj())
                    second = canonical_json(adapter.derive(parts, ctx).to_obj())
                self.assertEqual(first, second)

    def test_the_ctx_from_the_log_is_enough(self):
        """A fixture whose ctx has to be doctored would not be verifiable."""
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                ctx = S.ctx(provider)
                self.assertEqual(ctx.input, S.fetch_log(provider)["input"])
                self.assertEqual(ctx.mode, S.fetch_log(provider)["mode"])
                self.assertEqual(ctx.fetched_at, S.EXPECTED[provider]["fetched_at"])


class TestRecordedLedger(unittest.TestCase):
    def test_the_ledger_describes_the_tracks_it_ships_with(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                pl = S.playlist(provider)
                expected = S.EXPECTED[provider]
                self.assertEqual(pl.ledger.declared, expected["declared"])
                self.assertEqual(pl.ledger.fetched, len(pl.tracks))
                self.assertEqual(pl.ledger.fetched, expected["fetched"])
                self.assertEqual(pl.ledger.ok, len(pl.tracks_ok()))
                self.assertEqual(pl.ledger.ok, expected["ok"])
                self.assertEqual(pl.ledger.gap, 0)
                self.assertEqual(pl.ledger.unavailable, 0)
                self.assertEqual(pl.ledger.duplicates_removed, 0)
                self.assertEqual(pl.ledger.reasons, {})
                self.assertTrue(pl.ledger.complete)

    def test_the_source_block_matches_the_fetch_log(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                pl, log = S.playlist(provider), S.fetch_log(provider)
                expected = S.EXPECTED[provider]
                self.assertEqual(pl.source.provider, provider)
                self.assertEqual(pl.source.mode, expected["mode"])
                self.assertEqual(pl.source.mode, log["mode"])
                self.assertEqual(pl.source.mode, log["ctx"]["mode"])
                self.assertEqual(pl.source.adapter_version, expected["adapter_version"])
                self.assertEqual(pl.source.fetched_at, expected["fetched_at"])
                self.assertEqual(pl.source.fetched_at, log["started_at"])
                self.assertEqual(pl.source.requests, len(log["requests"]))
                self.assertTrue(pl.source.raw_saved)
                self.assertEqual(pl.source.raw_digest, expected["digest"])
                self.assertEqual(pl.source.raw_digest, S.manifest(provider)["digest"])
                # The playlist carries what derive() saw; the log's own field
                # falls back to the adapter's pinned service commit when no node
                # service was involved (qishui here fetched over SSR only).
                self.assertEqual(pl.source.upstream_commit, expected["upstream_commit"])
                self.assertEqual(pl.source.upstream_commit,
                                 log["ctx"]["upstream_commit"])
                self.assertEqual(log["upstream_commit"],
                                 log["ctx"]["upstream_commit"]
                                 or S.adapter(provider).upstream_commit)

    def test_no_warnings_mean_no_warnings(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(tuple(S.playlist(provider).warnings),
                                 S.EXPECTED[provider]["warnings"])
                self.assertEqual(S.playlist(provider).warnings,
                                 S.fetch_log(provider)["warnings"])


class TestRecordedFields(unittest.TestCase):
    def test_capabilities_are_recorded_and_are_the_adapters_own(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                pl = S.playlist(provider)
                self.assertEqual(pl.capabilities, S.EXPECTED[provider]["capabilities"])
                self.assertEqual(pl.capabilities, S.adapter(provider).capabilities)

    def test_unavailability_is_explained_per_track(self):
        for provider, unavailable in S.UNAVAILABLE_FIELDS.items():
            with self.subTest(provider=provider):
                for track in S.playlist(provider).tracks:
                    for field in TRACK_FIELDS:
                        cell = getattr(track, field)
                        if field in unavailable:
                            self.assertEqual(cell.source, SRC_UNAVAILABLE,
                                             f"{provider} {track.title} {field}")
                            self.assertTrue(cell.reason, "unavailable needs a reason")
                        else:
                            self.assertTrue(cell.known, f"{provider} {track.title} {field}")

    def test_a_platform_value_is_never_an_empty_placeholder(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                for track in S.playlist(provider).tracks:
                    for field in TRACK_FIELDS:
                        cell = getattr(track, field)
                        if cell.source == SRC_PLATFORM:
                            self.assertIsNotNone(cell.value)
                            self.assertNotEqual(cell.value, "")
                            self.assertIsNone(cell.method,
                                              "a platform value has no method")

    def test_every_ok_track_has_a_title_and_an_id(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                for track in S.playlist(provider).tracks_ok():
                    self.assertTrue(track.title)
                    self.assertTrue(track.id, track.title)
                    self.assertTrue(track.artist_names or track.artists)

    def test_provider_extras_are_kept_out_of_the_canonical_fields(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                for track in S.playlist(provider).tracks:
                    self.assertEqual(set(track.extra) - set(S.EXPECTED[provider]["extra_keys"]),
                                     set())
                    self.assertEqual(set(track.extra) & set(TRACK_FIELDS), set(),
                                     "an extra key shadows a canonical field")
                    for cell in track.extra.values():
                        self.assertTrue(cell.known)

    def test_provider_specific_extras_are_not_shared_by_accident(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                for key in S.EXPECTED[provider]["extra_keys"]:
                    seen = [t for t in S.playlist(provider).tracks if key in t.extra]
                    self.assertTrue(seen, f"{provider} declares {key} on no track")
                    self.assertTrue(all(t.extra[key].known for t in seen))
                for other in S.PROVIDERS:
                    if other != provider:
                        self.assertFalse(set(S.EXPECTED[provider]["extra_keys"])
                                         & set(S.EXPECTED[other]["extra_keys"]))

    def test_an_extra_key_is_never_a_canonical_field(self):
        # `Track.extra` exists precisely for what the schema does not model.
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(set(S.EXPECTED[provider]["extra_keys"]) & set(CANON),
                                 set())

    def test_the_extras_match_the_recorded_file_track_by_track(self):
        for provider in S.PROVIDERS:
            stored = {t["index"]: t.get("extra") or {}
                      for t in S.playlist_obj(provider)["tracks"]}
            for track in S.derive(provider).tracks:
                with self.subTest(provider=provider, index=track.index):
                    self.assertEqual({k: cell.to_obj() for k, cell in track.extra.items()},
                                     stored[track.index])


class TestPartsAreOnlyWhatDeriveNeeds(unittest.TestCase):
    def test_album_request_bodies_are_recorded_and_derive_needs_them(self):
        """QQ records the album POST bodies (``*_req``) because derive reads them.

        The response of a batched ``musicu.fcg`` POST is keyed ``req_0``/``req_1``;
        only the recorded request body says which album mid each slot belongs to.
        Without it derive cannot attach language/release dates to a single track,
        so those parts are real inputs, not documentation.
        """
        provider = "qq"
        parts = S.raw_store(provider).parts
        req = sorted(name for name in parts if name.endswith("_req"))
        self.assertEqual(req, ["album_detail_000_req", "album_detail_001_req",
                               "album_detail_002_req"])
        recorded = {e["name"] for e in S.manifest(provider)["requests"]}
        for name in req:
            with self.subTest(part=name):
                self.assertIn(name, recorded, "a needed request body was not recorded")
                self.assertIsInstance(parts[name], (dict, list))
                self.assertTrue(parts[name], "an empty request body proves nothing")

        full = S.playlist(provider)
        self.assertEqual(sum(1 for t in full.tracks if t.language.known),
                         len(full.tracks))

        stripped = {k: v for k, v in parts.items() if not k.endswith("_req")}
        degraded = S.adapter(provider).derive(stripped, S.ctx(provider))
        self.assertEqual(len(degraded.tracks), len(full.tracks),
                         "the track list itself comes from the playlist response")
        self.assertEqual(sum(1 for t in degraded.tracks if t.language.known), 0)
        self.assertEqual(sum(1 for t in degraded.tracks if t.released_at.known), 0)
        self.assertNotEqual(rebuilt_text(provider, stripped),
                            recorded_text(provider, "playlist.json"),
                            "dropping the bodies must not still verify as the same data")
        self.assertTrue(any("取到了专辑详情请求但一张都没解析出来" in w
                            for w in degraded.warnings), degraded.warnings)

    def test_kugou_uses_both_pages(self):
        parts = S.raw_store("kugou").parts
        self.assertEqual(sorted(parts), ["playlist_detail", "track_all_01"])
        for name in ("playlist_detail", "track_all_01"):
            with self.subTest(part=name):
                self.assertTrue(parts[name], "a part that is empty means a bad recording")

    def test_qishui_derives_from_the_ssr_page_alone(self):
        parts = S.raw_store("qishui").parts
        self.assertEqual(sorted(parts), ["ssr_page"])
        self.assertEqual(len(S.playlist("qishui").tracks), 65)

    def test_netease_pages_song_details_in_two_batches(self):
        parts = S.raw_store("netease").parts
        self.assertEqual(sorted(parts),
                         ["playlist_detail_v3", "song_detail_000", "song_detail_001"])
        self.assertEqual(len(parts["song_detail_000"]["songs"]), 100)
        self.assertEqual(len(parts["song_detail_001"]["songs"]), 100)


class TestExtrasAreHonest(unittest.TestCase):
    """``extra`` 两处（曲目级 / 歌单级）都要与 ``capabilities`` 对得上。

    这一档防的是两种错误，方向相反、后果一样：
    * **吹牛**：``capabilities`` 声明了某个 extra，fixture 里却一个都没有
      （调用方会以为「这个平台有」，于是把缺失当成数据缺陷）；
    * **漏报**：fixture 里明明有某个 extra，``capabilities`` 没说
      （调用方不知道它可能存在，等于这份数据白拿了）。

    判据全部来自磁盘上的 ``raw/`` 与 ``playlist.json``，不联网。
    """

    def test_playlist_level_extras_match_their_capabilities(self):
        for provider in S.PROVIDERS:
            playlist = S.playlist(provider)
            declared = {k.split(".", 1)[1] for k in playlist.capabilities
                        if k.startswith("playlist.")}
            present = set(playlist.playlist.extra)
            expected = set(S.EXPECTED[provider].get("playlist_extra_keys") or ())
            with self.subTest(provider=provider):
                self.assertEqual(declared, expected,
                                 "capabilities 里的 playlist.* 应与 EXPECTED 一致")
                self.assertLessEqual(present, declared,
                                     f"{provider}: 出现了没声明的歌单级 extra")
                if declared:
                    self.assertEqual(present, declared,
                                     f"{provider}: 声明了歌单级 extra 却没落到数据里")

    def test_track_level_extras_match_their_capabilities(self):
        for provider in S.PROVIDERS:
            playlist = S.playlist(provider)
            declared = {k for k in playlist.capabilities
                        if k not in CANON and not k.startswith("playlist.")}
            present = {k for t in playlist.tracks for k in t.extra}
            expected = set(S.EXPECTED[provider].get("extra_keys") or ())
            with self.subTest(provider=provider):
                self.assertEqual(declared, expected)
                self.assertLessEqual(present, declared,
                                     f"{provider}: 出现了没声明的曲目级 extra")
                self.assertEqual(present, declared,
                                 f"{provider}: 声明了曲目级 extra 却没落到数据里")

    def test_every_extra_value_carries_a_source_and_stays_verbatim(self):
        """extra 的值必须带 source；``raw`` 只在「值被格式化过」时出现。"""
        for provider in S.PROVIDERS:
            playlist = S.playlist(provider)
            for name, field in playlist.playlist.extra.items():
                with self.subTest(provider=provider, key=f"playlist.{name}"):
                    self.assertEqual(field.source, "platform")
                    self.assertIsNotNone(field.value)
            for track in playlist.tracks:
                for name, field in track.extra.items():
                    with self.subTest(provider=provider, key=name):
                        self.assertEqual(field.source, "platform")
                        self.assertIsNotNone(field.value)
                        if field.raw is not None:
                            self.assertNotEqual(field.raw, field.value)

    def test_the_new_fields_have_the_measured_coverage(self):
        """每个新字段的覆盖率都钉在这儿：raw/ 一变，这一条就会红。"""
        cases = {
            ("netease", "alias"): 39, ("netease", "trans_name"): 17,
            ("qq", "album_type"): 76, ("qq", "label"): 75,
            ("kugou", "fileid"): 50, ("kugou", "remark"): 50, ("kugou", "bpm"): 50,
            ("qishui", "genre"): 65, ("qishui", "subgenre"): 56,
            ("qishui", "composers"): 63, ("qishui", "lyricists"): 63,
        }
        for (provider, key), want in sorted(cases.items()):
            playlist = S.playlist(provider)
            got = sum(1 for t in playlist.tracks if key in t.extra)
            with self.subTest(provider=provider, key=key):
                self.assertEqual(got, want)

    def test_playlist_level_timestamps_keep_the_platform_epoch_in_raw(self):
        cases = {("kugou", "created_at"): 1769699660,
                 ("kugou", "updated_at"): 1770985184,
                 ("qq", "created_at"): 1559007935,
                 ("qishui", "created_at"): 1722571373,
                 ("netease", "created_at"): 1378721406014}
        for (provider, key), epoch in sorted(cases.items()):
            field = S.playlist(provider).playlist.extra[key]
            with self.subTest(provider=provider, key=key):
                self.assertEqual(field.raw, epoch)
                self.assertRegex(field.value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_kuwo_declares_no_extras_because_nothing_was_ever_recorded(self):
        adapter = adapter_by_name("kuwo")
        self.assertEqual([k for k in adapter.capabilities if k not in CANON], [],
                         "酷我没有 fixture：不许声明一个从没验证过的 extra")


class TestAdapterMetadata(unittest.TestCase):
    def test_every_adapter_is_registered_under_a_unique_name(self):
        adapters = all_adapters()
        names = [a.name for a in adapters]
        self.assertEqual(names, ["netease", "qq", "kuwo", "kugou", "qishui"])
        self.assertEqual(len(set(names)), len(names))
        for adapter in adapters:
            with self.subTest(provider=adapter.name):
                # 只要求「是个版本号」：各平台独立演化，不该钉死在包常量上
                # （与 ADAPTER_VERSION 相等这件事由 test_each_adapter_declares_its_own_version
                # 与 fixture 那条一起守）
                self.assertRegex(adapter.version, r"^\d+\.\d+\.\d+$")
                self.assertTrue(adapter.label)
                self.assertTrue(adapter.url_patterns)
                self.assertTrue(adapter.reference)
                self.assertIs(adapter_by_name(adapter.name).__class__, adapter.__class__)

    def test_each_adapter_declares_its_own_version(self):
        """版本号挂在适配器上，不挂在包上：改一个平台不该连累其它平台的 fixture。

        `Adapter.version` 只是默认值；五个适配器都必须显式声明，否则「独立演化」就只是
        一句口号——有人删掉自己那行，测试要能发现。
        """
        for adapter in all_adapters():
            with self.subTest(adapter=adapter.name):
                self.assertIn("version", vars(type(adapter)),
                              f"{adapter.name} 没有显式声明 version")
                self.assertEqual(adapter.version, adapter.version.strip())
                self.assertTrue(adapter.version)

    def test_the_recorded_adapter_version_is_the_current_one(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(S.EXPECTED[provider]["adapter_version"],
                                 S.adapter(provider).version)

    def test_digits_are_only_ambiguous_where_they_really_are(self):
        for name in ("netease", "qq", "kuwo", "qishui"):
            with self.subTest(provider=name):
                self.assertTrue(adapter_by_name(name).digits_are_ambiguous)
        self.assertFalse(adapter_by_name("kugou").digits_are_ambiguous,
                         "kugou ids carry a collection_ prefix")

    def test_kuwo_is_verified_live_but_still_has_no_fixture(self):
        """2026-10-03 起酷我有实测记录，但**仍然没有 fixture**——所以只有「跑通过」，
        没有「离线可复算的证据」。两件事必须分开说，别拿前者冒充后者。

        另外：那条实测用的是用户提供的私密歌单，所以 `verified_note` 里明写
        「id 不记录」，不允许把歌单 id 写进仓库。
        """
        adapter = adapter_by_name("kuwo")
        self.assertEqual(adapter.verified_on, "2026-10-03")
        self.assertIn("id 不记录", adapter.verified_note)
        self.assertNotIn("kuwo", S.PROVIDERS, "酷我依然没有 fixture：不许假装有离线证据")

    def test_the_verified_fixtures_name_the_day_they_were_verified(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                adapter = S.adapter(provider)
                self.assertEqual(adapter.verified_on, "2026-09-29")
                self.assertIn(f"声明 {S.EXPECTED[provider]['declared']}",
                              adapter.verified_note)

    def test_node_services_are_declared_as_such(self):
        self.assertTrue(adapter_by_name("kugou").requires_node)
        self.assertFalse(adapter_by_name("qishui").requires_node,
                         "qishui SSR works without the node service")
        for name in ("netease", "qq", "kuwo"):
            with self.subTest(provider=name):
                self.assertFalse(adapter_by_name(name).requires_node)

    def test_every_adapter_covers_the_canonical_fields(self):
        for adapter in all_adapters():
            with self.subTest(provider=adapter.name):
                self.assertLessEqual(set(CANON), set(adapter.capabilities),
                                     "every canonical field must be answered for")
                self.assertTrue(all(isinstance(v, bool)
                                    for v in adapter.capabilities.values()))
                # A provider may offer more than the canonical four (netease
                # records popularity, 汽水 genre/subgenre); those land in
                # Track.extra, not the schema.  EXPECTED is the single source of
                # truth for which extras each provider is allowed to declare.
                expected = S.EXPECTED.get(adapter.name) or {}
                extra_fields = set(expected.get("extra_keys") or ())
                # 歌单级 extra 带 `playlist.` 前缀，作用域不同，分开记账。
                playlist_fields = {"playlist." + k
                                   for k in expected.get("playlist_extra_keys") or ()}
                self.assertLessEqual(set(adapter.capabilities) - set(CANON),
                                     extra_fields | playlist_fields)
                for key in adapter.capabilities:
                    if key.startswith("playlist."):
                        self.assertIn(key.split(".", 1)[1],
                                      expected.get("playlist_extra_keys") or ())


class TestQishuiMetadata(unittest.TestCase):
    """The 汽水 SSR payload keeps its identity somewhere other than 网易云/QQ.

    ``playlistInfo`` has no ``description`` (it is ``desc``) and the author is
    ``owner{nickname,id}``.  Both were read from the wrong key once, which
    silently produced an empty creator and an empty intro instead of an error -
    so the recorded file is asserted field by field, and the fallback chain is
    asserted without any network.
    """

    def setUp(self):
        from mpa.adapters.qishui import QishuiAdapter
        self.adapter = QishuiAdapter()
        self.expected = S.EXPECTED["qishui"]

    def test_the_recorded_playlist_carries_the_creator_and_the_intro(self):
        meta = S.playlist("qishui").playlist
        self.assertEqual(meta.creator_name, self.expected["creator_name"])
        self.assertEqual(meta.creator_id, self.expected["creator_id"])
        self.assertEqual(meta.description, self.expected["description"])
        self.assertEqual(meta.declared_count, self.expected["declared"])

    def test_the_live_derive_produces_the_same_metadata(self):
        meta = S.derive("qishui").playlist
        self.assertEqual(meta.creator_name, "汽水音乐APP")
        self.assertEqual(meta.creator_id, "2331429247649368")
        self.assertEqual(meta.description, "在你给的爱里沉溺💗")

    def test_creator_falls_back_from_nickname_to_name_and_uid(self):
        self.assertEqual(self.adapter._creator({"owner": {"nickname": "A", "id": 7}}),
                         ("A", "7"))
        self.assertEqual(self.adapter._creator({"owner": {"name": "B", "uid": "u1"}}),
                         ("B", "u1"))
        self.assertEqual(
            self.adapter._creator({"user_artist_info": {"user_brief": {"nickname": "C"}}}),
            ("C", None))

    def test_creator_is_none_rather_than_guessed(self):
        for info in ({}, {"owner": None}, {"owner": "not-a-dict"},
                     {"owner": {}}, {"user_artist_info": {"user_brief": {}}}):
            with self.subTest(info=info):
                self.assertEqual(self.adapter._creator(info), (None, None))

    def test_description_prefers_desc_over_the_netease_spelling(self):
        self.assertEqual(self.adapter._description({"desc": "d", "description": "x"}), "d")
        self.assertEqual(self.adapter._description({"description": "x"}), "x")
        for info in ({}, {"desc": ""}, {"desc": None, "description": ""}):
            with self.subTest(info=info):
                self.assertIsNone(self.adapter._description(info))

    def test_parse_ssr_reads_playlist_info_and_medias(self):
        from mpa.adapters.qishui import parse_ssr
        html = ("<html><script>window._ROUTER_DATA = "
                '{"loaderData": {"playlist_page": {"playlistInfo": {"id": "9"},'
                ' "medias": [{"entity": {"track": {"name": "t"}}}]}}};</script>')
        info, medias = parse_ssr(html)
        self.assertEqual(info, {"id": "9"})
        self.assertEqual(medias, [{"entity": {"track": {"name": "t"}}}])

    def test_the_router_data_marker_wins_over_its_own_substring(self):
        """``OUTER_DATA`` only ever matched because it is inside ``_ROUTER_DATA``."""
        from mpa.adapters.qishui import _find_marker
        html = 'x = {"a": 1} // _ROUTER_DATA mentions OUTER_DATA'
        self.assertEqual(_find_marker(html), html.find("_ROUTER_DATA"))
        self.assertEqual(_find_marker("no marker here"), -1)
        self.assertEqual(_find_marker(""), -1)

    def test_an_unrecognised_page_is_an_upstream_change_not_an_empty_playlist(self):
        from mpa.errors import UpstreamChanged
        parts = dict(S.raw_store("qishui").parts)
        parts["ssr_page"] = "<html>nothing to see</html>"
        with self.assertRaises(UpstreamChanged) as ctx:
            self.adapter.derive(parts, S.ctx("qishui"))
        self.assertIn("_ROUTER_DATA", ctx.exception.message)
        self.assertIn("OUTER_DATA", ctx.exception.message)


class TestQishuiPartialRouteFallback(unittest.TestCase):
    """``partial_ok`` is the only thing that softens the truncated-SSR route.

    The share page silently stops at ~175 medias, so when ``count_tracks`` is
    bigger the adapter must reach for the local ``qishui-api`` service.  If that
    service cannot be started the default is to *refuse the route* (exit 5)
    rather than hand back a short list and call it done - that silent truncation
    was the original script's worst failure.  ``--partial-ok`` is the opt-out:
    keep the SSR slice, say why in ``ctx.extra``, and let the ledger gap turn the
    exit code into 1.  None of that needs a network or a node service.
    """

    KEY = "7397692920558452788"
    SHARE = f"https://music.douyin.com/qishui/share/playlist?playlist_id={KEY}"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.home = Path(self.tmpdir.name) / "home"
        self.recorded = S.raw_store("qishui").parts["ssr_page"]
        self.assertEqual(self.recorded.count('"count_tracks":65'), 1,
                         "the fixture's declared total moved; fix this test")
        self.long_page = re.sub(r'"count_tracks"\s*:\s*65', '"count_tracks":200',
                                self.recorded, count=1)

    def adapter(self, **kwargs):
        from mpa.adapters.qishui import QishuiAdapter
        kwargs.setdefault("service_home", self.home)
        kwargs.setdefault("allow_provision", False)
        return QishuiAdapter(**kwargs)

    def ctx(self):
        from mpa.adapters.base import Ctx, Ref
        return Ctx(input=self.SHARE, ref=Ref(provider="qishui", key=self.KEY),
                   mode="ssr-partial")

    def fake_client(self, html: str):
        """Only ``get_text`` may be used; anything else is a test failure."""
        client = mock.Mock()
        client.get_text.return_value = html
        return client

    def test_a_long_playlist_without_the_service_refuses_the_route(self):
        from mpa.errors import (EXIT_ROUTE_UNAVAILABLE, RouteUnavailable,
                               exit_code_for)
        client = self.fake_client(self.long_page)
        with self.assertRaises(RouteUnavailable) as caught:
            self.adapter(partial_ok=False).collect(client, self.ctx().ref, self.ctx())
        exc = caught.exception
        self.assertEqual(exc.kind, "route_unavailable")
        self.assertEqual(exit_code_for(exc), EXIT_ROUTE_UNAVAILABLE)
        self.assertIn("声明 200 首", exc.message)
        self.assertIn("SSR 只提供 65 首", exc.message)
        self.assertIn("qishui-api", exc.message)
        client.get_json.assert_not_called()

    def test_partial_ok_keeps_the_ssr_slice_and_records_the_reason(self):
        client = self.fake_client(self.long_page)
        ctx = self.ctx()
        parts = self.adapter(partial_ok=True).collect(client, ctx.ref, ctx)
        self.assertEqual(set(parts), {"ssr_page"})
        self.assertEqual(parts["ssr_page"], self.long_page)
        self.assertEqual(ctx.mode, "ssr-partial")
        self.assertNotIn("service", ctx.extra)
        self.assertTrue(ctx.extra.get("service_error"), ctx.extra)
        self.assertIn("node service disabled", ctx.extra["service_error"])
        client.get_json.assert_not_called()

    def test_the_partial_result_reports_the_gap_and_exits_one(self):
        from mpa.errors import EXIT_PARTIAL
        from mpa.pipeline import _exit_for
        client = self.fake_client(self.long_page)
        ctx = self.ctx()
        adapter = self.adapter(partial_ok=True)
        playlist = adapter.derive(adapter.collect(client, ctx.ref, ctx), ctx)
        self.assertEqual(playlist.ledger.to_obj(),
                         {"declared": 200, "fetched": 65, "ok": 65,
                          "unavailable": 0, "gap": 135})
        self.assertEqual(_exit_for(playlist), EXIT_PARTIAL)
        self.assertTrue(any("200" in w and "65" in w for w in playlist.warnings),
                        playlist.warnings)

    def test_a_playlist_that_fits_in_the_ssr_page_never_starts_the_service(self):
        client = self.fake_client(self.recorded)
        ctx = self.ctx()
        parts = self.adapter(partial_ok=False).collect(client, ctx.ref, ctx)
        self.assertEqual(set(parts), {"ssr_page"})
        self.assertEqual(ctx.extra, {})
        client.get_json.assert_not_called()
        client.get_text.assert_called_once()
        self.assertEqual(client.get_text.call_args.args[0], "ssr_page")

    def test_an_unknown_total_does_not_start_the_service_either(self):
        page = re.sub(r',\s*"count_tracks"\s*:\s*65', "", self.recorded, count=1)
        self.assertNotIn("count_tracks", page)
        client = self.fake_client(page)
        ctx = self.ctx()
        parts = self.adapter(partial_ok=False).collect(client, ctx.ref, ctx)
        self.assertEqual(set(parts), {"ssr_page"})
        self.assertEqual(ctx.extra, {})
        client.get_json.assert_not_called()


def genre_tag(first=None, second=None, category="Genre", first_id=1, second_id=2):
    tag = {"category": {"tag_name": category}}
    if first is not None:
        tag["first_level_tag"] = {"tag_name": first, "tag_id": first_id}
    if second is not None:
        tag["second_level_tag"] = {"tag_name": second, "tag_id": second_id}
    return tag


class TestQishuiGenres(unittest.TestCase):
    """``track.tags`` carries the platform's two-level Genre labels.

    They land in ``Track.extra`` as ``genre`` (first level) and ``subgenre``
    (second level, absent when the platform gives none).  No mapping, no
    bucketing: the label text and the tag id are all that is kept.
    """

    def setUp(self):
        from mpa.adapters.qishui import QishuiAdapter
        self.adapter = QishuiAdapter()

    def test_both_levels_become_two_keys(self):
        got = self.adapter._genres(
            {"tags": [genre_tag("Pop", "Chinese Pop", first_id=11, second_id=22)]})
        self.assertEqual(sorted(got), ["genre", "subgenre"])
        self.assertEqual(got["genre"].value, "Pop")
        self.assertEqual(got["genre"].source, SRC_PLATFORM)
        self.assertEqual(got["genre"].raw, {"tag_id": "11"})
        self.assertEqual(got["subgenre"].value, "Chinese Pop")
        self.assertEqual(got["subgenre"].raw, {"tag_id": "22"})

    def test_a_coarse_tag_produces_only_the_first_level(self):
        got = self.adapter._genres({"tags": [genre_tag("Hip Hop/Rap")]})
        self.assertEqual(list(got), ["genre"])
        self.assertEqual(got["genre"].value, "Hip Hop/Rap")

    def test_a_missing_tag_id_leaves_raw_out(self):
        got = self.adapter._genres(
            {"tags": [{"category": {"tag_name": "Genre"},
                       "first_level_tag": {"tag_name": "Pop"}}]})
        self.assertEqual(got["genre"].value, "Pop")
        self.assertIsNone(got["genre"].raw)

    def test_a_tag_without_a_name_is_not_a_value(self):
        for tag in (genre_tag(None, None), genre_tag("", "Chinese Pop")):
            with self.subTest(tag=tag):
                self.assertNotIn("genre", self.adapter._genres({"tags": [tag]}))

    def test_a_whitespace_only_tag_name_is_dropped(self):
        # Same contract as `_language`, which drops blank codes: strip first,
        # then decide, and never put whitespace into a platform value.
        got = self.adapter._genres({"tags": [{"category": {"tag_name": "Genre"},
                                              "first_level_tag": {"tag_name": "  "},
                                              "second_level_tag": {"tag_name": "\t"},
                                              "tag_id": 7}]})
        self.assertEqual(got, {})

    def test_a_padded_label_still_passes_through(self):
        got = self.adapter._genres({"tags": [
            {"category": {"tag_name": " Genre "},
             "first_level_tag": {"tag_name": " Pop ", "tag_id": 11},
             "second_level_tag": {"tag_name": "   "}}]})
        self.assertEqual(got["genre"].value, "Pop")
        self.assertEqual(got["genre"].source, SRC_PLATFORM)
        self.assertEqual(got["genre"].raw, {"tag_id": "11"})
        self.assertNotIn("subgenre", got)

    def test_labels_of_another_category_are_ignored(self):
        for category in ("Mood", "Language", "Scene", ""):
            with self.subTest(category=category):
                got = self.adapter._genres(
                    {"tags": [genre_tag("Sad", "Melancholic", category=category)]})
                self.assertEqual(got, {})

    def test_the_category_match_is_case_insensitive_and_trimmed(self):
        got = self.adapter._genres(
            {"tags": [genre_tag("Pop", "Chinese Pop", category=" genre ")]})
        self.assertEqual(sorted(got), ["genre", "subgenre"])

    def test_the_first_label_of_each_level_wins(self):
        got = self.adapter._genres({"tags": [
            genre_tag("Pop", None, first_id=1),
            genre_tag("Rock", "Chinese Rock", first_id=9, second_id=8),
        ]})
        self.assertEqual(got["genre"].value, "Pop")
        self.assertEqual(got["subgenre"].value, "Chinese Rock")

    def test_a_second_tag_can_supply_the_missing_level(self):
        got = self.adapter._genres({"tags": [
            genre_tag(None, "Chinese Pop", second_id=5),
            genre_tag("Pop", "Latin Pop", first_id=2, second_id=6),
        ]})
        self.assertEqual(got["genre"].value, "Pop")
        self.assertEqual(got["subgenre"].value, "Chinese Pop")

    def test_tags_missing_or_malformed_give_no_extras(self):
        for track in ({}, {"tags": None}, {"tags": []}, {"tags": [None, "x", 3]},
                      {"tags": [{"category": None}]},
                      {"tags": [{"category": {}, "first_level_tag": {"tag_name": "Pop"}}]},
                      {"tags": [{"category": {"tag_name": "Genre"},
                                 "first_level_tag": {"tag_name": None}}]}):
            with self.subTest(track=track):
                self.assertEqual(self.adapter._genres(track), {})

    def test_a_non_dict_tag_part_is_an_upstream_change_not_an_attribute_error(self):
        """Regression: ``.get`` on a string raised ``AttributeError`` out of derive.

        `mpa verify` only catches ``MPError``, so the traceback escaped. A shape
        that no longer matches is exactly what ``UpstreamChanged`` (exit 3) is
        for - guessing a genre from a string would be worse than saying so.
        """
        cases = ({"category": "Genre", "first_level_tag": {"tag_name": "Pop"}},
                 {"category": {"tag_name": "Genre"}, "first_level_tag": "Pop"},
                 {"category": {"tag_name": "Genre"},
                  "first_level_tag": {"tag_name": "Pop"}, "second_level_tag": 3})
        for tag in cases:
            with self.subTest(tag=tag):
                with self.assertRaises(UpstreamChanged) as ctx:
                    self.adapter._genres({"tags": [tag]})
                self.assertEqual(ctx.exception.kind, KIND_UPSTREAM_CHANGED)
                self.assertEqual(exit_code_for(ctx.exception), 3)

    def test_the_category_key_missing_entirely_is_still_silent(self):
        """``tag.grades``-style shapes that simply have no ``category`` are fine."""
        got = self.adapter._genres({"tags": [{"first_level_tag": {"tag_name": "Pop"}}]})
        self.assertEqual(got, {})

    def test_the_track_builder_passes_the_genres_through(self):
        track = self.adapter._track(4, {"id": "1", "name": "t",
                                        "artists": [{"name": "a"}],
                                        "tags": [genre_tag("Pop", "Chinese Pop")]})
        self.assertEqual({k: v.value for k, v in track.extra.items()},
                         {"genre": "Pop", "subgenre": "Chinese Pop"})

    def test_the_recorded_run_keeps_the_live_genre_distribution(self):
        from collections import Counter
        tracks = S.playlist("qishui").tracks
        genres = Counter(t.extra["genre"].value for t in tracks)
        self.assertEqual(len(genres), 5)
        self.assertEqual(genres["Pop"], 49)
        self.assertEqual(sum(genres.values()), 65)
        self.assertEqual(sum(1 for t in tracks if "subgenre" in t.extra), 56)
        self.assertTrue(all(t.extra["genre"].source == SRC_PLATFORM for t in tracks))


_ABSENT = object()


class TestQishuiLanguage(unittest.TestCase):
    """``media.entity.track.lang_codes`` is a platform field, passed through as-is.

    The adapter deliberately does *not* map ``ZH`` -> ``国语``: the value keeps the
    platform's shape and the original array stays in ``raw``.  The field used to
    be hard-coded unavailable, which silently dropped data the page did carry.
    """

    def setUp(self):
        from mpa.adapters.qishui import QishuiAdapter
        self.adapter = QishuiAdapter()

    def field(self, codes):
        return self.adapter._language({"lang_codes": codes} if codes is not _ABSENT
                                      else {})

    def test_a_single_code_is_passed_through(self):
        got = self.field(["ZH"])
        self.assertEqual(got.value, "ZH")
        self.assertEqual(got.source, SRC_PLATFORM)
        self.assertTrue(got.known)
        self.assertEqual(got.raw, {"lang_codes": ["ZH"]})

    def test_several_codes_are_joined_with_a_plus(self):
        for codes in (["ZH", "EN"], ("ZH", "EN")):
            with self.subTest(codes=codes):
                got = self.field(list(codes))
                self.assertEqual(got.value, "ZH+EN")
                self.assertEqual(got.raw, {"lang_codes": list(codes)})

    def test_the_code_order_is_the_platform_order(self):
        self.assertEqual(self.field(["EN", "ZH"]).value, "EN+ZH")

    def test_a_bare_string_is_treated_as_one_code(self):
        got = self.field("ZH")
        self.assertEqual(got.value, "ZH")
        self.assertEqual(got.raw, {"lang_codes": ["ZH"]})

    def test_whitespace_is_trimmed_from_the_value_only(self):
        got = self.field([" ZH ", " EN "])
        self.assertEqual(got.value, "ZH+EN")
        self.assertEqual(got.raw, {"lang_codes": [" ZH ", " EN "]})

    def test_missing_or_empty_codes_are_unavailable_with_a_reason(self):
        for codes in (_ABSENT, None, [], [""], ["   "], "  "):
            with self.subTest(codes=codes):
                got = self.field(codes)
                self.assertEqual(got.source, SRC_UNAVAILABLE)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

    def test_the_track_builder_uses_the_same_helper(self):
        track = self.adapter._track(3, {"id": "1", "name": "t",
                                        "artists": [{"name": "a"}],
                                        "lang_codes": ["ZH"]})
        self.assertEqual((track.index, track.language.value, track.language.source),
                         (3, "ZH", SRC_PLATFORM))

    def test_the_recorded_run_keeps_the_live_language_distribution(self):
        from collections import Counter
        tracks = S.playlist("qishui").tracks
        self.assertEqual(Counter(t.language.value for t in tracks),
                         Counter({"ZH": 64, "EN": 1}))
        for t in tracks:
            with self.subTest(index=t.index):
                self.assertTrue(t.language.known)
                self.assertEqual(t.language.source, SRC_PLATFORM)
                self.assertEqual(t.language.raw, {"lang_codes": [t.language.value]})

    def test_the_language_is_not_a_guess_and_needs_no_title(self):
        # A title with no letters at all still gets its platform code.
        track = self.adapter._track(0, {"id": "1", "title": "", "name": "🎵",
                                        "artists": [{"name": "a"}],
                                        "lang_codes": ["EN"]})
        self.assertEqual(track.language.value, "EN")


if __name__ == "__main__":
    unittest.main()
