"""酷狗音乐 adapter - requires the KuGouMusicApi community service.

Route facts (verified against the pinned commit):

* playlist identity in this API is the *plaintext* ``collection_3_<uid>_<idx>_0``,
  which ``/top/playlist`` returns as ``global_collection_id``. A numeric
  ``specialid`` or a bare ``gcid_xxx`` string is **not** accepted: the service
  answers ``{"data":{},"status":1}``, an empty success. We detect that and say so
  instead of reporting an empty playlist.
* ``duration`` is always null; the real value is ``timelen`` (milliseconds).
* the historical share-page route (m.kugou.com/songlist/gcid_xxx/) now serves a
  shell whose ``window.$output`` is empty, so a gcid can no longer be converted
  to a plaintext id. `resolve` says exactly that instead of guessing.
"""

from __future__ import annotations

import json
import re
import urllib.request

from ..errors import FetchError, InputError, KIND_NOTFOUND, UpstreamChanged
from ..model import Album, Artist, Field, PlaylistMeta, Track, TRACK_UNAVAILABLE
from ..runtime import HttpClient
from ..textutil import split_artists, split_kugou_name
from ..timeutil import iso_date
from .base import (Adapter, Ctx, Ref, build_ledger, clean_int, dedupe_by_id,
                   epoch_field, optional_text)
from .node_service import NodeService, ServiceSpec

SERVICE = ServiceSpec(name="KuGouMusicApi", repo="MakcRe/KuGouMusicApi",
                      commit="9b6a779abfc9b4590ed671836c82db398fde5929",
                      entry="app.js", health_path="/")
PAGE_SIZE = 300
MAX_PAGES = 10
#: 探测明文 id 时试多少个序号（v1 的实测：默认收藏/喜欢通常是 1~2）
PROBE_RANGE = 5

# The lookbehind matters: without it `global_collection_id=collection_3_...`
# matches the *field name* first („collection_id“ is a substring of
# „global_collection_id“) and resolve() hands back an id no provider has.
_COLLECTION = re.compile(r"(?<![0-9A-Za-z_])collection_[0-9a-zA-Z_]+")
_GCID = re.compile(r"gcid_([0-9a-zA-Z]+)")
_UID = re.compile(r"[?&]uid=(\d+)")
SHARE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
            "weibo (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


