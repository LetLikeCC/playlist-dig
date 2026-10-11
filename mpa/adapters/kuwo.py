"""酷我音乐 / 波点 adapter (direct HTTP, no third-party service).

The bd-api requires a specific header set (``plat: h5`` etc.) or answers 402.
The old scripts never touched this API; they shelled out to a third-party service
for 酷我 as well. It turns out to be pure HTTP, which is why 酷我 is the second
provider that works with zero Node dependency.

Namespace warning: www.kuwo.cn songlist ids and bd-api/波点 playlist ids are
*different* namespaces. A www id returns ``{"code":200,"data":{}}`` - an empty
success, the worst possible failure mode - so we refuse it up front.
"""

from __future__ import annotations

import re
import uuid

from ..errors import FetchError, InputError, KIND_NOTFOUND, UpstreamChanged
from ..model import Album, Artist, Field, PlaylistMeta, Track, TRACK_UNAVAILABLE
from ..runtime import HttpClient
from ..textutil import split_artists
from ..timeutil import iso_date
from .base import Adapter, Ctx, Ref, build_ledger, clean_int, pick

BASE = "https://bd-api.kuwo.cn/api"
PAGE_SIZE = 50
MAX_PAGES = 15

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

HEADERS = {
    "plat": "h5",
    "ver": "",
    "devid": "h5",
    "Referer": "https://h5app.kuwo.cn/m/bodian/collection.html",
}

_ID = re.compile(r"[?&](?:playlistId|id)=(\d+)")
_WWW = re.compile(r"www\.kuwo\.cn")
_SOURCE = re.compile(r"[?&]source=(\w+)")
#: value used by 波点 share links (see docs/references/kuwo.md)
DEFAULT_SOURCE = "5"


