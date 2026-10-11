"""mpa/errors.py - the error taxonomy and the exit-code table.

These strings are a public contract: they appear in JSON output, in the fetch
log and in the README's exit-code table.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mpa import errors
from mpa.errors import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_ROUTE_UNAVAILABLE,
                        EXIT_UPSTREAM_CHANGED, EXIT_USAGE, KIND_BLOCKED, KIND_HTTP,
                        KIND_INPUT, KIND_NETWORK, KIND_NOTFOUND, KIND_PARSE,
                        KIND_PRIVATE, KIND_ROUTE_UNAVAILABLE, KIND_TRUNCATED,
                        KIND_UPSTREAM_CHANGED, RETRYABLE, FetchError, InputError,
                        MPError, RouteUnavailable, UpstreamChanged, exit_code_for)


class TestKindVocabulary(unittest.TestCase):
    def test_kind_strings_are_stable(self):
        self.assertEqual(
            {
                "KIND_INPUT": KIND_INPUT, "KIND_NETWORK": KIND_NETWORK,
                "KIND_HTTP": KIND_HTTP, "KIND_BLOCKED": KIND_BLOCKED,
                "KIND_NOTFOUND": KIND_NOTFOUND, "KIND_PRIVATE": KIND_PRIVATE,
                "KIND_PARSE": KIND_PARSE, "KIND_UPSTREAM_CHANGED": KIND_UPSTREAM_CHANGED,
                "KIND_TRUNCATED": KIND_TRUNCATED,
                "KIND_ROUTE_UNAVAILABLE": KIND_ROUTE_UNAVAILABLE,
            },
            {
                "KIND_INPUT": "input", "KIND_NETWORK": "network", "KIND_HTTP": "http",
                "KIND_BLOCKED": "blocked", "KIND_NOTFOUND": "notfound",
                "KIND_PRIVATE": "private", "KIND_PARSE": "parse",
                "KIND_UPSTREAM_CHANGED": "upstream_changed",
                "KIND_TRUNCATED": "truncated",
                "KIND_ROUTE_UNAVAILABLE": "route_unavailable",
            })

    def test_only_network_and_http_are_retryable(self):
        self.assertEqual(RETRYABLE, {KIND_NETWORK, KIND_HTTP})

    def test_exit_code_table(self):
        self.assertEqual(
            (EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_UPSTREAM_CHANGED, EXIT_USAGE,
             EXIT_ROUTE_UNAVAILABLE),
            (0, 1, 2, 3, 4, 5))


class TestExceptionHierarchy(unittest.TestCase):
    def test_every_error_is_an_mperror(self):
        for cls in (InputError, FetchError, UpstreamChanged, RouteUnavailable):
            self.assertTrue(issubclass(cls, MPError))

    def test_specialised_fetch_errors_keep_the_base_class(self):
        # `except FetchError` in the adapters must keep catching these.
        for cls in (UpstreamChanged, RouteUnavailable):
            self.assertTrue(issubclass(cls, FetchError))

    def test_default_kinds(self):
        self.assertEqual(MPError("m").kind, KIND_INPUT)
        self.assertEqual(InputError("m").kind, KIND_INPUT)
        self.assertEqual(FetchError("m").kind, KIND_NETWORK)
        self.assertEqual(UpstreamChanged("m").kind, KIND_UPSTREAM_CHANGED)
        self.assertEqual(RouteUnavailable("m").kind, KIND_ROUTE_UNAVAILABLE)

    def test_fetch_error_kind_can_be_overridden(self):
        self.assertEqual(FetchError("m", kind=KIND_BLOCKED).kind, KIND_BLOCKED)
        # An empty override must not wipe the default.
        self.assertEqual(FetchError("m", kind=None).kind, KIND_NETWORK)
        self.assertEqual(FetchError("m", kind="").kind, KIND_NETWORK)

    def test_message_and_context(self):
        exc = FetchError("boom", kind=KIND_NOTFOUND, context={"status": 404})
        self.assertEqual(str(exc), "boom")
        self.assertEqual(exc.message, "boom")
        self.assertEqual(exc.context, {"status": 404})

    def test_context_defaults_to_an_independent_dict(self):
        first, second = FetchError("a"), FetchError("b")
        first.context["x"] = 1
        self.assertEqual(second.context, {})

    def test_to_json_shape(self):
        exc = FetchError("boom", kind=KIND_HTTP, context={"status": 500})
        self.assertEqual(exc.to_json(),
                         {"kind": "http", "message": "boom", "context": {"status": 500}})


class TestRetryable(unittest.TestCase):
    def test_retryable_only_for_transient_failures(self):
        self.assertTrue(FetchError("m", kind=KIND_NETWORK).retryable)
        self.assertTrue(FetchError("m", kind=KIND_HTTP).retryable)
        for kind in (KIND_BLOCKED, KIND_NOTFOUND, KIND_PRIVATE, KIND_PARSE,
                     KIND_UPSTREAM_CHANGED, KIND_TRUNCATED, KIND_ROUTE_UNAVAILABLE,
                     KIND_INPUT):
            with self.subTest(kind=kind):
                self.assertFalse(FetchError("m", kind=kind).retryable)

    def test_specialised_errors_are_not_retryable(self):
        self.assertFalse(UpstreamChanged("m").retryable)
        self.assertFalse(RouteUnavailable("m").retryable)
        self.assertFalse(InputError("m").retryable)


class TestExitCodeFor(unittest.TestCase):
    def test_mapping(self):
        cases = [
            (InputError("bad args"), EXIT_USAGE),
            (RouteUnavailable("needs node"), EXIT_ROUTE_UNAVAILABLE),
            (UpstreamChanged("shape drifted"), EXIT_UPSTREAM_CHANGED),
            (FetchError("network"), EXIT_FAILED),
            (MPError("generic"), EXIT_FAILED),
            (ValueError("not ours at all"), EXIT_FAILED),
            (RuntimeError("boom"), EXIT_FAILED),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(exit_code_for(exc), expected)

    def test_input_error_wins_over_fetch_specific_codes(self):
        # Order matters: InputError is not a FetchError, but a subclass of
        # FetchError that also looked like input must still be classified once.
        class Weird(InputError, FetchError):
            pass

        self.assertEqual(exit_code_for(Weird("m")), EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