class KugouAdapter(Adapter):
    name = "kugou"
    label = "酷狗音乐"
    url_patterns = (r"kugou\.com",)
    reference = "docs/references/kugou.md"
    verified_on = "2026-09-29"
    verified_note = ("collection_3_2132041455_14_0：声明 50 / 实取 50，经由本地 KuGouMusicApi @9b6a779 服务")
    upstream_commit = SERVICE.commit
    default_mode = "node-service"
    requires_node = True
    capabilities = {
        "duration_ms": True,
        "language": True,
        "released_at": True,
        "collected_at": True,
        # 曲目级 extra
        "fileid": True,
        "remark": True,
        "bpm": True,
        # 歌单级 extra
        "playlist.tags": True,
        "playlist.created_at": True,
        "playlist.updated_at": True,
        "playlist.published_at": True,
    }
    field_reasons = {
        "language": "该曲目没有 language 字段",
        "released_at": "该曲目没有 publish_date",
        "collected_at": "该曲目没有 collecttime",
    }

    def __init__(self, *, service_url: str | None = None, service_home=None,
                 allow_provision: bool = True, log=None):
        self.service_url = service_url
        self.service_home = service_home
        self.allow_provision = allow_provision
        self.log = log

    def match(self, raw: str) -> bool:
        return "kugou.com" in raw or raw.startswith("collection_") or "gcid_" in raw

    # -- resolve -------------------------------------------------------------
    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raw = raw.strip()
        m = _COLLECTION.search(raw)
        if m:
            return Ref(provider=self.name, key=m.group(0))
        gcid = _GCID.search(raw)
        if gcid:
            uid = _UID.search(raw)
            found, hint = self._share_lookup(gcid.group(1), uid.group(1) if uid else None)
            if found:
                return Ref(provider=self.name, key=found,
                           params={"resolved_from": raw, "gcid": gcid.group(1)})
            # 明文 id 藏起来了（no_show_gid=1）。v1 的老办法在这里仍然有效，而且它
            # **不是猜 id**：在 collection_3_<uid>_<idx>_0 的 idx 1..5 里逐个试，
            # 只有「歌单名相同 **且** list_create_userid 等于链接里的 uid」才算命中。
            # 序号是歌单在该用户列表里的位置，范围小且可验证，所以搬回来。
            probed_note = ""
            if hint.get("uid") and hint.get("name"):
                probed = self._probe_collection_id(str(hint["uid"]), hint["name"])
                if probed:
                    key, idx = probed
                    return Ref(provider=self.name, key=key,
                               params={"resolved_from": raw, "gcid": gcid.group(1),
                                       "resolved_by": "probe(name+uid)",
                                       "probed_idx": idx})
                probed_note = (f"；也按「歌单名 + uid」试过 collection_3_{hint['uid']}"
                               f"_<idx>_0（idx 1..{PROBE_RANGE}），没有一份的名字与 uid"
                               "同时对得上")
            raise InputError(
                f"kugou: 分享页 m.kugou.com/songlist/gcid_{gcid.group(1)}/ 里没有可用的"
                " collection_ 明文 id —— " + _share_hint(hint) + probed_note +
                "。请在酷狗客户端/网页版歌单的分享链接中找到形如 "
                "collection_3_<uid>_<idx>_0 的 id 后重试（见 docs/references/kugou.md）")
        if raw.isdigit():
            raise InputError(
                "kugou: 这个接口只认明文 collection_3_<uid>_<idx>_0；数字 specialid "
                "会得到空 data（空成功）。可以从 /top/playlist 的 global_collection_id 取得。")
        raise InputError(f"kugou: 无法从输入中解析歌单 id: {raw[:120]}")

    @staticmethod
    def _share_lookup(gcid: str, uid: str | None) -> tuple[str | None, dict]:
        """分享页 -> (明文 collection id 或 None, 诊断信息)。

        2026-10-03 实测（两份真实的 `gcid_*` 分享链接）：页面**仍然**内嵌
        ``window.$output``，里面有 ``info.listinfo``（歌单名、平台声明数、创建者）和
        ``info.songs``（**前 10 首**的完整曲目对象）。所以「分享页返回空壳」这个说法
        只对了一半：真正会空的是 ``global_collection_id`` / ``list_create_gid``——
        当歌单带 ``no_show_gid=1``（用户自己的收藏/喜欢，平台不公开明文 id）时它们就是
        空串，此时 SSR 与服务两条路都走不通，只能让用户换一个 id 形态。
        诊断信息交给调用方拼进报错，别再说「页面是空壳」——那是误报。
        """
        url = f"https://m.kugou.com/songlist/gcid_{gcid}/" + (f"?uid={uid}" if uid else "")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": SHARE_UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception:                                     # noqa: BLE001 - optional route
            return None, {"fetched": False}
        m = _COLLECTION.search(html)
        if m:
            return m.group(0), {"fetched": True, "has_output": True, "uid": uid}
        payload = _share_payload(html)
        if not payload:
            return None, {"fetched": True, "has_output": False}
        info = payload.get("info") or {}
        li = info.get("listinfo") if isinstance(info.get("listinfo"), dict) else {}
        page_uid = li.get("list_create_userid")
        return None, {
            "fetched": True, "has_output": True,
            "name": (li.get("name") or "").strip() or None,
            "count": li.get("count"),
            "creator": (li.get("list_create_username") or "").strip() or None,
            "embedded": len(info.get("songs") or []),
            "hidden": not (payload.get("global_collection_id") or li.get("list_create_gid")),
            # 链接里的 uid 优先（v1 的做法），页面里的兜底
            "uid": uid or (str(page_uid) if page_uid else None),
        }

    # -- collect -------------------------------------------------------------
    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict:
        service = NodeService(SERVICE, home=self.service_home, service_url=self.service_url,
                              allow_provision=self.allow_provision,
                              log=self.log or (lambda m: None))
        with service.ensure() as handle:
            ctx.mode = handle.mode
            ctx.upstream_commit = handle.upstream_commit or SERVICE.commit
            ctx.extra["service"] = {
                "base_url": handle.base_url, "mode": handle.mode,
                "install_method": handle.install_method,
                "resolved_digest": handle.resolved_digest,
                "upstream_commit": handle.upstream_commit}
            parts: dict = {}
            detail = client.get_json(
                "playlist_detail", f"{handle.base_url}/playlist/detail?ids={ref.key}",
                error_hints={404: "该路由不存在"})
            info = self._detail(detail, ref)
            parts["playlist_detail"] = detail
            declared = clean_int(info.get("count"))
            seen: set[str] = set()
            for page in range(1, MAX_PAGES + 1):
                name = f"track_all_{page:02d}"
                resp = client.get_json(
                    name,
                    f"{handle.base_url}/playlist/track/all?id={ref.key}"
                    f"&page={page}&pagesize={PAGE_SIZE}")
                data = resp.get("data") if isinstance(resp, dict) else None
                if isinstance(data, dict):
                    songs = data.get("songs") or []
                elif isinstance(data, list):
                    songs = data
                else:
                    raise UpstreamChanged(f"kugou: {name} 返回结构异常")
                parts[name] = resp
                if not songs:
                    break
                ids = {_song_id(s) for s in songs if isinstance(s, dict)}
                if ids and ids <= seen:
                    break
                seen |= ids
                if declared is not None and len(seen) >= declared:
                    break
            return parts

    @staticmethod
    def _detail(resp, ref: Ref) -> dict:
        if not isinstance(resp, dict):
            raise UpstreamChanged("kugou: playlist/detail 返回的不是 JSON 对象")
        data = resp.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        raise FetchError(
            "kugou: playlist/detail 返回空 data（歌单不存在，或 id 不是 collection_ 明文形式）",
            kind=KIND_NOTFOUND,
            context={"key": ref.key, "status": resp.get("status"),
                     "error_code": resp.get("error_code")})

    # -- derive --------------------------------------------------------------
    def derive(self, parts: dict, ctx: Ctx):
        info = self._detail(parts["playlist_detail"], ctx.ref)
        declared = clean_int(info.get("count"))
        reported = None
        raw_songs: list[dict] = []
        for name in sorted(n for n in parts if n.startswith("track_all_")):
            payload = parts[name]
            data = payload.get("data") if isinstance(payload, dict) else None
            songs = data.get("songs") if isinstance(data, dict) else data
            if reported is None and isinstance(data, dict):
                reported = clean_int(data.get("count"))
            raw_songs.extend(s for s in songs or [] if isinstance(s, dict))

        tracks: list[Track] = []
        for i, song in enumerate(raw_songs):
            audio_id = song.get("mixsongid") or song.get("audio_id")
            tid = str(song.get("hash") or audio_id or "") or None
            raw_name = song.get("name") or song.get("filename") or ""
            title = song.get("songname") or song.get("filename")
            artists = [Artist(name=s.get("name") or "", id=str(s.get("id")) if s.get("id") else None)
                       for s in song.get("singerinfo") or []
                       if isinstance(s, dict) and s.get("name")]
            if not artists and raw_name:
                artists, split_title = split_kugou_name(raw_name)
                title = title or split_title
            if not title and raw_name:
                title = raw_name
            if not title:
                tracks.append(Track(index=i, id=tid, status=TRACK_UNAVAILABLE,
                                    reason="该曲目已被下架（接口只留空位）"))
                continue
            album = song.get("albuminfo") or {}
            published = iso_date(song.get("publish_date"))
            collected_ts = clean_int(song.get("collecttime"))
            timelen = clean_int(song.get("timelen")) or clean_int(song.get("duration"))
            extra = {"fileid": Field.platform(song.get("fileid"))}
            # remark 是平台写在曲目上的说明串（fixture 里如 "《星曜计划》音乐企划"），
            # bpm 是平台自己算的速度值——两个都是「平台当次给的值」且不需要我们解释。
            # tagmap.genre0 是位掩码（524288 这种），要一张映射表才读得懂，不取。
            remark = optional_text(song.get("remark"))
            if remark:
                extra["remark"] = Field.platform(remark, raw=song.get("remark"))
            bpm = clean_int(song.get("bpm"))
            if bpm:
                extra["bpm"] = Field.platform(bpm, raw=song.get("bpm"))
            tracks.append(Track(
                index=i, id=tid, title=title, artists=artists,
                album=Album(name=album.get("name") if isinstance(album, dict) else None,
                            id=str(album.get("id")) if isinstance(album, dict) and album.get("id") else None,
                            release_date=published),
                duration_ms=(Field.platform(timelen, raw={"timelen": song.get("timelen"),
                                                          "duration": song.get("duration")})
                             if timelen else Field.unavailable("该曲目没有 timelen")),
                language=Field.platform(song.get("language"), raw=song.get("language")),
                released_at=(Field.platform(published, raw=song.get("publish_date"))
                             if published else self.unavailable("released_at")),
                collected_at=(Field.platform(iso_date(collected_ts), raw={"collecttime": collected_ts})
                              if collected_ts else self.unavailable("collected_at")),
                extra=extra,
            ))

        before = len(tracks)
        tracks, removed = dedupe_by_id(tracks)
        warnings = []
        if removed:
            warnings.append(f"分页去重移除 {removed} 条重复曲目")
        if declared is not None and len(tracks) != declared:
            warnings.append(
                f"歌单声明 {declared} 首（track/all 报告 count={reported}），"
                f"实际取到 {len(tracks)} 首（原样 {before} 条）")
        if len(tracks) >= MAX_PAGES * PAGE_SIZE:
            warnings.append(f"分页在上限 {MAX_PAGES} 页处停止，可能仍有未取到的曲目")

        meta = PlaylistMeta(
            id=str(info.get("global_collection_id") or ctx.ref.key), title=info.get("name"),
            creator_name=info.get("list_create_username") or info.get("nickname"),
            creator_id=str(info.get("list_create_userid")) if info.get("list_create_userid") else None,
            description=info.get("intro"), declared_count=declared,
            # 歌单标签：musiclib_tags[].tag_name 是平台自己写的字符串列表（"流行/网络/
            # 甜蜜/精选"），同一条 data 里还有一个逗号串 ``tags``，内容是同一批标签，
            # 取结构化的这一份、不取那个串（避免同一信息两个来源互相打架）。
            # collect_total / heat 是社交计数，不取（docs/schema.md 第 11 节）。
            extra={
                "tags": _tags_of(info.get("musiclib_tags")),
                "created_at": epoch_field(info.get("create_time"), unit="sec"),
                "updated_at": epoch_field(info.get("update_time"), unit="sec"),
                "published_at": _date_field(info.get("publish_date")),
            })
        pl = self.make(ctx, meta, tracks, warnings)
        pl.ledger = build_ledger(tracks, declared, duplicates=removed)
        return pl


    def _probe_collection_id(self, uid: str, name: str, *,
                             probe_range: int = PROBE_RANGE) -> tuple[str, int] | None:
        """试 ``collection_3_<uid>_<idx>_0``（idx 1..5），返回 (id, idx) 或 None。

        判据与 v1 一致，两条**同时**满足才算命中：``name`` 完全相同、
        ``list_create_userid`` 等于链接里的 uid。名字单独相同不算数——不同用户完全
        可以给歌单起同一个名字。

        只在分享页没给出明文 id 时才调用（典型：``no_show_gid=1`` 的用户歌单）。
        需要本地 node 服务；服务起不来（没装 node、不允许 provision、端口不通）就
        返回 None，让调用方给出那条「请换 collection_ 链接」的报错，而不是崩。
        **探测请求不进 ``raw/``**：它们是「为了找到 id」的临时请求，不是这份数据的
        证据；走 raw urllib 而不是带记录的 HttpClient，正是为了不破坏
        「记进 raw/ 的必须等于交给 derive 的」这条不变量。
        """
        log = self.log or (lambda m: None)
        try:
            service = NodeService(SERVICE, home=self.service_home,
                                  service_url=self.service_url,
                                  allow_provision=self.allow_provision, log=log)
            with service.ensure() as handle:
                for idx in range(1, probe_range + 1):
                    cid = f"collection_3_{uid}_{idx}_0"
                    log(f"[kugou] 分享页没给明文 id，试探 {cid} …")
                    row = self._service_detail(handle.base_url, cid)
                    if not row:
                        continue
                    if (row.get("name") == name
                            and str(row.get("list_create_userid")) == str(uid)):
                        log(f"[kugou] 命中：{cid}（歌单名与 uid 都对得上）")
                        return cid, idx
        except Exception as exc:                              # noqa: BLE001 - 尽力而为
            log(f"[kugou] 探测明文 id 时服务不可用：{type(exc).__name__}: {exc}")
            return None
        return None

    @staticmethod
    def _service_detail(base_url: str, cid: str) -> dict | None:
        """向本地服务问一份歌单详情（只用于探测 id，不记录）。"""
        url = f"{base_url}/playlist/detail?ids={urllib.parse.quote(cid)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": SHARE_UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:                                     # noqa: BLE001
            return None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return None


