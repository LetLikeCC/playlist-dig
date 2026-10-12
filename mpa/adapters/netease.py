"""网易云音乐 adapter (direct HTTP, no third-party service).

Route: POST /api/v3/playlist/detail  -> ordered trackIds (+ creator, count)
       GET  /api/song/detail?ids=[..] -> per-song name/artists/album/duration

The order of a playlist is the order of ``trackIds``, not of ``tracks``; the old
script sorted by popularity in the analysis step, which destroyed the ordering
information. Here the order is preserved and the popularity number is kept as an
optional extra field.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..errors import FetchError, KIND_NOTFOUND, KIND_PRIVATE, UpstreamChanged
from ..model import Artist, Album, Field, PlaylistMeta, Track, TRACK_UNAVAILABLE
from ..runtime import HttpClient
from ..timeutil import iso_date
from .base import (Adapter, Ctx, Ref, build_ledger, clean_int, epoch_field,
                   optional_text, strings_of)

DETAIL_URL = "https://music.163.com/api/v3/playlist/detail"
SONG_URL = "https://music.163.com/api/song/detail"
SONG_CHUNK = 100

_ID_PATTERNS = (
    re.compile(r"[?&#]id=(\d+)"),
    re.compile(r"/playlist/(\d+)"),
    re.compile(r"/discover/toplist\?id=(\d+)"),
)


class NeteaseAdapter(Adapter):
    name = "netease"
    #: 这个适配器的数据契约版本（独立演化：改酷狗的解析不必连累 netease）。
    #: 它进 `playlist.json.source.adapter_version`，`--strict` 会核对；
    #: 与包版本（`mpa --version`）是两件事，见 docs/schema.md 第 5.2 节。
    version = "2.0.0"
    label = "网易云音乐"
    url_patterns = (r"music\.163\.com",)
    reference = "docs/references/netease.md"
    verified_on = "2026-09-29"
    verified_note = "网易云热歌榜 3778678：声明 200 / 实取 200"
    default_mode = "direct-http"
    capabilities = {
        "duration_ms": True,
        "language": False,
        "released_at": True,
        "collected_at": False,
        # 曲目级 extra（进 Track.extra，见 docs/schema.md 第 3.2 节）
        "popularity": True,
        "alias": True,
        "trans_name": True,
        # 歌单级 extra（进 playlist.extra，前缀标明作用域，见第 3.3 节）
        "playlist.created_at": True,
        "playlist.updated_at": True,
        "playlist.track_updated_at": True,
    }
    field_reasons = {
        "language": "网易云不返回语种字段（旧版按歌名猜语言属于启发式，不属于数据）",
        "collected_at": "网易云不返回收藏时间",
        "released_at": "该曲目在 song/detail 中没有 publishTime（网易云对部分曲目省略）",
    }

    # -- resolve -------------------------------------------------------------
    digits_are_ambiguous = True

    def match(self, raw: str) -> bool:
        return "music.163.com" in raw or raw.isdigit()

    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raw = raw.strip()
        if raw.isdigit():
            return Ref(provider=self.name, key=raw)
        for pat in _ID_PATTERNS:
            m = pat.search(raw)
            if m:
                return Ref(provider=self.name, key=m.group(1))
        from ..errors import InputError
        raise InputError(f"netease: 无法从输入中解析歌单 id: {raw[:120]}")

    # -- collect -------------------------------------------------------------
    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict:
        detail = client.post_json(
            "playlist_detail_v3", DETAIL_URL, data={"id": ref.key, "n": "1000"},
            headers={"Referer": "https://music.163.com/"})
        self._validate_detail(detail)
        playlist = detail["playlist"]
        ids = [clean_int(t.get("id")) for t in playlist.get("trackIds") or []
               if isinstance(t, dict)]
        ids = [i for i in ids if i]
        if not ids:  # some responses only carry the (truncated) tracks array
            ids = [clean_int(t.get("id")) for t in playlist.get("tracks") or []
                   if isinstance(t, dict)]
            ids = [i for i in ids if i]
        if not ids:
            raise UpstreamChanged(
                "netease: playlist 中没有 trackIds - 接口结构可能已变化",
                context={"key": ref.key})
        parts: dict = {"playlist_detail_v3": detail}
        for i in range(0, len(ids), SONG_CHUNK):
            chunk = ids[i:i + SONG_CHUNK]
            url = SONG_URL + "?ids=" + urllib.parse.quote(json.dumps(chunk))
            resp = client.get_json(f"song_detail_{i // SONG_CHUNK:03d}", url,
                                   headers={"Referer": "https://music.163.com/"})
            if not isinstance(resp, dict) or "songs" not in resp:
                raise UpstreamChanged(
                    f"netease: song/detail 返回结构异常 (chunk {i // SONG_CHUNK})",
                    context={"url": url})
            parts[f"song_detail_{i // SONG_CHUNK:03d}"] = resp
        return parts

    @staticmethod
    def _validate_detail(detail) -> None:
        if not isinstance(detail, dict):
            raise UpstreamChanged("netease: playlist/detail 返回的不是 JSON 对象")
        if detail.get("code") in (401, 403):
            raise FetchError("netease: 歌单不可读（需要登录或已被限制）",
                             kind=KIND_PRIVATE, context={"code": detail.get("code")})
        if detail.get("code") not in (200, None) or not isinstance(detail.get("playlist"), dict):
            code = detail.get("code")
            if code in (404, -1):
                raise FetchError("netease: 歌单不存在", kind=KIND_NOTFOUND,
                                 context={"code": code})
            raise UpstreamChanged(f"netease: playlist/detail 结构异常 (code={code})",
                                  context={"code": code})

    # -- derive (pure) -------------------------------------------------------
    def derive(self, parts: dict, ctx: Ctx):
        playlist = parts["playlist_detail_v3"]["playlist"]
        ids = [clean_int(t.get("id")) for t in playlist.get("trackIds") or [] if isinstance(t, dict)]
        ids = [i for i in ids if i]
        if not ids:
            ids = [clean_int(t.get("id")) for t in playlist.get("tracks") or []
                   if isinstance(t, dict)]
            ids = [i for i in ids if i]
        songs: dict[int, dict] = {}
        for name, payload in parts.items():
            if name.startswith("song_detail_"):
                for s in payload.get("songs") or []:
                    sid = clean_int(s.get("id"))
                    if sid:
                        songs[sid] = s
        banned = {clean_int(b) for b in playlist.get("bannedTrackIds") or []}
        # 不写 `or len(ids)`：那样等于把「平台没声明」悄悄变成「声明数=取到数」，
        # 完整性就永远看不出问题。缺 trackCount 时 declared 保持 None。
        declared = clean_int(playlist.get("trackCount"))

        tracks: list[Track] = []
        for i, sid in enumerate(ids):
            song = songs.get(sid)
            if not song:
                reason = ("已被下架或屏蔽（bannedTrackIds）" if sid in banned
                          else "playlist 声明了该曲目，但 song/detail 没有返回")
                tracks.append(Track(index=i, id=str(sid), status=TRACK_UNAVAILABLE,
                                    reason=reason))
                continue
            album = song.get("album") or {}
            publish = album.get("publishTime") or song.get("publishTime")
            artists = [Artist(name=a.get("name") or "", id=str(a.get("id")) if a.get("id") else None)
                       for a in song.get("artists") or [] if a.get("name")]
            extra = {"popularity": Field.platform(song.get("popularity"))}
            # 别名（"求你别离开我"）与译名：平台写在 song/detail 里的字符串，
            # 只是读出来，不做拆分也不做翻译。
            alias = strings_of(song.get("alias"))
            if alias:
                extra["alias"] = Field.platform(alias, raw=song.get("alias"))
            trans = optional_text(song.get("transName"))
            if trans:
                extra["trans_name"] = Field.platform(trans, raw=song.get("transName"))
            tracks.append(Track(
                index=i, id=str(sid), title=song.get("name"),
                artists=artists or [Artist(name="<网易云未返回歌手>")],
                album=Album(name=album.get("name"), id=str(album.get("id")) if album.get("id") else None,
                            release_date=iso_date(publish)),
                duration_ms=Field.platform(clean_int(song.get("duration")),
                                           raw=song.get("duration"),
                                           reason="该曲目没有 duration"),
                language=self.unavailable("language"),
                released_at=(Field.platform(iso_date(publish), raw=publish)
                             if iso_date(publish) else self.unavailable("released_at")),
                collected_at=self.unavailable("collected_at"),
                extra=extra,
            ))
        warnings = []
        if declared != len(ids):
            warnings.append(
                f"歌单声明 {declared} 首，接口只返回 {len(ids)} 条 trackIds（差额见 ledger.gap）")
        if len(banned) :
            warnings.append(f"接口标记 {len(banned)} 首为下架/屏蔽（bannedTrackIds）")
        meta = PlaylistMeta(
            id=str(playlist.get("id") or ctx.ref.key),
            # /api/v3/playlist/detail calls it "name"; "title" never exists there.
            title=playlist.get("name") or playlist.get("title"),
            creator_name=(playlist.get("creator") or {}).get("nickname"),
            creator_id=str((playlist.get("creator") or {}).get("userId")
                           or (playlist.get("userId") or "")) or None,
            description=playlist.get("description"), declared_count=declared,
            # 歌单级时间戳：平台给的是毫秒 epoch（createTime/updateTime/
            # trackUpdateTime），值是 UTC ISO，raw 留原数。
            # 播放/收藏/评论/分享数刻意不取——那是社交计数不是歌单属性，
            # 每次请求都在动，拿了只会让两次抓取的结果白白抖动（见 docs/schema.md 第 11 节）。
            extra={
                "created_at": epoch_field(playlist.get("createTime"), unit="ms"),
                "updated_at": epoch_field(playlist.get("updateTime"), unit="ms"),
                "track_updated_at": epoch_field(playlist.get("trackUpdateTime"), unit="ms"),
            })
        pl = self.make(ctx, meta, tracks, warnings)
        pl.ledger = build_ledger(tracks, declared)
        return pl
