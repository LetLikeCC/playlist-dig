"""mpa/model.py - the mpa/playlist@1 contract.

The rule this file protects: a value is either from the platform, derived by us
(with a named method), or explicitly unavailable with a reason.  Never a guess,
never an empty string standing in for "we don't know".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.model import (ADAPTER_VERSION, SCHEMA_PLAYLIST, SRC_DERIVED,
                       SRC_PLATFORM, SRC_UNAVAILABLE, TRACK_FIELDS, TRACK_OK,
                       TRACK_UNAVAILABLE, Album, Artist, Field, Ledger, Playlist,
                       PlaylistMeta, SourceInfo, Track)


class TestSchemaConstants(unittest.TestCase):
    def test_schema_ids_are_versioned(self):
        self.assertEqual(SCHEMA_PLAYLIST, "mpa/playlist@1")
        self.assertEqual(ADAPTER_VERSION, "2.0.0")

    def test_track_fields_are_the_four_canonical_ones(self):
        self.assertEqual(TRACK_FIELDS,
                         ("duration_ms", "language", "released_at", "collected_at"))


class TestFieldConstructors(unittest.TestCase):
    def test_platform_value_is_known_and_carries_source(self):
        field = Field.platform(295940)
        self.assertEqual(field.value, 295940)
        self.assertEqual(field.source, SRC_PLATFORM)
        self.assertTrue(field.known)
        self.assertIsNone(field.reason)

    def test_platform_zero_is_a_real_value_not_a_missing_one(self):
        # 0 duration is wrong data, but it is *data*: it must not be laundered
        # into "unavailable" by a falsy check.
        field = Field.platform(0)
        self.assertEqual(field.value, 0)
        self.assertTrue(field.known)

    def test_platform_none_becomes_unavailable_with_a_reason(self):
        field = Field.platform(None)
        self.assertIsNone(field.value)
        self.assertEqual(field.source, SRC_UNAVAILABLE)
        self.assertEqual(field.reason, "provider returned no value")
        self.assertFalse(field.known)

    def test_platform_empty_string_is_treated_as_missing(self):
        self.assertEqual(Field.platform("").source, SRC_UNAVAILABLE)
        self.assertEqual(Field.platform("", reason="该曲目没有 language 字段").reason,
                         "该曲目没有 language 字段")

    def test_platform_keeps_raw_only_when_it_differs_from_the_value(self):
        # raw is for the unit conversion that was applied; storing a duplicate
        # of `value` would just double the file size.
        self.assertNotIn("raw", Field.platform(156, raw=156).to_obj())
        self.assertEqual(Field.platform(156, raw={"sec": 156}).to_obj()["raw"],
                         {"sec": 156})
        self.assertEqual(Field.platform("国语", raw={"granularity": "album"}).to_obj()["raw"],
                         {"granularity": "album"})

    def test_derived_requires_a_method(self):
        with self.assertRaises(TypeError):
            Field.derived("国语")  # method is keyword-only and mandatory
        field = Field.derived("国语", method="title_char_script", confidence="medium")
        self.assertEqual(field.source, SRC_DERIVED)
        self.assertEqual(field.method, "title_char_script")
        self.assertEqual(field.confidence, "medium")
        self.assertTrue(field.known)

    def test_derived_defaults_to_low_confidence(self):
        self.assertEqual(Field.derived(4, method="count_decades").confidence, "low")

    def test_derived_from_nothing_is_unavailable_not_zero(self):
        for empty in (None, ""):
            field = Field.derived(empty, method="whatever")
            self.assertEqual(field.source, SRC_UNAVAILABLE)
            self.assertEqual(field.reason, "nothing to derive from")
            self.assertIsNone(field.method)

    def test_unavailable_is_not_known(self):
        self.assertFalse(Field.unavailable("nope").known)

    def test_known_demands_both_a_value_and_a_trusted_source(self):
        self.assertFalse(Field(value=1, source="weird").known)
        self.assertFalse(Field(value=None, source=SRC_PLATFORM).known)
        self.assertFalse(Field(value=None, source=SRC_DERIVED).known)

    def test_known_does_not_re_check_emptiness(self):
        # Documented sharp edge: `known` tests `value is not None`, so a
        # hand-built (or hand-edited) {"value": "", "source": "platform"} claims
        # to be known. The constructors cannot produce that - Field.platform("")
        # degrades to unavailable - and no recorded fixture contains it, so this
        # is only reachable through from_obj on foreign data.
        self.assertTrue(Field.from_obj({"value": "", "source": SRC_PLATFORM}).known)
        self.assertEqual(Field.platform("").source, SRC_UNAVAILABLE)


class TestFieldSerialisation(unittest.TestCase):
    def test_to_obj_drops_empty_keys(self):
        self.assertEqual(Field.unavailable("x").to_obj(),
                         {"source": SRC_UNAVAILABLE, "reason": "x"})
        self.assertEqual(Field.platform(5).to_obj(), {"value": 5, "source": SRC_PLATFORM})

    def test_from_obj_none_is_absent_not_missing_reasonless(self):
        field = Field.from_obj(None)
        self.assertEqual(field.source, SRC_UNAVAILABLE)
        self.assertEqual(field.reason, "absent from stored data")

    def test_from_obj_accepts_a_bare_scalar(self):
        # Old/foreign data may store a plain number; treat it as platform data
        # rather than losing it.
        field = Field.from_obj(1234)
        self.assertEqual(field.value, 1234)
        self.assertEqual(field.source, SRC_PLATFORM)

    def test_from_obj_round_trips(self):
        for original in (Field.platform(1), Field.platform("x", raw={"a": 1}),
                         Field.derived(2, method="m", confidence="high"),
                         Field.unavailable("why")):
            with self.subTest(original=original):
                self.assertEqual(Field.from_obj(original.to_obj()).to_obj(),
                                 original.to_obj())


class TestArtistAndAlbum(unittest.TestCase):
    def test_artist_drops_an_absent_id(self):
        self.assertEqual(Artist(name="A").to_obj(), {"name": "A"})
        self.assertEqual(Artist(name="A", id="1").to_obj(), {"name": "A", "id": "1"})
        self.assertEqual(Artist.from_obj({"name": "A"}).id, None)
        self.assertEqual(Artist.from_obj({}).name, "")

    def test_album_round_trip_and_defaults(self):
        album = Album(name="是非题", id="175619458", release_date="2026-02-05")
        self.assertEqual(Album.from_obj(album.to_obj()), album)
        self.assertEqual(Album.from_obj(None), Album())
        self.assertEqual(Album().to_obj(), {})


class TestTrack(unittest.TestCase):
    def test_unfetched_fields_are_unavailable_not_zero(self):
        track = Track(index=0)
        for name in TRACK_FIELDS:
            field = getattr(track, name)
            self.assertEqual(field.source, SRC_UNAVAILABLE)
            self.assertEqual(field.reason, "not fetched")
            self.assertFalse(field.known)

    def test_defaults(self):
        track = Track(index=3)
        self.assertEqual(track.status, TRACK_OK)
        self.assertTrue(track.ok)
        self.assertEqual(track.artists, [])
        self.assertIsNone(track.album)
        self.assertEqual(track.artist_names, [])
        self.assertEqual(track.extra, {})

    def test_ok_is_status_based(self):
        self.assertFalse(Track(index=0, status=TRACK_UNAVAILABLE).ok)
        self.assertTrue(Track(index=0, status=TRACK_OK).ok)

    def test_artist_names_skips_blank_names(self):
        track = Track(index=0, artists=[Artist(name="A"), Artist(name=""), Artist(name="B")])
        self.assertEqual(track.artist_names, ["A", "B"])

    def test_to_obj_omits_empty_scaffolding(self):
        obj = Track(index=1).to_obj()
        self.assertEqual(obj["index"], 1)
        self.assertEqual(obj["status"], TRACK_OK)
        self.assertNotIn("reason", obj)
        self.assertNotIn("id", obj)
        self.assertNotIn("title", obj)
        self.assertNotIn("artists", obj)
        self.assertNotIn("album", obj)
        self.assertNotIn("extra", obj)
        # ...but every canonical field is always present, so consumers never
        # have to guess whether "absent" means "unavailable" or "old schema".
        for name in TRACK_FIELDS:
            self.assertIn(name, obj)

    def test_to_obj_includes_extra_only_when_present(self):
        track = Track(index=0, extra={"popularity": Field.platform(100)})
        self.assertEqual(track.to_obj()["extra"]["popularity"],
                         {"value": 100, "source": SRC_PLATFORM})

    def test_round_trip_with_extra(self):
        track = Track(index=2, title="t", id="i", artists=[Artist(name="A", id="1")],
                      album=Album(name="al"), duration_ms=Field.platform(1000),
                      extra={"fileid": Field.platform("F")})
        self.assertEqual(Track.from_obj(track.to_obj()), track)

    def test_from_obj_tolerates_a_minimal_object(self):
        track = Track.from_obj({})
        self.assertEqual(track.index, 0)
        self.assertEqual(track.status, TRACK_OK)
        self.assertEqual(track.duration_ms.source, SRC_UNAVAILABLE)


class TestLedger(unittest.TestCase):
    def test_gap_is_declared_minus_fetched(self):
        self.assertEqual(Ledger(declared=5, fetched=3, ok=3).gap, 2)
        self.assertEqual(Ledger(declared=5, fetched=5, ok=5).gap, 0)
        self.assertEqual(Ledger(declared=3, fetched=5, ok=5).gap, 0)

    def test_gap_is_none_when_the_provider_declared_nothing(self):
        # "unknown" and "zero" are different answers; None is the honest one.
        self.assertIsNone(Ledger(fetched=3, ok=3).gap)

    def test_complete(self):
        self.assertTrue(Ledger(declared=3, fetched=3, ok=3).complete)
        self.assertFalse(Ledger(declared=3, fetched=2, ok=2).complete)
        self.assertFalse(Ledger(declared=3, fetched=3, ok=2, unavailable=1).complete)
        self.assertFalse(Ledger(fetched=3, ok=3).complete)

    def test_to_obj_key_order_is_part_of_the_contract(self):
        """`mpa verify` 逐字节比对 playlist.json，所以键序也是契约：改它必须重算 fixture。"""
        ledger = Ledger(declared=5, fetched=3, ok=3, unavailable=0, duplicates_removed=1,
                        reasons={"gone": 2})
        self.assertEqual(list(ledger.to_obj()),
                         ["declared", "fetched", "ok", "unavailable",
                          "duplicates_removed", "gap", "reasons"])
        # 默认值被丢掉之后顺序也不许乱：0 不是「默认值」（`fetched`/`ok`/`unavailable`
        # 照写），只有 `duplicates_removed` 与空的 `reasons` 会被 `_clean` 拿掉。
        self.assertEqual(list(Ledger(declared=1, fetched=1, ok=1).to_obj()),
                         ["declared", "fetched", "ok", "unavailable", "gap"])

    def test_to_obj_always_writes_declared_and_gap_even_when_null(self):
        """null 在这里是信息（平台没说总数），不是「这个键不存在」。"""
        obj = Ledger(fetched=2, ok=2).to_obj()
        self.assertIsNone(obj["declared"])
        self.assertIsNone(obj["gap"])
        self.assertIn("declared", obj)

    def test_to_obj_keeps_zero_but_drops_defaults(self):
        self.assertEqual(Ledger(declared=5, fetched=3, ok=3).to_obj(),
                         {"declared": 5, "fetched": 3, "ok": 3, "unavailable": 0, "gap": 2})

    def test_to_obj_always_says_whether_the_total_is_known(self):
        """``declared``/``gap`` are never dropped: null is an answer.

        Omitting the key would make "the platform did not give a total" look the
        same as "this file was written before the field existed" - and the whole
        point of the ledger is that the difference between "0 missing" and
        "cannot be proven complete" is visible in the JSON.
        """
        obj = Ledger(fetched=1, ok=1).to_obj()
        self.assertIsNone(obj["declared"])
        self.assertIsNone(obj["gap"])
        self.assertEqual(list(obj), ["declared", "fetched", "ok", "unavailable", "gap"])
        self.assertNotIn("reasons", obj)
        self.assertNotIn("duplicates_removed", obj)

    def test_to_obj_key_order_is_declared_fetched_ok_unavailable_gap(self):
        """The recorded files (and their diffs) depend on this order."""
        obj = Ledger(declared=10, fetched=8, ok=7, unavailable=1,
                     duplicates_removed=2, reasons={"下架": 1}).to_obj()
        self.assertEqual(list(obj), ["declared", "fetched", "ok", "unavailable",
                                     "duplicates_removed", "gap", "reasons"])

    def test_a_known_declaration_still_serialises_gap(self):
        obj = Ledger(declared=5, fetched=5, ok=5).to_obj()
        self.assertEqual(obj["gap"], 0)
        self.assertIn("gap", obj)

    def test_from_obj_and_round_trip(self):
        ledger = Ledger(declared=10, fetched=8, ok=7, unavailable=1,
                        duplicates_removed=2, reasons={"下架": 1})
        self.assertEqual(Ledger.from_obj(ledger.to_obj()), ledger)
        self.assertEqual(Ledger.from_obj({}), Ledger())


class TestPlaylistMetaAndSourceInfo(unittest.TestCase):
    def test_creator_is_nested_and_omitted_when_empty(self):
        meta = PlaylistMeta(id="1", title="t", creator_name="A", creator_id="9")
        self.assertEqual(meta.to_obj()["creator"], {"name": "A", "id": "9"})
        self.assertEqual(PlaylistMeta(id="1").to_obj(), {"id": "1"})
        self.assertEqual(PlaylistMeta.from_obj(meta.to_obj()), meta)

    def test_source_info_round_trip(self):
        source = SourceInfo(provider="qq", input="x", adapter_version="2.0.0",
                            upstream_commit="abc", fetched_at="2026-09-29T17:41:26Z",
                            mode="direct-http", raw_saved=True, raw_digest="d" * 64,
                            requests=4)
        self.assertEqual(SourceInfo.from_obj(source.to_obj()), source)

    def test_source_info_keeps_raw_saved_false_visible(self):
        # A dataset fetched with --no-raw must say so rather than look normal.
        obj = SourceInfo(provider="p", input="i", raw_saved=False).to_obj()
        self.assertIn("raw_saved", obj)
        self.assertFalse(obj["raw_saved"])
        self.assertNotIn("requests", obj)  # 0 requests is the default, dropped


class TestPlaylist(unittest.TestCase):
    def test_tracks_ok_filters_unavailable_entries(self):
        playlist = Playlist(source=SourceInfo(provider="p", input="i"),
                            tracks=[Track(index=0), Track(index=1, status=TRACK_UNAVAILABLE)])
        self.assertEqual([t.index for t in playlist.tracks_ok()], [0])

    def test_default_schema_and_warnings(self):
        playlist = Playlist(source=SourceInfo(provider="p", input="i"))
        self.assertEqual(playlist.schema, SCHEMA_PLAYLIST)
        self.assertEqual(playlist.warnings, [])
        self.assertEqual(playlist.to_obj()["schema"], SCHEMA_PLAYLIST)

    def test_to_obj_shape(self):
        playlist = Playlist(source=SourceInfo(provider="p", input="i"),
                            playlist=PlaylistMeta(title="t"), warnings=["w"])
        self.assertEqual(sorted(playlist.to_obj()),
                         ["capabilities", "ledger", "playlist", "schema", "source",
                          "tracks", "warnings"])

    def test_round_trip_of_a_recorded_fixture(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                original = S.playlist(provider)
                self.assertEqual(Playlist.from_obj(original.to_obj()), original)

    def test_fixture_schema_is_the_versioned_id(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(S.playlist(provider).schema, SCHEMA_PLAYLIST)


class TestPlaylistMetaExtra(unittest.TestCase):
    """歌单级 extra：与 Track.extra 同一套 Field 语义，但作用域是整张歌单。

    写盘位置固定是 ``playlist.extra``（不新增顶层键），键序在 ``to_obj`` 里由
    dataclass 顺序决定；``extra`` 为空时整个键不出现，这样老数据集读起来不变。
    """

    def test_key_order_is_id_title_creator_description_count_extra(self):
        meta = PlaylistMeta(id="1", title="t", creator_name="n", creator_id="2",
                            description="d", declared_count=3,
                            extra={"tags": Field.platform(["a"])})
        self.assertEqual(list(meta.to_obj()),
                         ["id", "title", "creator", "description", "declared_count",
                          "extra"])

    def test_an_empty_extra_is_left_out_entirely(self):
        obj = PlaylistMeta(id="1", title="t").to_obj()
        self.assertNotIn("extra", obj)
        self.assertEqual(PlaylistMeta.from_obj(obj).extra, {})

    def test_round_trip_keeps_values_sources_and_raw(self):
        meta = PlaylistMeta(
            id="1", title="t",
            extra={"created_at": Field.platform("2026-01-29T15:14:20Z", raw=1769699660),
                   "tags": Field.platform(["流行", "网络"], raw={"tag_ids": [9, 35]})})
        again = PlaylistMeta.from_obj(meta.to_obj())
        self.assertEqual(again, meta)
        obj = meta.to_obj()["extra"]
        self.assertEqual(obj["created_at"]["source"], SRC_PLATFORM)
        self.assertEqual(obj["created_at"]["raw"], 1769699660)
        self.assertEqual(obj["tags"]["value"], ["流行", "网络"])

    def test_an_unavailable_playlist_extra_keeps_its_reason(self):
        """平台没给这个时间时留 reason，而不是塞一个 0 或空串。"""
        meta = PlaylistMeta(id="1", extra={"updated_at":
                                           Field.unavailable("平台没给这个时间")})
        obj = meta.to_obj()["extra"]["updated_at"]
        self.assertEqual(obj, {"source": SRC_UNAVAILABLE, "reason": "平台没给这个时间"})


class TestFixtureFieldProvenance(unittest.TestCase):
    """The recorded data itself must obey the contract we just tested."""

    def test_no_known_field_has_a_null_or_empty_value(self):
        for provider in S.PROVIDERS:
            for track in S.playlist_obj(provider)["tracks"]:
                for name in TRACK_FIELDS:
                    field = track.get(name)
                    with self.subTest(provider=provider, index=track["index"], field=name):
                        if field.get("source") in (SRC_PLATFORM, SRC_DERIVED):
                            self.assertIsNotNone(field.get("value"))
                            self.assertNotEqual(field.get("value"), "")
                        else:
                            self.assertEqual(field.get("source"), SRC_UNAVAILABLE)
                            self.assertTrue(field.get("reason"))

    def test_unavailable_fields_never_carry_a_value(self):
        for provider in S.PROVIDERS:
            for name in S.UNAVAILABLE_FIELDS[provider]:
                counts = S.track_field_sources(provider)[name]
                with self.subTest(provider=provider, field=name):
                    self.assertEqual(sorted(counts), [SRC_UNAVAILABLE])
                    self.assertEqual(counts[SRC_UNAVAILABLE], S.EXPECTED[provider]["fetched"])

    def test_capabilities_match_what_the_data_actually_contains(self):
        # `mpa adapters` promises a capability map; the dataset must not claim a
        # field it never filled, nor hide one it did.
        for provider in S.PROVIDERS:
            caps = S.EXPECTED[provider]["capabilities"]
            sources = S.track_field_sources(provider)
            for name in TRACK_FIELDS:
                with self.subTest(provider=provider, field=name):
                    got = SRC_PLATFORM in sources[name]
                    if caps.get(name):
                        self.assertTrue(got, f"{provider}.{name} promised but never filled")
                    else:
                        self.assertFalse(got, f"{provider}.{name} filled but marked unsupported")

    def test_every_track_has_an_index_and_a_status(self):
        for provider in S.PROVIDERS:
            tracks = S.playlist_obj(provider)["tracks"]
            with self.subTest(provider=provider):
                self.assertEqual([t["index"] for t in tracks], list(range(len(tracks))))
                self.assertTrue(all(t["status"] == TRACK_OK for t in tracks))


if __name__ == "__main__":
    unittest.main()