def _share_payload(html: str) -> dict | None:
    """从分享页里取出 ``window.$output`` 的 JSON 对象（没有就返回 None）。"""
    i = html.find("window.$output")
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _share_hint(hint: dict) -> str:
    """把 `_share_lookup` 的诊断翻成一句人话，接在报错后面。"""
    if not hint.get("fetched"):
        return "分享页这次没取到（网络失败或被拦截），所以无法判断页面上有什么"
    if not hint.get("has_output"):
        return "页面里没有 window.$output，确实没有内嵌任何歌单数据"
    bits = []
    if hint.get("name"):
        bits.append(f"歌单「{hint['name']}」")
    if hint.get("count") is not None:
        bits.append(f"平台声明 {hint['count']} 首")
    if hint.get("creator"):
        bits.append(f"创建者 {hint['creator']}")
    gotten = "，".join(bits) if bits else "页面内嵌了 window.$output"
    tail = (f"页面只嵌了前 {hint['embedded']} 首" if hint.get("embedded") else "页面上没有内嵌曲目")
    if hint.get("hidden"):
        return (gotten + "，但 global_collection_id 与 list_create_gid 都是空串"
                "（no_show_gid=1：平台把这类歌单的明文 id 藏起来了），" + tail)
    return gotten + f"，但没有可用的 collection_ 明文 id，{tail}"


def _song_id(song: dict) -> str:
    return str(song.get("hash") or song.get("mixsongid") or song.get("audio_id") or "")


def _tags_of(entries) -> Field:
    """``musiclib_tags[]`` -> 平台标签名列表（``Field``）。

    只读平台写在 ``tag_name`` 里的字符串；``tag_id``/``parent_id`` 是平台的分类
    编码，要一张表才读得懂，不进值里（按需可以再从 ``raw`` 取）。
    """
    if not isinstance(entries, list):
        return Field.unavailable("该歌单没有 musiclib_tags")
    names = [t for t in (optional_text((e or {}).get("tag_name"))
                         if isinstance(e, dict) else None for e in entries) if t]
    if not names:
        return Field.unavailable("该歌单没有 musiclib_tags")
    ids = [e.get("tag_id") for e in entries if isinstance(e, dict) and e.get("tag_id")]
    return Field.platform(names, raw={"tag_ids": ids} if ids else None)


def _date_field(value) -> Field:
    """平台已经写成 ``YYYY-MM-DD`` 的日期：值就是它，``raw`` 因相等而被丢掉。"""
    text = optional_text(value)
    return (Field.platform(text) if text
            else Field.unavailable("该歌单没有 publish_date"))
