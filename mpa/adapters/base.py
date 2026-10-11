"""Adapter interface.

The split that makes offline verification possible:

    resolve  - input string  -> Ref          (may hit the network: short links)
    collect  - Ref           -> raw parts    (all network I/O, all recording)
    derive   - raw parts     -> Playlist     (pure function, no I/O, no clock)

``mpa fetch`` runs all three. ``mpa verify`` re-runs only ``derive`` against the
stored parts. If derive is not pure, verification is meaningless - so adapters
must not read the clock, the filesystem or the network inside derive.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..errors import FetchError, InputError, MPError, UpstreamChanged
from ..model import (ADAPTER_VERSION, SCHEMA_PLAYLIST, Field, Ledger, Playlist,
                     PlaylistMeta, SourceInfo, Track, TRACK_OK)
from ..rawstore import RawStore
from ..runtime import HttpClient
from ..timeutil import iso_utc


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Ref:
    """What we decided the user pointed at."""

    provider: str
    key: str                       # provider-native playlist id
    params: dict = dc_field(default_factory=dict)

    def to_obj(self) -> dict:
        return {"provider": self.provider, "key": self.key, "params": self.params}

    @classmethod
    def from_obj(cls, obj: dict) -> "Ref":
        return cls(provider=obj.get("provider", "?"), key=obj.get("key", ""),
                   params=obj.get("params") or {})


@dataclass
class Ctx:
    """Everything derive needs that is not a response body."""

    input: str
    ref: Ref
    mode: str
    upstream_commit: str | None = None
    fetched_at: str | None = None
    extra: dict = dc_field(default_factory=dict)

    def to_obj(self) -> dict:
        return {"input": self.input, "ref": self.ref.to_obj(), "mode": self.mode,
                "upstream_commit": self.upstream_commit,
                "fetched_at": self.fetched_at, "extra": self.extra}

    @classmethod
    def from_obj(cls, obj: dict) -> "Ctx":
        return cls(input=obj.get("input", ""), ref=Ref.from_obj(obj.get("ref") or {}),
                   mode=obj.get("mode", "?"), upstream_commit=obj.get("upstream_commit"),
                   fetched_at=obj.get("fetched_at"), extra=obj.get("extra") or {})


def build_ledger(tracks: list[Track], declared: int | None, *,
                 duplicates: int = 0) -> Ledger:
    ok = sum(1 for t in tracks if t.ok)
    reasons: dict[str, int] = {}
    for t in tracks:
        if not t.ok:
            reasons[t.reason or "unknown"] = reasons.get(t.reason or "unknown", 0) + 1
    return Ledger(declared=declared, fetched=len(tracks), ok=ok,
                  unavailable=len(tracks) - ok, duplicates_removed=duplicates,
                  reasons=reasons)


def optional_text(value) -> str | None:
    """A platform string, or None when it carries nothing.

    ``extra`` 的规矩是「平台当次给的值，去掉首尾空白后为空就不出这个键」——
    一串空格不是平台给的信息（见 docs/schema.md 第 3.2 节）。这里只做这一件事：
    strip，然后决定要不要这个键；不做映射、不截断、不改写。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def names_of(entries) -> list[str] | None:
    """``[{"name": "x"}, …]`` -> ``["x", …]``，全是空白就当没有这个键。

    与 ``Track.artists`` 取 ``artists[].name`` 是同一种「读字段」，不是归类：
    我们只挑平台自己写在 ``name`` 里的那个字符串。
    """
    if not isinstance(entries, list):
        return None
    names = [t for t in (optional_text((e or {}).get("name"))
                         if isinstance(e, dict) else None for e in entries) if t]
    return names or None


def strings_of(values) -> list[str] | None:
    """``["a", " b "]`` -> ``["a", "b"]``；空串丢掉，全空则返回 None。"""
    if not isinstance(values, list):
        return None
    out = [t for t in (optional_text(v) for v in values) if t]
    return out or None


def epoch_field(value, *, unit: str) -> Field:
    """歌单级时间戳：值是 UTC ISO，``raw`` 留平台原始 epoch。

    与 ``released_at`` / ``duration_ms`` 同一条约定：换算单位不改 ``source``
    （平台仍是这个值的来源），原始数字一律留在 ``raw`` 里。``unit`` 只用于
    写清「这个平台的数是秒还是毫秒」，不参与换算（``iso_utc`` 按数值阈值判断）。
    """
    iso = iso_utc(value)
    if not iso:
        return Field.unavailable(f"平台没给这个时间，或它不是合法时间戳（{unit}）")
    return Field.platform(iso, raw=value)


