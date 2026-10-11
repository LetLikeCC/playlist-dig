"""The canonical data contract: mpa/playlist@1.

Everything downstream reads this structure and nothing else. Two rules make it
worth calling a dataset:

1. Every value carries its provenance (``source``). A value we got from the
   platform, a value we computed, and a value we could not get are three
   different things and must never be mixed into one column.
2. Nothing is invented. A field we cannot obtain is ``unavailable`` with a
   ``reason`` - never a placeholder, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

SCHEMA_PLAYLIST = "mpa/playlist@1"
ADAPTER_VERSION = "2.0.0"

# value provenance
SRC_PLATFORM = "platform"        # taken verbatim from the provider response
SRC_DERIVED = "derived"          # computed by us (documented method, maybe low confidence)
SRC_UNAVAILABLE = "unavailable"  # provider does not return it, or the item is gone

TRACK_OK = "ok"
TRACK_UNAVAILABLE = "unavailable"

#: fields every track carries; adapters declare which ones they can fill
TRACK_FIELDS = ("duration_ms", "language", "released_at", "collected_at")


def _clean(obj: dict) -> dict:
    """Drop keys whose value is None/empty so written JSON stays readable."""
    return {k: v for k, v in obj.items() if v not in (None, [], {}, "")}


@dataclass
class Field:
    value: Any = None
    source: str = SRC_UNAVAILABLE
    reason: str | None = None
    method: str | None = None
    confidence: str | None = None
    raw: Any = None

    # -- constructors --------------------------------------------------------
    @classmethod
    def platform(cls, value: Any, *, raw: Any = None, reason: str | None = None) -> "Field":
        """A value straight from the provider. None means 'provider had none'."""
        if value in (None, ""):
            return cls.unavailable(reason or "provider returned no value")
        return cls(value=value, source=SRC_PLATFORM, raw=raw if raw != value else None)

    @classmethod
    def derived(cls, value: Any, *, method: str, confidence: str = "low",
                raw: Any = None) -> "Field":
        if value in (None, ""):
            return cls.unavailable("nothing to derive from")
        return cls(value=value, source=SRC_DERIVED, method=method,
                   confidence=confidence, raw=raw)

    @classmethod
    def unavailable(cls, reason: str) -> "Field":
        return cls(value=None, source=SRC_UNAVAILABLE, reason=reason)

    # -- helpers -------------------------------------------------------------
    @property
    def known(self) -> bool:
        return self.value is not None and self.source in (SRC_PLATFORM, SRC_DERIVED)

    def to_obj(self) -> dict:
        return _clean({"value": self.value, "source": self.source, "reason": self.reason,
                       "method": self.method, "confidence": self.confidence, "raw": self.raw})

    @classmethod
    def from_obj(cls, obj: Any) -> "Field":
        if obj is None:
            return cls.unavailable("absent from stored data")
        if not isinstance(obj, dict):
            return cls(value=obj, source=SRC_PLATFORM)
        return cls(value=obj.get("value"), source=obj.get("source", SRC_UNAVAILABLE),
                   reason=obj.get("reason"), method=obj.get("method"),
                   confidence=obj.get("confidence"), raw=obj.get("raw"))


@dataclass
class Artist:
    name: str
    id: str | None = None

    def to_obj(self) -> dict:
        return _clean({"name": self.name, "id": self.id})

    @classmethod
    def from_obj(cls, obj: dict) -> "Artist":
        return cls(name=obj.get("name", ""), id=obj.get("id"))


@dataclass
class Album:
    name: str | None = None
    id: str | None = None
    release_date: str | None = None

    def to_obj(self) -> dict:
        return _clean({"name": self.name, "id": self.id, "release_date": self.release_date})

    @classmethod
    def from_obj(cls, obj: dict | None) -> "Album":
        obj = obj or {}
        return cls(name=obj.get("name"), id=obj.get("id"), release_date=obj.get("release_date"))


@dataclass
class Track:
    index: int
    title: str | None = None
    id: str | None = None
    status: str = TRACK_OK
    reason: str | None = None
    artists: list[Artist] = dc_field(default_factory=list)
    album: Album | None = None
    duration_ms: Field = dc_field(default_factory=lambda: Field.unavailable("not fetched"))
    language: Field = dc_field(default_factory=lambda: Field.unavailable("not fetched"))
    released_at: Field = dc_field(default_factory=lambda: Field.unavailable("not fetched"))
    collected_at: Field = dc_field(default_factory=lambda: Field.unavailable("not fetched"))
    extra: dict[str, Field] = dc_field(default_factory=dict)

    @property
    def artist_names(self) -> list[str]:
        return [a.name for a in self.artists if a.name]

    @property
    def ok(self) -> bool:
        return self.status == TRACK_OK

    def to_obj(self) -> dict:
        obj = {
            "index": self.index,
            "status": self.status,
            "reason": self.reason,
            "id": self.id,
            "title": self.title,
            "artists": [a.to_obj() for a in self.artists],
            "album": (self.album or Album()).to_obj(),
            "duration_ms": self.duration_ms.to_obj(),
            "language": self.language.to_obj(),
            "released_at": self.released_at.to_obj(),
            "collected_at": self.collected_at.to_obj(),
        }
        if self.extra:
            obj["extra"] = {k: v.to_obj() for k, v in self.extra.items()}
        return _clean(obj)

    @classmethod
    def from_obj(cls, obj: dict) -> "Track":
        extra = {k: Field.from_obj(v) for k, v in (obj.get("extra") or {}).items()}
        return cls(
            index=obj.get("index", 0), title=obj.get("title"), id=obj.get("id"),
            status=obj.get("status", TRACK_OK), reason=obj.get("reason"),
            artists=[Artist.from_obj(a) for a in obj.get("artists") or []],
            album=Album.from_obj(obj.get("album")),
            duration_ms=Field.from_obj(obj.get("duration_ms")),
            language=Field.from_obj(obj.get("language")),
            released_at=Field.from_obj(obj.get("released_at")),
            collected_at=Field.from_obj(obj.get("collected_at")),
            extra=extra,
        )


@dataclass
class Ledger:
    """The four numbers that must always be answerable.

    Any difference between ``declared`` and ``ok`` has a name and a reason; that
    is the whole point of this class.
    """

    declared: int | None = None
    fetched: int = 0        # records we actually materialised (ok + unavailable)
    ok: int = 0
    unavailable: int = 0
    duplicates_removed: int = 0
    reasons: dict[str, int] = dc_field(default_factory=dict)

    def finalize(self) -> "Ledger":
        self.reasons = {}
        return self

    @property
    def complete(self) -> bool:
        if self.declared is None:
            return False
        return self.ok + self.unavailable >= self.declared and self.unavailable == 0

    @property
    def gap(self) -> int | None:
        return None if self.declared is None else max(0, self.declared - self.fetched)

    def to_obj(self) -> dict:
        obj = _clean({
            "declared": self.declared, "fetched": self.fetched, "ok": self.ok,
            "unavailable": self.unavailable,
            "duplicates_removed": self.duplicates_removed or None,
            "gap": self.gap, "reasons": self.reasons or None,
        })
        # declared/gap 的 null 是有信息量的（平台没说总数 → 无法证明完整），
        # 不能和「没这个字段」混为一谈，所以这两项即使为 null 也照写。
        return {"declared": self.declared, **obj, "gap": self.gap}

    @classmethod
    def from_obj(cls, obj: dict) -> "Ledger":
        obj = obj or {}
        return cls(declared=obj.get("declared"), fetched=obj.get("fetched", 0),
                   ok=obj.get("ok", 0), unavailable=obj.get("unavailable", 0),
                   duplicates_removed=obj.get("duplicates_removed", 0),
                   reasons=obj.get("reasons") or {})


@dataclass
class SourceInfo:
    provider: str
    input: str
    adapter_version: str = ADAPTER_VERSION
    upstream_commit: str | None = None
    fetched_at: str | None = None
    mode: str | None = None            # direct-http | ssr-partial |
                                       # node-service-local | node-service-external
    raw_saved: bool = False
    raw_digest: str | None = None
    requests: int = 0

    def to_obj(self) -> dict:
        return _clean({"provider": self.provider, "input": self.input,
                       "adapter_version": self.adapter_version,
                       "upstream_commit": self.upstream_commit,
                       "fetched_at": self.fetched_at, "mode": self.mode,
                       "raw_saved": self.raw_saved, "raw_digest": self.raw_digest,
                       "requests": self.requests or None})

    @classmethod
    def from_obj(cls, obj: dict) -> "SourceInfo":
        return cls(provider=obj.get("provider", "?"), input=obj.get("input", ""),
                   adapter_version=obj.get("adapter_version", "?"),
                   upstream_commit=obj.get("upstream_commit"),
                   fetched_at=obj.get("fetched_at"), mode=obj.get("mode"),
                   raw_saved=obj.get("raw_saved", False), raw_digest=obj.get("raw_digest"),
                   requests=obj.get("requests", 0))


@dataclass
class PlaylistMeta:
    id: str | None = None
    title: str | None = None
    creator_name: str | None = None
    creator_id: str | None = None
    description: str | None = None
    declared_count: int | None = None
    #: 歌单级平台专属字段（标签、创建/更新时间……），与 ``Track.extra`` 同一套
    #: ``Field`` 语义：只在平台当次真的给了的时候才出现。``capabilities`` 里用
    #: ``playlist.`` 前缀区分（不带前缀的是曲目级），见 docs/schema.md 第 3.3 节。
    extra: dict[str, Field] = dc_field(default_factory=dict)

    def to_obj(self) -> dict:
        obj = _clean({"id": self.id, "title": self.title,
                      "creator": _clean({"name": self.creator_name, "id": self.creator_id}),
                      "description": self.description,
                      "declared_count": self.declared_count})
        if self.extra:
            obj["extra"] = {k: v.to_obj() for k, v in self.extra.items()}
        return obj

    @classmethod
    def from_obj(cls, obj: dict) -> "PlaylistMeta":
        obj = obj or {}
        creator = obj.get("creator") or {}
        return cls(id=obj.get("id"), title=obj.get("title"),
                   creator_name=creator.get("name"), creator_id=creator.get("id"),
                   description=obj.get("description"),
                   declared_count=obj.get("declared_count"),
                   extra={k: Field.from_obj(v)
                          for k, v in (obj.get("extra") or {}).items()})


@dataclass
class Playlist:
    source: SourceInfo
    playlist: PlaylistMeta = dc_field(default_factory=PlaylistMeta)
    ledger: Ledger = dc_field(default_factory=Ledger)
    capabilities: dict[str, bool] = dc_field(default_factory=dict)
    tracks: list[Track] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    schema: str = SCHEMA_PLAYLIST

    def tracks_ok(self) -> list[Track]:
        return [t for t in self.tracks if t.ok]

    def to_obj(self) -> dict:
        return {
            "schema": self.schema,
            "source": self.source.to_obj(),
            "playlist": self.playlist.to_obj(),
            "ledger": self.ledger.to_obj(),
            "capabilities": self.capabilities,
            "warnings": self.warnings,
            "tracks": [t.to_obj() for t in self.tracks],
        }

    @classmethod
    def from_obj(cls, obj: dict) -> "Playlist":
        return cls(
            schema=obj.get("schema", SCHEMA_PLAYLIST),
            source=SourceInfo.from_obj(obj.get("source") or {}),
            playlist=PlaylistMeta.from_obj(obj.get("playlist") or {}),
            ledger=Ledger.from_obj(obj.get("ledger") or {}),
            capabilities=obj.get("capabilities") or {},
            warnings=obj.get("warnings") or [],
            tracks=[Track.from_obj(t) for t in obj.get("tracks") or []],
        )
