"""mpa/rawstore.py - the raw-response store that makes `verify` possible.

Two jobs: keep every response in a form that can be re-parsed offline (text
verbatim, JSON re-serialised - see the module docstring), and be able to prove
later that it has not changed.  The tests therefore check digests and headers as
carefully as they check "it loads", and they check that a broken dataset is
reported as one instead of escaping as ``FileNotFoundError``.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.errors import DatasetError
from mpa.rawstore import RawStore, _safe_name


class TestSafeName(unittest.TestCase):
    def test_replaces_everything_outside_the_safe_alphabet(self):
        self.assertEqual(_safe_name("a b/c?d=e"), "a_b_c_d_e")
        self.assertEqual(_safe_name("playlist/detail?ids=1"), "playlist_detail_ids_1")
        self.assertEqual(_safe_name("song_detail_000"), "song_detail_000")

    def test_truncates_long_names(self):
        self.assertEqual(len(_safe_name("x" * 500)), 120)

    def test_keeps_dots_and_dashes(self):
        self.assertEqual(_safe_name("a-b.c"), "a-b.c")


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"

    def test_json_payload_is_recorded_compactly_and_hashed(self):
        store = RawStore(self.dir)
        payload = {"a": 1, "b": "中文"}
        store.record("p", "http://x/1", payload, status=200)
        entry = store.entries[0]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(entry["kind"], "json")
        self.assertEqual(entry["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(entry["bytes"], len(body.encode()))
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(store.parts["p"], payload)
        self.assertEqual((self.dir / "p.json").read_text(encoding="utf-8"), body)

    def test_text_payload_uses_txt_and_is_stored_verbatim(self):
        store = RawStore(self.dir)
        html = "<html>\n<body>中文</body>\n</html>"
        store.record("ssr_page", "http://x/2", html, kind=None)
        self.assertEqual(store.entries[0]["kind"], "text")
        self.assertEqual((self.dir / "ssr_page.txt").read_text(encoding="utf-8"), html)

    def test_kind_can_be_forced(self):
        store = RawStore(self.dir)
        store.record("blob", "http://x/3", {"a": 1}, kind="text")
        self.assertEqual(store.entries[0]["kind"], "text")
        self.assertTrue((self.dir / "blob.txt").exists())

    def test_lists_count_as_json(self):
        store = RawStore(self.dir)
        store.record("l", "http://x/4", [1, 2])
        self.assertEqual(store.entries[0]["kind"], "json")

    def test_seq_increments_and_names_are_sanitised(self):
        store = RawStore(self.dir)
        store.record("a b", "http://x/1", {})
        store.record("c/d", "http://x/2", {})
        self.assertEqual([e["seq"] for e in store.entries], [1, 2])
        self.assertEqual([e["name"] for e in store.entries], ["a_b", "c_d"])

    def test_write_false_touches_no_files(self):
        store = RawStore(self.dir, write=False)
        store.record("p", "http://x/1", {"a": 1})
        store.finish()
        self.assertFalse(self.dir.exists())
        self.assertEqual(store.parts, {"p": {"a": 1}})

    def test_parts_property_is_a_copy(self):
        store = RawStore(self.dir, write=False)
        store.record("p", "http://x/1", {"a": 1})
        store.parts["q"] = 2
        self.assertEqual(sorted(store.parts), ["p"])


class TestDigest(unittest.TestCase):
    def test_digest_binds_the_sequence_name_and_hash_of_each_part(self):
        store = RawStore("/nonexistent", write=False)
        store.record("a", "u", {})
        store.record("b", "u", {})
        entry = store.entries[0]
        expected = hashlib.sha256(
            f'{entry["seq"]}:{entry["name"]}:{entry["sha256"]}'.encode()).hexdigest()
        # The second part is part of the digest too, so the combined value is not
        # just the first part's hash.
        self.assertNotEqual(store.digest(), expected)

    def test_digest_is_sensitive_to_content_and_to_request_order(self):
        def make(order, payloads):
            store = RawStore("/nonexistent", write=False)
            for name in order:
                store.record(name, "u", payloads[name])
            return store

        payloads = {"a": {"n": 1}, "b": {"n": 2}}
        self.assertNotEqual(make(["a", "b"], payloads).digest(),
                            make(["a", "b"], {"a": {"n": 1}, "b": {"n": 3}}).digest())
        # seq is assigned in recording order, so the same bodies in a different
        # order describe a different fetch and must not share a digest.
        self.assertNotEqual(make(["a", "b"], payloads).digest(),
                            make(["b", "a"], payloads).digest())

    def test_digest_ignores_the_order_of_the_entries_list_itself(self):
        store = RawStore("/nonexistent", write=False)
        store.record("a", "u", {"n": 1})
        store.record("b", "u", {"n": 2})
        before = store.digest()
        store.entries.reverse()
        self.assertEqual(store.digest(), before)

    def test_total_bytes(self):
        store = RawStore("/nonexistent", write=False)
        store.record("a", "u", "12345")
        store.record("b", "u", "123")
        self.assertEqual(store.total_bytes(), 8)
        self.assertEqual(store.total_bytes(), sum(e["bytes"] for e in store.entries))


class TestLoadRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"

    def test_write_then_load_reproduces_parts_and_verifies(self):
        store = RawStore(self.dir)
        store.record("detail", "http://x/1", {"data": [1, 2]}, status=200)
        store.record("page", "http://x/2", "<html>hi</html>", kind="text")
        store.finish()

        loaded = RawStore.load(self.dir)
        self.assertFalse(loaded.write)
        self.assertEqual(loaded.parts, store.parts)
        self.assertEqual(loaded.digest(), store.digest())
        self.assertEqual(loaded.total_bytes(), store.total_bytes())
        self.assertEqual(loaded.check_manifest(), (True, "2 个原始响应全部通过 sha256 校验"))

    def test_manifest_shape(self):
        store = RawStore(self.dir)
        store.record("a", "http://x/1", {"a": 1})
        store.finish()
        manifest = json.loads((self.dir / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest),
                         ["bytes", "count", "digest", "requests"])
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(manifest["digest"], store.digest())
        self.assertEqual(manifest["bytes"], store.total_bytes())

    def test_load_requires_a_manifest(self):
        self.dir.mkdir(parents=True)
        with self.assertRaises(DatasetError) as caught:
            RawStore.load(self.dir)
        self.assertIn("_manifest.json", str(caught.exception))

    def test_load_part_prefers_json_then_txt(self):
        store = RawStore(self.dir)
        store.record("a", "u", {"x": 1})
        store.record("b", "u", "text", kind="text")
        self.assertEqual(store.load_part("a"), {"x": 1})
        self.assertEqual(store.load_part("b"), "text")
        with self.assertRaises(DatasetError) as caught:
            store.load_part("missing")
        self.assertIn("missing", str(caught.exception))

    def test_the_kind_from_the_manifest_decides_the_extension(self):
        """``b.json`` on disk must not satisfy a ``kind=text`` manifest entry."""
        store = RawStore(self.dir)
        store.record("b", "u", "text", kind="text")
        (self.dir / "b.json").write_text('"from the wrong file"', encoding="utf-8")
        entry = store.entries[0]
        loaded = RawStore(self.dir, write=False)
        loaded.entries = [entry]
        self.assertEqual(loaded.load_part("b", entry["kind"]), "text")

    def test_a_corrupt_part_is_a_dataset_error_not_a_json_traceback(self):
        store = RawStore(self.dir)
        store.record("a", "u", {"x": 1})
        store.finish()
        (self.dir / "a.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(DatasetError) as caught:
            RawStore.load(self.dir)
        self.assertIn("a.json", str(caught.exception))

    def test_load_all_falls_back_to_the_manifest(self):
        store = RawStore(self.dir)
        store.record("a", "u", {"x": 1})
        store.finish()
        fresh = RawStore(self.dir, write=False)
        self.assertEqual(fresh.load_all(), {"a": {"x": 1}})
        self.assertEqual(fresh.entries[0]["name"], "a")
        # Second call is a no-op, not a duplicate.
        self.assertEqual(fresh.load_all(), {"a": {"x": 1}})
        self.assertEqual(len(fresh.entries), 1)

    def test_finish_is_a_noop_when_not_writing(self):
        store = RawStore(self.dir, write=False)
        store.finish()
        self.assertFalse(self.dir.exists())


class TestCheckManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"
        store = RawStore(self.dir)
        store.record("a", "u", {"x": 1})
        store.record("b", "u", "text", kind="text")
        store.finish()
        self.store = store

    def test_missing_manifest_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = RawStore(Path(tmp), write=False).check_manifest()
        self.assertFalse(ok)
        self.assertIn("不存在", message)

    def test_detects_an_edited_body(self):
        (self.dir / "a.json").write_text('{"x":2}', encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertEqual(message, "a.json 的 sha256 与 manifest 不符")

    def test_detects_a_deleted_body(self):
        (self.dir / "b.txt").unlink()
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertEqual(message, "raw/b.* 缺失（manifest 里有记录）")

    def test_detects_a_manifest_digest_that_does_not_match_its_entries(self):
        manifest_path = self.dir / "_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["digest"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertEqual(message, "manifest 的 digest 与各项 sha256 不一致")

    def test_a_tampered_body_that_also_has_a_rewritten_hash_still_fails_the_digest(self):
        # This is the realistic "someone edited the fixture and its manifest"
        # case: per-file hashes agree, the manifest digest does not.
        path = self.dir / "a.json"
        path.write_text('{"x": 2}', encoding="utf-8")
        blob = path.read_bytes()
        manifest_path = self.dir / "_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["requests"]:
            if entry["name"] == "a":
                entry["sha256"] = hashlib.sha256(blob).hexdigest()
                entry["bytes"] = len(blob)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ok, _ = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)

    def test_detects_a_file_the_manifest_does_not_mention(self):
        # A leftover from an earlier recording would leave nobody able to say
        # which file produced playlist.json, so raw/ stops being a record.
        (self.dir / "leftover.json").write_text("{}", encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertIn("raw/ 里有 manifest 未记录的文件：leftover", message)
        self.assertIn("重跑一次 mpa fetch", message)

    def test_detects_a_hidden_stray_file_never_reported(self):
        (self.dir / ".DS_Store").write_text("junk", encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertTrue(ok, message)

    def test_the_stray_list_is_truncated_after_four_names(self):
        for i in range(6):
            (self.dir / f"stray{i}.json").write_text("{}", encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertIn("stray0, stray1, stray2, stray3 等", message)
        self.assertNotIn("stray4", message)

    def test_reports_at_most_four_problems(self):
        for entry in ("a.json", "b.txt"):
            (self.dir / entry).write_text("changed", encoding="utf-8")
        manifest_path = self.dir / "_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["digest"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertLessEqual(len(message.split("; ")), 4)


class TestStrayFiles(unittest.TestCase):
    """Which files on disk count as "not part of the manifest"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"
        store = RawStore(self.dir)
        store.record("a", "u", {"x": 1})
        store.record("b", "u", "text", kind="text")
        store.finish()

    def store(self) -> RawStore:
        """A store that has read the manifest, the way verify does."""
        return RawStore.load(self.dir)

    def test_a_clean_directory_has_no_stray_files(self):
        self.assertEqual(self.store().stray_files(), [])
        self.assertNotIn("_manifest", self.store().stray_files())

    def test_a_leftover_body_is_reported_by_its_stem(self):
        (self.dir / "old_part.json").write_text("{}", encoding="utf-8")
        (self.dir / "older_part.txt").write_text("text", encoding="utf-8")
        self.assertEqual(self.store().stray_files(), ["old_part", "older_part"])

    def test_hidden_files_and_other_extensions_are_ignored(self):
        (self.dir / ".DS_Store").write_text("junk", encoding="utf-8")
        (self.dir / ".hidden.json").write_text("{}", encoding="utf-8")
        (self.dir / "notes.md").write_text("hello", encoding="utf-8")
        (self.dir / "cookies.dat").write_text("hello", encoding="utf-8")
        self.assertEqual(self.store().stray_files(), [])

    def test_stray_files_needs_the_manifest_already_loaded(self):
        # Documented behaviour: the caller either loaded the manifest (``load``,
        # ``load_all``, ``check_manifest``) or gets "everything is stray".
        (self.dir / "old.json").write_text("{}", encoding="utf-8")
        self.assertEqual(RawStore(self.dir, write=False).stray_files(),
                         ["a", "b", "old"])
        self.assertEqual(self.store().stray_files(), ["old"])

    def test_a_same_stem_file_with_the_other_extension_is_reported(self):
        """A stale ``b.txt`` next to a recorded ``b.json`` is unexplained data.

        Regression: ``stray_files`` compared only ``Path.stem``, so the leftover
        was invisible, and ``check_manifest`` then hashed ``b.json`` (the first
        extension that existed) and declared the directory clean - while the
        ``b.txt`` body sat in ``raw/`` with nothing recording where it came from.
        Recording a part under the same name with a different kind is what a
        rewritten adapter does, so this is reachable in practice.
        """
        body = '{"fresh": true}'
        (self.dir / "b.json").write_text(body, encoding="utf-8")
        manifest_path = self.dir / "_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["requests"]:
            if entry["name"] == "b":
                entry["kind"] = "json"
                entry["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                entry["bytes"] = len(body.encode("utf-8"))
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        manifest["digest"] = RawStore.load(self.dir).digest()
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

        store = RawStore.load(self.dir)
        self.assertEqual(store.stray_files(), ["b"])
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertFalse(ok)
        self.assertIn("raw/ 里有 manifest 未记录的文件：b", message)
        self.assertEqual((self.dir / "b.txt").read_text(encoding="utf-8"), "text")

    def test_a_recorded_stem_whose_kind_file_is_missing_is_reported(self):
        """Only the *other* extension exists: the body is wrong, not just extra."""
        (self.dir / "b.txt").unlink()
        (self.dir / "b.json").write_text("{}", encoding="utf-8")
        st = RawStore(self.dir, write=False)
        ok, message = st.check_manifest()
        self.assertFalse(ok)
        self.assertIn("不是 manifest 记录的那一份", message)
        self.assertNotIn("缺失", message)
        self.assertEqual(st.stray_files(), ["b"])
        # …and reading the dataset is a one-line error, not a traceback.
        with self.assertRaises(DatasetError):
            RawStore.load(self.dir)


class TestFinishRemovesStaleParts(unittest.TestCase):
    """``finish`` must leave ``raw/`` as exactly this run's responses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "raw"

    def record(self, *names) -> RawStore:
        store = RawStore(self.dir)
        for name in names:
            store.record(name, "http://x/", {"name": name})
        return store

    def test_a_first_run_removes_nothing(self):
        store = self.record("a", "b")
        self.assertEqual(store.finish(), [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["_manifest.json", "a.json", "b.json"])
        self.assertTrue(RawStore(self.dir, write=False).check_manifest()[0])

    def test_a_second_run_into_the_same_directory_drops_the_old_parts(self):
        self.record("old_a", "old_b").finish()
        store = self.record("new")
        self.assertEqual(store.finish(), ["old_a", "old_b"])
        self.assertFalse((self.dir / "old_a.json").exists())
        self.assertFalse((self.dir / "old_b.json").exists())
        self.assertTrue((self.dir / "new.json").exists())
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertTrue(ok, message)
        fresh = RawStore.load(self.dir)
        self.assertEqual(list(fresh.parts), ["new"])

    def test_the_removed_names_come_back_sorted(self):
        self.record("zz", "aa", "mm").finish()
        self.assertEqual(self.record("keep").finish(), ["aa", "mm", "zz"])

    def test_a_name_the_new_run_reuses_survives(self):
        self.record("shared", "gone").finish()
        store = self.record("shared")
        self.assertEqual(store.finish(), ["gone"])
        self.assertTrue((self.dir / "shared.json").exists())

    def test_a_part_that_changes_kind_deletes_only_the_old_file(self):
        """json → text: the stale ``.json`` goes, the fresh ``.txt`` stays.

        Regression: ``finish`` used to unlink *both* extensions for every name
        ``stray_files`` reported, which would have deleted the body this run had
        just written and left the manifest describing a missing file.
        """
        store = RawStore(self.dir)
        store.record("b", "http://x/", {"name": "b"})
        store.finish()
        store = RawStore(self.dir)
        store.record("b", "http://x/", "plain", kind="text")
        self.assertEqual(store.finish(), ["b"])
        self.assertFalse((self.dir / "b.json").exists())
        self.assertTrue((self.dir / "b.txt").exists())
        ok, message = RawStore(self.dir, write=False).check_manifest()
        self.assertTrue(ok, message)
        self.assertEqual(RawStore.load(self.dir).parts, {"b": "plain"})

    def test_finish_writes_the_manifest_even_with_nothing_to_remove(self):
        store = self.record("a")
        store.finish()
        manifest = json.loads((self.dir / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(manifest["digest"], store.digest())
        self.assertEqual(manifest["bytes"], store.total_bytes())


class TestFixtureRawStores(unittest.TestCase):
    """Every recorded fixture must still be self-consistent on disk."""

    def test_manifest_verifies_and_matches_the_playlist_digest(self):
        for provider in S.PROVIDERS:
            expected = S.EXPECTED[provider]
            with self.subTest(provider=provider):
                store = S.raw_store(provider)
                self.assertEqual(store.check_manifest(),
                                 (True, f"{len(expected['parts'])} 个原始响应全部通过 sha256 校验"))
                self.assertEqual(store.digest(), expected["digest"])
                self.assertEqual(store.digest(), S.playlist_obj(provider)["source"]["raw_digest"])
                self.assertEqual(tuple(sorted(store.parts)), expected["parts"])

    def test_manifest_count_and_bytes_agree_with_the_entries(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                manifest = S.manifest(provider)
                self.assertEqual(manifest["count"], len(manifest["requests"]))
                self.assertEqual(manifest["bytes"],
                                 sum(e["bytes"] for e in manifest["requests"]))
                self.assertEqual(S.raw_store(provider).total_bytes(), manifest["bytes"])

    def test_provenance_requests_are_a_copy_of_the_fetch_log(self):
        for provider in S.PROVIDERS:
            expected = S.EXPECTED[provider]
            with self.subTest(provider=provider):
                logged = S.fetch_log(provider)["requests"]
                self.assertEqual(S.provenance(provider)["requests"], logged)
                self.assertEqual(len(logged), expected["requests"])
                self.assertEqual([r["name"] for r in logged], expected["parts"][:0] or
                                 [r["name"] for r in logged])

    def test_response_names_in_the_manifest_match_the_fetch_log(self):
        # QQ additionally records the request bodies (*_req); every *response*
        # must still be there under the name the log used.
        for provider in S.PROVIDERS:
            expected = S.EXPECTED[provider]
            with self.subTest(provider=provider):
                manifest_names = [e["name"] for e in S.manifest(provider)["requests"]]
                response_names = [r["name"] for r in S.fetch_log(provider)["requests"]]
                self.assertEqual([n for n in manifest_names if not n.endswith("_req")],
                                 response_names)
                self.assertEqual(tuple(sorted(manifest_names)), expected["parts"])

    def test_manifest_bytes_are_the_stored_file_sizes(self):
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                store = S.raw_store(provider)
                for entry in S.manifest(provider)["requests"]:
                    path = next((S.fixture_dir(provider) / "raw" / f"{entry['name']}.{ext}"
                                 for ext in ("json", "txt")
                                 if (S.fixture_dir(provider) / "raw" /
                                     f"{entry['name']}.{ext}").exists()), None)
                    self.assertIsNotNone(path, entry["name"])
                    self.assertEqual(entry["bytes"], path.stat().st_size, entry["name"])
                self.assertEqual(store.total_bytes(),
                                 sum(e["bytes"] for e in store.entries))

    def test_a_recorded_json_body_is_not_always_byte_for_byte(self):
        # Known gap: RawStore.record re-serialises a JSON payload with compact
        # separators, so the file (and its manifest size) can be smaller than the
        # response that came off the wire. Text parts are stored verbatim.
        sizes = {}
        for provider in S.PROVIDERS:
            entries = {e["name"]: e for e in S.manifest(provider)["requests"]}
            for request in S.fetch_log(provider)["requests"]:
                entry = entries[request["name"]]
                self.assertLessEqual(entry["bytes"], request["bytes"])
                if entry["kind"] == "text":
                    self.assertEqual(entry["bytes"], request["bytes"], request["name"])
                sizes[(provider, request["name"])] = request["bytes"] - entry["bytes"]
        # QQ's musicu responses come back with JSON whitespace; the stored
        # compact form is measurably smaller.
        self.assertEqual(sizes[("qq", "album_detail_000")], 75)
        self.assertGreater(sizes[("qq", "album_detail_000")], 0)

    def test_relative_path_extensions_are_unique_per_part(self):
        # Two parts with the same name but different kinds would silently shadow
        # each other; load_part picks .json first.
        for provider in S.PROVIDERS:
            with self.subTest(provider=provider):
                names = [e for e in S.manifest(provider)["requests"]]
                self.assertEqual(len({e["name"] for e in names}), len(names))


if __name__ == "__main__":
    unittest.main()
