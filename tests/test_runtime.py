"""mpa/runtime.py - HTTP with an honest error taxonomy, retries and recording.

No test here touches the network: ``urllib.request.urlopen`` is replaced by a
fake for the duration of each test, and ``HttpClient.sleep`` is monkeypatched so
the suite never really waits (the tests assert on the *pacing decisions*, not on
wall-clock time).
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.errors import (KIND_BLOCKED, KIND_HTTP, KIND_NETWORK, KIND_NOTFOUND,
                        KIND_PARSE)
from mpa.rawstore import RawStore
from mpa.runtime import _RETRY_STATUS, DEFAULT_UA, HttpClient


class RuntimeCase(unittest.TestCase):
    """Common wiring: a temp store, a client whose sleep never happens."""

    retries = 2
    min_interval = 0.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RawStore(Path(self.tmp.name) / "raw")
        self.client = self.make_client()
        self.calls = []

    def make_client(self, **kwargs):
        opts = {"retries": self.retries, "min_interval": self.min_interval}
        opts.update(kwargs)
        client = HttpClient("test", self.store, **opts)
        self.slept = []
        client.sleep = self.slept.append
        return client

    def serve(self, handler):
        return S.install_urlopen(self, handler)

    def serve_json(self, obj, *, status=200):
        return self.serve(lambda req, attempt: S.json_response(obj, status=status))


class TestSuccessPath(RuntimeCase):
    def test_json_response_is_returned_logged_and_recorded(self):
        fake = self.serve_json({"code": 200, "data": {"x": 1}})
        payload = self.client.request("detail", "http://example.invalid/1")
        self.assertEqual(payload, {"code": 200, "data": {"x": 1}})
        self.assertEqual(fake.count, 1)

        entry = self.client.log[-1]
        self.assertEqual(entry["name"], "detail")
        self.assertEqual(entry["url"], "http://example.invalid/1")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["status"], 200)
        self.assertIsInstance(entry["ms"], int)
        self.assertNotIn("error", entry)

        self.assertEqual([e["name"] for e in self.store.entries], ["detail"])
        self.assertEqual(self.store.parts["detail"], payload)
        self.assertEqual(self.store.entries[0]["status"], 200)
        self.assertEqual(self.store.entries[0]["kind"], "json")
        self.assertEqual(self.client.errors, [])

    def test_bytes_are_the_encoded_body_length_on_both_sides(self):
        # The fetch log records what came off the wire; the manifest records the
        # file on disk. For a JSON part those are two different serialisations
        # (see test_json_parts_are_restored_compactly_not_byte_for_byte).
        body = json.dumps({"中文": "值"}, ensure_ascii=False).encode("utf-8")
        self.serve(lambda req, attempt: S.FakeResponse(body))
        self.client.request("x", "http://example.invalid/2")
        self.assertEqual(self.client.log[-1]["bytes"], len(body))
        stored = (self.store.dir / "x.json").read_bytes()
        self.assertEqual(self.store.entries[0]["bytes"], len(stored))

    def test_json_parts_are_restored_compactly_not_byte_for_byte(self):
        # Documented behaviour, with a sharp edge: rawstore re-serialises a JSON
        # payload with compact separators instead of writing the received bytes.
        # The parsed value survives; the exact wire bytes do not.
        body = b'{"a": 1, "b": 2}'
        self.serve(lambda req, attempt: S.FakeResponse(body))
        self.client.request("x", "http://example.invalid/2b")
        self.assertEqual((self.store.dir / "x.json").read_text(encoding="utf-8"),
                         '{"a":1,"b":2}')
        self.assertNotEqual(self.client.log[-1]["bytes"], self.store.entries[0]["bytes"])

    def test_text_expectation_skips_json_parsing(self):
        self.serve(lambda req, attempt: S.text_response("not json at all"))
        text = self.client.get_text("page", "http://example.invalid/3")
        self.assertEqual(text, "not json at all")
        self.assertEqual(self.store.entries[0]["kind"], "text")
        self.assertEqual((self.store.dir / "page.txt").read_text(encoding="utf-8"),
                         "not json at all")

    def test_default_headers_include_a_browser_user_agent(self):
        fake = self.serve_json({})
        self.client.request("x", "http://example.invalid/4")
        req = fake.requests[0]
        self.assertEqual(req.get_header("User-agent"), DEFAULT_UA)
        self.assertEqual(req.get_header("Accept-language"), "zh-CN,zh;q=0.9")

    def test_call_headers_are_merged_over_the_defaults(self):
        fake = self.serve_json({})
        self.client.request("x", "http://example.invalid/5",
                            headers={"Referer": "http://ref/", "User-Agent": "custom"})
        req = fake.requests[0]
        self.assertEqual(req.get_header("Referer"), "http://ref/")
        self.assertEqual(req.get_header("User-agent"), "custom")

    def test_per_call_headers_do_not_leak_into_the_next_request(self):
        fake = self.serve_json({})
        self.client.request("a", "http://example.invalid/6", headers={"Referer": "r"})
        self.client.request("b", "http://example.invalid/7")
        self.assertIsNone(fake.requests[1].get_header("Referer"))

    def test_get_json_is_an_alias_for_request(self):
        self.serve_json({"a": 1})
        self.assertEqual(self.client.get_json("x", "http://example.invalid/8"), {"a": 1})

    def test_post_json_sends_a_json_body_and_records_the_request(self):
        fake = self.serve_json({"req_0": {}})
        payload = {"comm": {"ct": 24}}
        self.client.post_json("musicu", "http://example.invalid/9", json_body=payload,
                              record_body=True)
        req = fake.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.data.decode("utf-8")), payload)
        self.assertEqual(req.get_header("Content-type"),
                         "application/json; charset=utf-8")
        self.assertEqual([e["name"] for e in self.store.entries],
                         ["musicu_req", "musicu"])
        self.assertEqual(self.store.parts["musicu_req"], payload)

    def test_data_dict_is_form_encoded(self):
        fake = self.serve_json({})
        self.client.request("f", "http://example.invalid/10", data={"ids": "[1,2]", "n": "1"},
                            method="POST")
        req = fake.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(urllib.parse.parse_qs(req.data.decode("utf-8")),
                         {"ids": ["[1,2]"], "n": ["1"]})
        self.assertEqual(req.get_header("Content-type"),
                         "application/x-www-form-urlencoded")

    def test_str_and_bytes_bodies_are_sent_verbatim(self):
        fake = self.serve_json({})
        self.client.request("s", "http://example.invalid/11", data="raw text", method="POST")
        self.assertEqual(fake.requests[0].data, b"raw text")
        self.client.request("b", "http://example.invalid/12", data=b"\x00\x01", method="POST")
        self.assertEqual(fake.requests[1].data, b"\x00\x01")

    def test_body_recorded_from_a_json_string_is_stored_structured(self):
        # QQ's responses are keyed by request order, so the *structured* request
        # body is what makes offline re-derivation possible.
        self.serve_json({})
        self.client.post_json("mu", "http://example.invalid/13",
                              data=json.dumps({"comm": {"ct": 24}}), record_body=True)
        self.assertEqual(self.store.parts["mu_req"], {"comm": {"ct": 24}})
        self.assertEqual(self.store.entries[0]["kind"], "json")

    def test_non_json_string_body_is_recorded_as_is(self):
        self.serve_json({})
        self.client.request("mu", "http://example.invalid/14", data="ct=24&v=1",
                            method="POST", record_body=True)
        self.assertEqual(self.store.parts["mu_req"], "ct=24&v=1")

    def test_record_body_is_ignored_when_recording_is_off(self):
        self.serve_json({})
        self.client.request("mu", "http://example.invalid/15", json_body={"a": 1},
                            record_body=True, record=False)
        self.assertEqual(self.store.entries, [])
        self.assertEqual(self.store.parts, {})
        self.assertEqual(len(self.client.log), 1)

    def test_record_false_still_logs_but_stores_nothing(self):
        self.serve_json({"a": 1})
        payload = self.client.request("x", "http://example.invalid/16", record=False)
        self.assertEqual(payload, {"a": 1})
        self.assertEqual(self.store.entries, [])
        self.assertEqual(self.client.log[-1]["name"], "x")

    def test_finish_returns_provider_and_the_request_log(self):
        self.serve_json({})
        self.client.request("x", "http://example.invalid/17")
        self.assertEqual(self.client.finish(),
                         {"provider": "test", "requests": self.client.log})


class TestRetries(RuntimeCase):
    def test_retryable_status_is_retried_then_succeeds(self):
        def handler(req, attempt):
            if attempt < 3:
                return S.http_error(req.full_url, 503)
            return S.json_response({"ok": True})

        fake = self.serve(handler)
        self.assertEqual(self.client.request("x", "http://example.invalid/1"), {"ok": True})
        self.assertEqual(fake.count, 3)
        self.assertEqual([e["status"] for e in self.client.log], [503, 503, 200])
        self.assertEqual([e["error"] for e in self.client.log if "error" in e],
                         ["HTTP 503", "HTTP 503"])
        self.assertEqual(self.client.errors, [])
        # backoff * attempt, and no sleeping between successful attempts
        self.assertEqual(len(self.slept), 2)

    def test_network_error_is_retried_then_succeeds(self):
        def handler(req, attempt):
            if attempt == 1:
                return S.url_error()
            return S.json_response({})

        fake = self.serve(handler)
        self.client.request("x", "http://example.invalid/2")
        self.assertEqual(fake.count, 2)
        self.assertEqual(self.client.log[0]["status"], None)
        self.assertIn("error", self.client.log[0])
        self.assertEqual(self.client.errors, [])

    def test_retryable_status_gives_up_after_retries_and_reports_http(self):
        fake = self.serve(lambda req, attempt: S.http_error(req.full_url, 500))
        with self.assertRaises(Exception) as ctx:
            self.client.request("x", "http://example.invalid/3")
        self.assertEqual(type(ctx.exception).__name__, "FetchError")
        self.assertEqual(ctx.exception.kind, KIND_HTTP)
        self.assertEqual(fake.count, self.retries + 1)
        self.assertEqual(len(self.client.log), self.retries + 1)
        self.assertEqual(len(self.client.errors), 1)
        self.assertEqual(self.client.errors[0]["kind"], KIND_HTTP)

    def test_network_gives_up_after_retries_with_the_attempt_count(self):
        fake = self.serve(lambda req, attempt: S.url_error("dns boom"))
        with self.assertRaises(Exception) as ctx:
            self.client.request("x", "http://example.invalid/4")
        exc = ctx.exception
        self.assertEqual(exc.kind, KIND_NETWORK)
        self.assertIn(f"after {self.retries + 1} attempts", exc.message)
        self.assertEqual(exc.context["url"], "http://example.invalid/4")
        self.assertEqual(fake.count, self.retries + 1)

    def test_every_retryable_status_is_actually_retried(self):
        for status in sorted(_RETRY_STATUS):
            with self.subTest(status=status):
                self.setUp()
                fake = self.serve(lambda req, attempt, s=status: S.http_error(req.full_url, s))
                with self.assertRaises(Exception):
                    self.client.request("x", "http://example.invalid/5")
                self.assertEqual(fake.count, self.retries + 1)

    def test_retries_zero_means_a_single_attempt(self):
        self.client = self.make_client(retries=0)
        fake = self.serve(lambda req, attempt: S.url_error())
        with self.assertRaises(Exception):
            self.client.request("x", "http://example.invalid/6")
        self.assertEqual(fake.count, 1)
        self.assertEqual(self.slept, [])

    def test_transient_retries_are_slept_with_linear_backoff(self):
        client = HttpClient("test", self.store, retries=2, backoff=1.5)
        slept = []
        client.sleep = slept.append
        S.install_urlopen(self, lambda req, attempt: S.url_error())
        with self.assertRaises(Exception):
            client.request("x", "http://example.invalid/7")
        self.assertEqual(slept, [1.5, 3.0])


class TestTerminalErrors(RuntimeCase):
    def request_error(self, status, *, hints=None, expect="json", body=None):
        fake = self.serve(lambda req, attempt: S.http_error(req.full_url, status))
        with self.assertRaises(Exception) as ctx:
            self.client.request("song_detail_000", "http://example.invalid/x",
                                expect=expect, error_hints=hints)
        self.assertEqual(fake.count, 1, "a terminal status must not be retried")
        return ctx.exception

    def test_blocked_statuses_map_to_the_blocked_kind(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                exc = self.request_error(status)
                self.assertEqual(exc.kind, KIND_BLOCKED)
                self.assertIn("upstream refused the request", exc.message)

    def test_404_is_notfound_and_names_the_part_that_was_missing(self):
        exc = self.request_error(404)
        self.assertEqual(exc.kind, KIND_NOTFOUND)
        self.assertEqual(exc.message,
                         "test: HTTP 404 (no such resource: song_detail_000)")
        self.assertEqual(exc.context["status"], 404)
        self.assertEqual(exc.context["name"], "song_detail_000")

    def test_other_4xx_is_a_plain_http_error(self):
        exc = self.request_error(400)
        self.assertEqual(exc.kind, KIND_HTTP)
        self.assertEqual(exc.message, "test: HTTP 400 on song_detail_000")

    def test_hints_are_appended_to_the_message_and_kept_in_the_context(self):
        exc = self.request_error(403, hints={403: "歌单仅限登录用户"})
        self.assertEqual(exc.kind, KIND_BLOCKED)
        self.assertTrue(exc.message.endswith("- 歌单仅限登录用户"))
        self.assertEqual(exc.context["hint"], "歌单仅限登录用户")
        self.assertNotIn("hint", self.request_error(403).context)

    def test_bad_json_is_a_parse_error_and_is_not_retried(self):
        fake = self.serve(lambda req, attempt: S.text_response("<html>nope</html>"))
        with self.assertRaises(Exception) as ctx:
            self.client.request("x", "http://example.invalid/8")
        self.assertEqual(ctx.exception.kind, KIND_PARSE)
        self.assertIn("response was not JSON", ctx.exception.message)
        self.assertEqual(fake.count, 1)
        self.assertEqual(self.client.log[-1]["error"], "bad json")

    def test_failures_are_remembered_in_the_log_and_in_errors(self):
        exc = self.request_error(404)
        self.assertEqual(exc.kind, KIND_NOTFOUND)
        self.assertEqual(self.client.errors,
                         [{"kind": KIND_NOTFOUND,
                           "message": "test: HTTP 404 (no such resource: song_detail_000)",
                           "context": {"url": "http://example.invalid/x",
                                       "name": "song_detail_000", "status": 404}}])

    def test_a_failed_request_records_nothing_in_the_store(self):
        self.request_error(404)
        self.assertEqual(self.store.entries, [])

    def test_timeout_error_is_treated_as_a_network_failure(self):
        fake = self.serve(lambda req, attempt: TimeoutError("timed out"))
        with self.assertRaises(Exception) as ctx:
            self.client.request("x", "http://example.invalid/9")
        self.assertEqual(ctx.exception.kind, KIND_NETWORK)
        self.assertEqual(fake.count, self.retries + 1)

    def test_oserror_is_treated_as_a_network_failure(self):
        self.serve(lambda req, attempt: OSError("connection refused"))
        with self.assertRaises(Exception) as ctx:
            self.client.request("x", "http://example.invalid/10")
        self.assertEqual(ctx.exception.kind, KIND_NETWORK)


class TestPacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RawStore(Path(self.tmp.name) / "raw")

    def test_no_sleep_at_all_when_min_interval_is_zero(self):
        client = HttpClient("test", self.store, min_interval=0.0)
        slept = []
        client.sleep = slept.append
        S.install_urlopen(self, lambda req, attempt: S.json_response({}))
        client.request("a", "http://example.invalid/1")
        client.request("b", "http://example.invalid/2")
        self.assertEqual(slept, [])

    def test_a_second_request_waits_out_the_min_interval(self):
        client = HttpClient("test", self.store, min_interval=0.5)
        slept = []
        client.sleep = slept.append
        S.install_urlopen(self, lambda req, attempt: S.json_response({}))
        client.request("a", "http://example.invalid/3")
        client.request("b", "http://example.invalid/4")
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)
        self.assertLessEqual(slept[0], 0.5)

    def test_timeout_is_passed_to_urlopen(self):
        fake = S.install_urlopen(self, lambda req, attempt: S.json_response({}))
        client = HttpClient("test", self.store, timeout=7.5)
        client.sleep = lambda s: None
        client.request("a", "http://example.invalid/5")
        self.assertEqual(fake.timeouts, [7.5])


if __name__ == "__main__":
    unittest.main()