class KuwoAdapter(Adapter):
    name = "kuwo"
    label = "酷我音乐（波点）"
    url_patterns = (r"kuwo\.cn",)
    reference = "docs/references/kuwo.md"
    # 2026-10-03 首次端到端跑通（用户提供的波点歌单，id 不记录；此前手头没有可用的
    # 样例 id，所以这一行长期写着「未实测」）。
    verified_on = "2026-10-03"
    verified_note = ("用户提供的波点歌单（id 不记录）：声明 236 / 实取 202，gap=34 如实报出"
                     "（退出码 1）、verify --strict 五项全过")
    capabilities = {
        "duration_ms": True,
        "language": True,
        "released_at": True,
        "collected_at": False,
    }
    field_reasons = {
        "collected_at": "酷我接口不返回收藏时间",
        "released_at": "该曲目没有 releaseDate",
        "language": "该曲目没有 language 字段",
    }

    digits_are_ambiguous = True

    def match(self, raw: str) -> bool:
        return "kuwo.cn" in raw or raw.isdigit()

    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raw = raw.strip()
        # `source` comes from the share link and is part of the request; 5 is what
        # 波点 share links carry. Keep it in the Ref so raw/ records which one was
        # used - the response can differ per source.
        m = _SOURCE.search(raw)
        params = {"source": m.group(1)} if m else {"source": DEFAULT_SOURCE}
        if raw.isdigit():
            return Ref(provider=self.name, key=raw, params=params)
        if _WWW.search(raw):
            raise InputError(
                "kuwo: www.kuwo.cn 的歌单 id 与波点(bd-api)不是同一个命名空间；"
                "www 歌单 id 在这个接口上会返回「空成功」，因此拒绝。"
                "请提供 h5app.kuwo.cn/m/bodian/collection.html?playlistId=... 中的 id")
        m = _ID.search(raw)
        if m:
            return Ref(provider=self.name, key=m.group(1), params=params)
        raise InputError(f"kuwo: 无法从输入中解析歌单 id: {raw[:120]}")

    # -- collect -------------------------------------------------------------
    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict:
        source = ref.params.get("source") or DEFAULT_SOURCE
        parts: dict = {}
        info = client.get_json(
            "playlist_info",
            f"{BASE}/service/playlist/info/{ref.key}?source={source}&reqId={uuid.uuid4()}",
            headers={**HEADERS, "User-Agent": MOBILE_UA},
            error_hints={402: "缺少 plat/devid 等必需请求头"})
        self._validate(info, "playlist/info", ref)
        parts["playlist_info"] = info
        declared = clean_int((info.get("data") or {}).get("musicCount"))
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            name = f"music_list_{page:02d}"
            resp = client.get_json(
                name,
                f"{BASE}/service/playlist/{ref.key}/musicList?source={source}"
                f"&pn={page}&rn={PAGE_SIZE}&reqId={uuid.uuid4()}",
                headers={**HEADERS, "User-Agent": MOBILE_UA})
            self._validate(resp, "playlist/musicList", ref)
            items = (resp.get("data") or {}).get("list") or []
            parts[name] = resp
            if not items:
                break
            ids = {str(i.get("id")) for i in items if isinstance(i, dict)}
            if ids and ids <= seen:
                break
            seen |= ids
            total = clean_int((resp.get("data") or {}).get("total"))
            if total is not None and len(seen) >= total:
                break
            if declared is not None and len(seen) >= declared:
                break
        return parts

    @staticmethod
    def _validate(resp, route: str, ref: Ref) -> None:
        if not isinstance(resp, dict):
            raise UpstreamChanged(f"kuwo: {route} 返回的不是 JSON 对象")
        data = resp.get("data")
        if data in ({}, None) or (isinstance(data, dict) and not data):
            raise FetchError(
                f"kuwo: {route} 返回空 data（歌单不存在，或 id 属于 www.kuwo.cn 命名空间）",
                kind=KIND_NOTFOUND, context={"key": ref.key, "route": route})

    # -- derive --------------------------------------------------------------
    def derive(self, parts: dict, ctx: Ctx):
        info = (parts.get("playlist_info") or {}).get("data") or {}
        declared = clean_int(info.get("musicCount"))
        total_reported = None
        tracks: list[Track] = []
        for name in sorted(n for n in parts if n.startswith("music_list_")):
            data = (parts[name].get("data") or {})
            if total_reported is None:
                total_reported = clean_int(data.get("total"))
            for item in data.get("list") or []:
                if not isinstance(item, dict):
                    continue
                idx = len(tracks)
                if not item.get("name"):
                    tracks.append(Track(index=idx, id=str(item.get("id") or "") or None,
                                        status=TRACK_UNAVAILABLE, reason="接口返回了空曲目占位"))
                    continue
                artists = [Artist(name=a.get("name") or "", id=str(a.get("id")) if a.get("id") else None)
                           for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name")]
                if not artists:
                    artists = [Artist(name=n) for n in split_artists(item.get("artist"))]
                album = item.get("album")
                album_name = album.get("name") if isinstance(album, dict) else album
                album_id = str(album.get("id")) if isinstance(album, dict) and album.get("id") else None
                released = iso_date(item.get("releaseDate"))
                duration_s = clean_int(item.get("duration"))
                tracks.append(Track(
                    index=idx, id=str(item.get("id")) if item.get("id") else None,
                    title=item.get("name"), artists=artists,
                    album=Album(name=album_name, id=album_id, release_date=released),
                    duration_ms=(Field.platform(duration_s * 1000, raw={"sec": duration_s})
                                 if duration_s else Field.unavailable("该曲目没有 duration")),
                    language=Field.platform(item.get("language"), raw=item.get("language")),
                    released_at=(Field.platform(released, raw=item.get("releaseDate"))
                                 if released else self.unavailable("released_at")),
                    collected_at=self.unavailable("collected_at"),
                ))

        warnings = []
        before = len(tracks)
        from .base import dedupe_by_id
        tracks, removed = dedupe_by_id(tracks)
        if removed:
            warnings.append(f"分页去重移除 {removed} 条重复曲目")
        if declared is not None and len(tracks) != declared:
            warnings.append(
                f"歌单声明 {declared} 首（接口 total={total_reported}），实际取到 {len(tracks)} 首")
        if len(tracks) >= MAX_PAGES * PAGE_SIZE:
            warnings.append(f"分页在上限 {MAX_PAGES} 页处停止，可能仍有未取到的曲目")

        meta = PlaylistMeta(
            id=str(info.get("id") or ctx.ref.key), title=info.get("name"),
            creator_name=info.get("creatorName"),
            creator_id=str(info.get("creatorId")) if info.get("creatorId") else None,
            description=info.get("description"), declared_count=declared)
        pl = self.make(ctx, meta, tracks, warnings)
        pl.ledger = build_ledger(tracks, declared, duplicates=removed)
        return pl
