"""QQ 音乐 adapter (direct HTTP, no third-party service).

Two public endpoints, both without login:

* ``c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg`` -> playlist body
  with the ordered ``songlist``.
* ``u.y.qq.com/cgi-bin/musicu.fcg`` -> album details, batched 30 albummids per
  request. The body must be a *one level* JSON object (``req_0``, ``req_1``...);
  wrapping it in ``{"data": "<json string>"}`` - a common mistake - returns an
  empty object, which is exactly the "empty success" failure mode this project
  exists to catch.

语种 comes from the album, so it is album-level granularity, labelled as such.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from ..errors import FetchError, InputError, KIND_NOTFOUND, UpstreamChanged
from ..model import Album, Artist, Field, PlaylistMeta, Track, TRACK_UNAVAILABLE
from ..runtime import HttpClient
from ..timeutil import iso_date
from .base import (Adapter, Ctx, Ref, build_ledger, clean_int, epoch_field,
                   optional_text)

CD_URL = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
ALBUM_BATCH = 30
ALBUM_METHOD = {"module": "music.musichallAlbum.AlbumInfoServer",
                "method": "GetAlbumDetail"}

_DISS = re.compile(r"[?&]disstid=(\d+)")
_PLAYLIST = re.compile(r"/playlist/(\d+)")


class QQAdapter(Adapter):
    name = "qq"
    #: 这个适配器的数据契约版本（独立演化：改酷狗的解析不必连累 netease）。
    #: 它进 `playlist.json.source.adapter_version`，`--strict` 会核对；
    #: 与包版本（`mpa --version`）是两件事，见 docs/schema.md 第 5.2 节。
    version = "2.0.0"
    label = "QQ 音乐"
    url_patterns = (r"y\.qq\.com", r"c\.y\.qq\.com")
    reference = "docs/references/qqmusic.md"
    verified_on = "2026-09-29"
    verified_note = "公开歌单 7011264340：声明 76 / 实取 76（含专辑详情批量请求）"
    capabilities = {
        "duration_ms": True,
        "language": True,
        "released_at": True,
        "collected_at": False,
        # 曲目级 extra（来自专辑详情，同一专辑内所有曲目相同）
        "album_type": True,
        "label": True,
        # 歌单级 extra
        "playlist.created_at": True,
    }
    field_reasons = {
        "language": "该曲目没有 albummid，拿不到专辑语种（QQ 音乐的语种是专辑级字段）",
        "collected_at": "QQ 音乐接口不返回收藏时间",
        "released_at": "该专辑没有 publishDate",
    }

    digits_are_ambiguous = True

    def match(self, raw: str) -> bool:
        return "y.qq.com" in raw or raw.isdigit()

    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raw = raw.strip()
        if raw.isdigit():
            return Ref(provider=self.name, key=raw)
        for pat in (_DISS, _PLAYLIST):
            m = pat.search(raw)
            if m:
                return Ref(provider=self.name, key=m.group(1))
        raise InputError(f"qq: 无法从输入中解析歌单 id: {raw[:120]}")

    # -- collect -------------------------------------------------------------
    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict:
        url = (f"{CD_URL}?type=1&json=1&utf8=1&onlysong=0&disstid={ref.key}"
               "&begin=0&num=999&loginUin=0&hostUin=0&format=json&inCharset=utf8"
               "&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0")
        resp = client.get_json("playlist_fcg", url, headers={"Referer": "https://y.qq.com/"})
        cd = self._detail(resp, ref)
        parts: dict = {"playlist_fcg": resp}

        mids: list[str] = []
        for song in cd.get("songlist") or []:
            mid = (song.get("album") or {}).get("mid") or song.get("albummid")
            if mid and mid not in mids:
                mids.append(mid)
        for i in range(0, len(mids), ALBUM_BATCH):
            chunk = mids[i:i + ALBUM_BATCH]
            payload: dict = {"comm": {"ct": 19, "cv": 1873, "uin": "0"}}
            for j, mid in enumerate(chunk):
                payload[f"req_{j}"] = {**ALBUM_METHOD, "param": {"albumMid": mid}}
            name = f"album_detail_{i // ALBUM_BATCH:03d}"
            # The response keys are req_0..req_n in request order, so the request
            # body is required to decode it - keep it in `parts` too, not only in
            # raw/, or derive would see fewer albums online than offline.
            parts[f"{name}_req"] = payload
            parts[name] = client.request(
                name, MUSICU_URL, method="POST", data=json.dumps(payload),
                record_body=True,
                headers={"Referer": "https://y.qq.com/",
                         "Content-Type": "application/x-www-form-urlencoded"})
        return parts

    @staticmethod
    def _detail(resp, ref: Ref) -> dict:
        if not isinstance(resp, dict):
            raise UpstreamChanged("qq: playlist 接口返回的不是 JSON 对象")
        cdlist = resp.get("cdlist") or []
        if not cdlist:
            code = resp.get("code")
            if code in (-1, 10, 1000) or code is None:
                raise FetchError("qq: 歌单不存在、未公开或需要登录",
                                 kind=KIND_NOTFOUND,
                                 context={"key": ref.key, "code": code,
                                          "subcode": resp.get("subcode")})
            raise UpstreamChanged(f"qq: playlist 返回结构异常 (code={code})",
                                  context={"code": code})
        return cdlist[0]

    # -- derive --------------------------------------------------------------
    def derive(self, parts: dict, ctx: Ctx):
        cd = self._detail(parts["playlist_fcg"], ctx.ref)
        songs = cd.get("songlist") or []
        # 不去回退成 len(songs)：那会把「平台没声明总数」伪装成「声明数=取到数」。
        declared = clean_int(cd.get("total_song_num")) or clean_int(cd.get("songnum"))

        albums: dict[str, dict] = {}
        album_misses = 0
        for name, payload in sorted(parts.items()):
            if not name.startswith("album_detail_") or name.endswith("_req"):
                continue
            mids = _request_mids(parts.get(f"{name}_req"))
            if not isinstance(payload, dict):
                album_misses += len(mids)
                continue
            for j, mid in enumerate(mids):
                node = payload.get(f"req_{j}") or {}
                data = node.get("data") or {}
                info = data.get("basicInfo") or None
                if info:
                    # 整份 data 留着：basicInfo 之外还有 company（厂牌），
                    # 那也是平台当次给的值，见下面的 extra。
                    albums[mid] = data
                else:
                    album_misses += 1

        tracks: list[Track] = []
        for i, song in enumerate(songs):
            if not isinstance(song, dict):
                continue
            mid = (song.get("album") or {}).get("mid") or song.get("albummid")
            data = albums.get(mid) or {}
            info = data.get("basicInfo") or {}
            company = data.get("company") or {}
            interval = clean_int(song.get("interval"))
            released = iso_date(info.get("publishDate"))
            album_name = (song.get("album") or {}).get("name") or song.get("albumname")
            if not song.get("songname"):
                tracks.append(Track(index=i, id=str(song.get("songmid") or "") or None,
                                    status=TRACK_UNAVAILABLE, reason="接口返回了空曲目占位"))
                continue
            extra = {}
            # 专辑类型（Single / EP / 录音室专辑）与厂牌名：都是专辑详情里
            # 平台自己写的字符串，读出来即可，不归类也不缩写。
            album_type = optional_text(info.get("albumType"))
            if album_type:
                extra["album_type"] = Field.platform(album_type, raw=info.get("albumType"))
            label = optional_text(company.get("name"))
            if label:
                extra["label"] = Field.platform(label, raw=company.get("name"))
            tracks.append(Track(
                index=i, id=song.get("songmid") or song.get("songid"),
                title=song.get("songname"),
                artists=[Artist(name=s.get("name") or "", id=str(s.get("mid")) if s.get("mid") else None)
                         for s in song.get("singer") or [] if isinstance(s, dict) and s.get("name")]
                        or [Artist(name="<QQ 音乐未返回歌手>")],
                album=Album(name=album_name, id=mid, release_date=released),
                duration_ms=(Field.platform(interval * 1000, raw={"sec": interval})
                             if interval else Field.unavailable("该曲目没有 interval")),
                language=(Field.platform(info.get("language"), raw={"granularity": "album"})
                          if info.get("language") else self.unavailable("language")),
                released_at=(Field.platform(released, raw={"publishDate": info.get("publishDate"),
                                                           "granularity": "album"})
                             if released else self.unavailable("released_at")),
                collected_at=self.unavailable("collected_at"),
                extra=extra,
            ))

        warnings = []
        album_parts = [n for n in parts
                       if n.startswith("album_detail_") and not n.endswith("_req")]
        if album_parts and not albums:
            warnings.append(
                "取到了专辑详情请求但一张都没解析出来：请求体可能无法离线复算，"
                "相关语种/发行日期只能标为 unavailable（这是数据缺陷，不是平台没有）")
        if album_misses:
            warnings.append(f"{album_misses} 张专辑未取到详情，相关曲目的语种/发行日期为 unavailable")
        if declared != len(songs):
            warnings.append(f"歌单声明 {declared} 首，接口返回 songlist {len(songs)} 首")
        langs = Counter(t.language.value for t in tracks if t.language.known)
        if langs:
            warnings.append("语种为专辑级字段，同一专辑内所有曲目相同：" +
                            "、".join(f"{k} {v}" for k, v in langs.most_common(4)))
        meta = PlaylistMeta(
            id=str(cd.get("disstid") or ctx.ref.key), title=cd.get("dissname"),
            creator_name=cd.get("nickname"),
            creator_id=str(cd.get("uin")) if cd.get("uin") else None,
            description=cd.get("desc"), declared_count=declared,
            # ctime 是秒 epoch；visitnum/cmtnum（访问/评论数）是社交计数，
            # mtime 与 song_update_time 在这份记录里平台给的就是 0，都不取。
            extra={"created_at": epoch_field(cd.get("ctime"), unit="sec")})
        pl = self.make(ctx, meta, tracks, warnings)
        pl.ledger = build_ledger(tracks, declared)
        return pl


def _request_mids(payload) -> list[str]:
    """Recover the albummids of one musicu batch from the recorded request body.

    Tolerant on purpose: the store keeps the body as JSON when it can and as the
    verbatim string when it cannot, and either shape must decode here - a body we
    cannot parse silently means "no album language/date", which is exactly the
    kind of quiet degradation this project exists to avoid.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if isinstance(payload, dict) and isinstance(payload.get("body"), str):
        try:
            payload = json.loads(payload["body"])
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    mids: list[str] = []
    j = 0
    while f"req_{j}" in payload:
        mid = ((payload[f"req_{j}"] or {}).get("param") or {}).get("albumMid")
        if mid:
            mids.append(mid)
        j += 1
    return mids
