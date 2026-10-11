"""HTTP with an honest error taxonomy, polite pacing and raw recording.

This replaces the original scripts' `urllib.request.urlopen(...)` + `sys.exit(1)`.
Every failure becomes a FetchError whose ``kind`` says what actually happened,
so the CLI can distinguish "upstream blocked us" (give up, exit 2) from "slow
network" (retry) from "the response shape changed" (exit 3, contract alarm).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import (KIND_BLOCKED, KIND_HTTP, KIND_NETWORK, KIND_NOTFOUND,
                     KIND_PARSE, FetchError)
from .rawstore import RawStore

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, provider: str, store: RawStore, *, timeout: float = 25.0,
                 retries: int = 2, backoff: float = 1.5, min_interval: float = 0.0,
                 headers: dict[str, str] | None = None):
        self.provider = provider
        self.store = store
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.min_interval = min_interval
        self.headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        self.headers.update(headers or {})
        self.log: list[dict] = []
        self.errors: list[dict] = []
        self._last = 0.0
        self.sleep = time.sleep

    # -- low level -----------------------------------------------------------
    def request(self, name: str, url: str, *, method: str = "GET",
                data: dict | str | bytes | None = None,
                headers: dict[str, str] | None = None,
                expect: str = "json", record: bool = True, record_body: bool = False,
                json_body: Any = None, error_hints: dict[int, str] | None = None) -> Any:
        """Perform one request, retrying only what is worth retrying.

        ``record_body`` also stores the request payload. Some APIs (QQ's musicu)
        are only decodable together with the request that produced them - the
        response keys are ``req_0``, ``req_1``... in request order - so keeping
        the body is what makes offline re-derivation possible.
        """
        body: bytes | None = None
        hdrs = dict(self.headers)
        hdrs.update(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        elif isinstance(data, bytes):
            body = data
        if record_body and record:
            body_obj = json_body if json_body is not None else data
            if isinstance(body_obj, str):
                # Store the structured form when the body is JSON: it makes the
                # response decodable offline without re-implementing the encoder.
                try:
                    body_obj = json.loads(body_obj)
                except ValueError:
                    pass
            self.store.record(f"{name}_req", url, body_obj,
                              method=method, kind="json")

        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 2):
            self._pace()
            started = time.time()
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status = resp.status
                    text = raw.decode("utf-8", "replace")
                    payload = json.loads(text) if expect == "json" else text
                    self._note(name, url, method, status, len(raw), started)
                    if record:
                        self.store.record(name, url, payload, method=method,
                                          status=status, kind=expect)
                    return payload
            except urllib.error.HTTPError as exc:
                status = exc.code
                hint = (error_hints or {}).get(status)
                self._note(name, url, method, status, 0, started, error=f"HTTP {status}")
                if status in _RETRY_STATUS and attempt <= self.retries:
                    last_exc = self._retry(name, status, attempt)
                    continue
                raise self._fail(self._http_error(name, url, status, hint)) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                self._note(name, url, method, None, 0, started, error=str(exc))
                if attempt <= self.retries:
                    last_exc = FetchError(f"{self.provider}: {exc} ({url})", kind=KIND_NETWORK,
                                          context={"url": url, "attempt": attempt})
                    self.sleep(self.backoff * attempt)
                    continue
                raise self._fail(FetchError(
                    f"{self.provider}: network failed after {attempt} attempts: {exc}",
                    kind=KIND_NETWORK, context={"url": url})) from exc
            except json.JSONDecodeError as exc:
                self._note(name, url, method, 200, 0, started, error="bad json")
                raise self._fail(FetchError(
                    f"{self.provider}: response was not JSON ({url}): {exc}",
                    kind=KIND_PARSE, context={"url": url})) from exc
        raise last_exc or FetchError("unreachable", kind=KIND_NETWORK)

    # -- helpers -------------------------------------------------------------
    def get_json(self, name: str, url: str, **kw) -> Any:
        return self.request(name, url, **kw)

    def get_text(self, name: str, url: str, **kw) -> str:
        kw.setdefault("expect", "text")
        return self.request(name, url, **kw)

    def post_json(self, name: str, url: str, **kw) -> Any:
        kw.setdefault("method", "POST")
        return self.request(name, url, **kw)

    def finish(self) -> dict:
        return {"provider": self.provider, "requests": self.log}

    # -- internals -----------------------------------------------------------
    def _pace(self) -> None:
        if self.min_interval > 0:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = time.monotonic()

    def _note(self, name: str, url: str, method: str, status: int | None,
              size: int, started: float, error: str | None = None) -> None:
        entry = {"name": name, "url": url, "method": method, "status": status,
                 "bytes": size, "ms": int((time.time() - started) * 1000)}
        if error:
            entry["error"] = error
        self.log.append(entry)

    def _retry(self, name: str, status: int, attempt: int) -> FetchError:
        err = FetchError(f"{self.provider}: HTTP {status} on {name}, retrying",
                         kind=KIND_HTTP, context={"name": name, "status": status})
        self.sleep(self.backoff * attempt)
        return err

    def _fail(self, exc: FetchError) -> FetchError:
        """Remember every terminal failure: the fetch log must not hide them."""
        self.errors.append({"kind": exc.kind, "message": exc.message,
                            "context": exc.context})
        return exc

    def _http_error(self, name: str, url: str, status: int, hint: str | None) -> FetchError:
        ctx = {"url": url, "name": name, "status": status}
        if hint:
            ctx["hint"] = hint
        if status in (401, 402, 403):
            return FetchError(f"{self.provider}: HTTP {status} (upstream refused the request)"
                              + (f" - {hint}" if hint else ""), kind=KIND_BLOCKED, context=ctx)
        if status == 404:
            return FetchError(f"{self.provider}: HTTP 404 (no such resource: {name})",
                              kind=KIND_NOTFOUND, context=ctx)
        return FetchError(f"{self.provider}: HTTP {status} on {name}",
                          kind=KIND_HTTP, context=ctx)