class Adapter:
    name: str = ""
    label: str = ""
    version: str = ADAPTER_VERSION
    url_patterns: tuple[str, ...] = ()
    #: canonical field -> can this provider fill it?
    capabilities: dict[str, bool] = {}
    #: canonical field -> why not (used verbatim as Field.reason)
    field_reasons: dict[str, str] = {}
    upstream_commit: str | None = None
    default_mode: str = "direct-http"
    #: needs a locally run node service (auto-provisioned or supplied)
    requires_node: bool = False
    #: a bare numeric id does not identify this provider (so `mpa fetch 123` must
    #: not silently pick one) - only true for adapters whose ids are plain digits
    digits_are_ambiguous: bool = False
    #: where a human can read about this provider's quirks
    reference: str = ""
    #: last live check against the real platform, and what was checked. ``None``
    #: means "written from documentation, never run end to end" - said out loud
    #: by `mpa adapters` / `mpa doctor` instead of being quietly implied.
    verified_on: str | None = None
    verified_note: str | None = None

    # -- to implement --------------------------------------------------------
    def match(self, raw: str) -> bool:
        raise NotImplementedError

    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raise NotImplementedError

    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict[str, Any]:
        raise NotImplementedError

    def derive(self, parts: dict[str, Any], ctx: Ctx) -> Playlist:
        raise NotImplementedError

    # -- shared --------------------------------------------------------------
    def fetch(self, client: HttpClient, ref: Ref, ctx: Ctx) -> Playlist:
        parts = self.collect(client, ref, ctx)
        self._check_parts_match(parts, client)
        return self.derive(parts, ctx)

    def _check_parts_match(self, parts: dict, client: HttpClient) -> None:
        """Online derivation must see exactly what offline derivation will see.

        ``verify`` re-runs ``derive`` on the recorded parts. If ``collect`` returns
        something slightly different - a request body it never put in ``parts``, a
        name it forgot - the two runs disagree and the data looks non-reproducible
        long after the request is gone. Catch it while the request is still here.
        """
        store = getattr(client, "store", None)
        if store is None or not getattr(store, "write", False):
            return
        recorded = {e["name"] for e in store.entries}
        returned = set(parts)
        if recorded != returned:
            only_recorded = sorted(recorded - returned)
            only_returned = sorted(returned - recorded)
            detail = []
            if only_recorded:
                detail.append(f"只写进 raw/ 没有交给 derive: {only_recorded}")
            if only_returned:
                detail.append(f"derive 用了 raw/ 里没有的 part: {only_returned}")
            raise MPError(
                f"{self.name}: collect 返回的 part 与记录的原始响应不一致（"
                + "；".join(detail) + "）——离线复算会失败，这是适配器的 bug")
        # The value handed to derive must equal the recorded one, name for name.
        # For JSON parts this compares the parsed objects, not the bytes on disk
        # (those are a re-serialisation of the same payload - see rawstore.py).
        for name, value in parts.items():
            if store._parts.get(name) != value:
                raise MPError(f"{self.name}: part {name!r} 与记录的内容不同")

    def unavailable(self, fieldname: str) -> Field:
        return Field.unavailable(self.field_reasons.get(
            fieldname, "provider does not expose this field"))

    def make(self, ctx: Ctx, meta: PlaylistMeta, tracks: list[Track],
             warnings: list[str] | None = None) -> Playlist:
        declared = meta.declared_count
        warnings = list(warnings or [])
        if declared is None:
            # 「平台没说总数」不等于「总数就是取到的数量」。少一个数字就没法
            # 证明完整，这种事必须写进 warnings 并让退出码变成 1（见 _exit_for），
            # 否则静默截断在这里被洗白了——v1最危险的失败就是这种。
            warnings.append(
                f"平台没有给出曲目总数，只取到 {len(tracks)} 首："
                "这份数据无法证明完整（ledger.declared 为 null）")
        return Playlist(
            schema=SCHEMA_PLAYLIST,
            source=SourceInfo(provider=self.name, input=ctx.input,
                              adapter_version=self.version,
                              upstream_commit=ctx.upstream_commit,
                              fetched_at=ctx.fetched_at or now_iso(),
                              mode=ctx.mode, raw_saved=True),
            playlist=meta, ledger=build_ledger(tracks, declared),
            capabilities=dict(self.capabilities), tracks=tracks,
            warnings=warnings,
        )

    # -- helpers for implementations ----------------------------------------
    @staticmethod
    def require(condition: bool, message: str, *, kind: str | None = None) -> None:
        if not condition:
            raise UpstreamChanged(message) if kind is None else FetchError(message, kind=kind)


def clean_int(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def pick(mapping: dict, *keys, default=None):
    for k in keys:
        if isinstance(mapping, dict) and mapping.get(k) not in (None, ""):
            return mapping[k]
    return default


def dedupe_by_id(tracks: list[Track]) -> tuple[list[Track], int]:
    """Keep the first occurrence of each provider id (paginated APIs repeat)."""
    seen: set[str] = set()
    out: list[Track] = []
    removed = 0
    for t in tracks:
        if t.id and t.id in seen:
            removed += 1
            continue
        if t.id:
            seen.add(t.id)
        out.append(t)
    for i, t in enumerate(out):
        t.index = i
    return out, removed


def as_track(index: int, **kw) -> Track:
    kw.setdefault("status", TRACK_OK)
    return Track(index=index, **kw)


def dataclass_public_fields(cls) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]
