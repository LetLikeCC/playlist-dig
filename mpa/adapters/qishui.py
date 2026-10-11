"""汽水音乐 (抖音) adapter.

Two routes, tried in order:

1. **SSR** - the share page embeds up to ~175 medias in ``_ROUTER_DATA``. Pure
   HTTP, no Node, but silently truncated beyond that: the page ignores cursor
   parameters and issues no XHR when scrolled.
2. **qishui-api** (community Node service) - one request returns everything,
   including ``next_cursor``. Used only when the declared track count exceeds
   what SSR gave us.

Because route 1 can return "success" with less than the declared count, the
ledger records the gap and the exit code becomes 1 (partial) unless the caller
accepts it. Silent truncation was the original script's worst failure: a 390-track
playlist was reported as if 175 were the whole list.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..errors import (FetchError, InputError, KIND_NETWORK, KIND_NOTFOUND,
                      RouteUnavailable, UpstreamChanged)
from ..model import Album, Artist, Field, PlaylistMeta, Track, TRACK_UNAVAILABLE
from ..runtime import HttpClient
from ..timeutil import iso_date
from .base import (Adapter, Ctx, Ref, build_ledger, clean_int,
                   epoch_field, names_of)
from .node_service import NodeService, ServiceSpec

SERVICE = ServiceSpec(name="qishui-api", repo="guowenye/qishui-api",
                      commit="e409c10fe7441a10d370da7da61d62bbc1e5126c",
                      entry="app.js", health_path="/api/capabilities")

ALLOWED = re.compile(r"^https?://(qishui\.douyin\.com|music\.douyin\.com)/", re.I)
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
_SHARE = re.compile(r"https?://music\.douyin\.com/qishui/share/playlist\?[^\s]*"
                    r"playlist_id=(\d+)")
_PID = re.compile(r"[?&]playlist_id=(\d+)")
_SHORT = re.compile(r"qishui\.douyin\.com/s/([A-Za-z0-9_-]+)")
SSR_LIMIT = 175


class QishuiAdapter(Adapter):
    name = "qishui"
    default_mode = "ssr-partial"
    requires_node = False  # SSR first; the node service is only needed for long lists
    label = "汽水音乐"
    url_patterns = (r"qishui\.douyin\.com", r"music\.douyin\.com")
    reference = "docs/references/qishui.md"
    verified_on = "2026-09-29"
    verified_note = ("分享页 playlist_id=7397692920558452788：声明 65 / 实取 65，SSR 一路即可，未触发 node 服务")
    upstream_commit = SERVICE.commit
    capabilities = {
        "duration_ms": True,
        "language": True,
        "released_at": True,
        "collected_at": False,
        # 平台专属字段（进 Track.extra，见 docs/schema.md 第 3.2 节）
        "genre": True,
        "subgenre": True,
        "composers": True,
        "lyricists": True,
        # 歌单级 extra（进 playlist.extra）
        "playlist.created_at": True,
        "playlist.updated_at": True,
    }
    field_reasons = {
        "language": "该曲目没有 lang_codes",
        "collected_at": "汽水接口不返回收藏时间",
        "released_at": "该曲目没有 release_date",
    }

    def __init__(self, *, service_url: str | None = None, service_home=None,
                 allow_provision: bool = True, partial_ok: bool = False, log=None):
        self.service_url = service_url
        self.service_home = service_home
        self.allow_provision = allow_provision
        self.partial_ok = partial_ok
        self.log = log

    digits_are_ambiguous = True

    def match(self, raw: str) -> bool:
        return "douyin.com" in raw or raw.isdigit()

    # -- resolve -------------------------------------------------------------
    def resolve(self, raw: str, client: HttpClient | None) -> Ref:
        raw = raw.strip()
        if raw.isdigit():
            return Ref(provider=self.name, key=raw)
        m = _PID.search(raw) or _SHARE.search(raw)
        if m:
            return Ref(provider=self.name, key=m.group(1))
        short = _SHORT.search(raw)
        if short:
            target = raw if raw.startswith("http") else f"https://qishui.douyin.com/s/{short.group(1)}/"
            if not ALLOWED.match(target):
                raise InputError(f"qishui: 拒绝请求非抖音域名（SSRF 防护）: {target[:90]}")
            final = self._follow(target)
            m = _PID.search(final)
            if not m:
                raise FetchError("qishui: 短链没有跳转到歌单页", kind=KIND_NOTFOUND,
                                 context={"url": target, "final": final})
            return Ref(provider=self.name, key=m.group(1),
                       params={"resolved_from": target, "final": final})
        raise InputError(f"qishui: 无法从输入中解析 playlist_id: {raw[:120]}")

    @staticmethod
    def _follow(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as exc:
            raise FetchError(f"qishui: 短链解析失败 HTTP {exc.code}",
                             kind=KIND_NOTFOUND, context={"url": url}) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FetchError(f"qishui: 短链解析网络错误 {exc}",
                             kind=KIND_NETWORK, context={"url": url}) from exc

    # -- collect -------------------------------------------------------------
    def collect(self, client: HttpClient, ref: Ref, ctx: Ctx) -> dict:
        share = (f"https://music.douyin.com/qishui/share/playlist?playlist_id={ref.key}")
        html = client.get_text("ssr_page", share, headers={"User-Agent": MOBILE_UA})
        info, medias = parse_ssr(html)
        if info is None:
            raise UpstreamChanged("qishui: SSR 页面里找不到 _ROUTER_DATA / OUTER_DATA - 结构可能已变化",
                                  context={"url": share})
        declared = clean_int(info.get("count_tracks"))
        ctx_mode = "ssr-partial"
        parts: dict = {"ssr_page": html}
        if declared is not None and declared > len(medias):
            service = NodeService(SERVICE, home=self.service_home, service_url=self.service_url,
                                  allow_provision=self.allow_provision,
                                  log=self.log or (lambda m: None))
            try:
                with service.ensure() as handle:
                    payload = client.get_json(
                        "service_playlist_detail",
                        f"{handle.base_url}/playlist/detail"
                        f"?playlist_id={urllib.parse.quote(ref.key)}",
                        headers={"Accept": "application/json"},
                        error_hints={404: "该路由不存在"})
                    if not isinstance(payload, dict) or not (payload.get("data") or {}).get("playlist"):
                        raise UpstreamChanged("qishui: service /playlist/detail 返回结构异常")
                    parts["service_playlist_detail"] = payload
                    ctx_mode = handle.mode
                    ctx.mode = ctx_mode
                    ctx.upstream_commit = handle.upstream_commit or SERVICE.commit
                    ctx.extra["service"] = {
                        "base_url": handle.base_url, "mode": handle.mode,
                        "install_method": handle.install_method,
                        "resolved_digest": handle.resolved_digest,
                        "upstream_commit": handle.upstream_commit}
            except RouteUnavailable as exc:
                if not self.partial_ok:
                    raise RouteUnavailable(
                        f"qishui: 歌单声明 {declared} 首，SSR 只提供 {len(medias)} 首，"
                        f"需要 qishui-api 服务才能拿全（{exc}）") from exc
                ctx.extra["service_error"] = str(exc)
        ctx.mode = ctx_mode
        return parts

    # -- derive --------------------------------------------------------------
    def derive(self, parts: dict, ctx: Ctx):
        warnings: list[str] = []
        service = parts.get("service_playlist_detail")
        if service:
            meta, tracks, declared = self._from_service(service, warnings)
        else:
            meta, tracks, declared = self._from_ssr(parts, ctx, warnings)
        if ctx.extra.get("service_error"):
            warnings.append(f"未能使用 qishui-api 服务：{ctx.extra['service_error']}")
        if declared is not None and len(tracks) < declared:
            warnings.append(
                f"歌单声明 {declared} 首，只取到 {len(tracks)} 首"
                + ("（SSR 页面上限约 %d 首）" % SSR_LIMIT if not service else ""))
        meta.declared_count = declared
        pl = self.make(ctx, meta, tracks, warnings)
        pl.ledger = build_ledger(tracks, declared)
        return pl

    @staticmethod
    def _creator(info: dict) -> tuple[str | None, str | None]:
        """(昵称, id)，取不到就是 (None, None)。

        实测 SSR 的 ``playlistInfo`` 里是 ``owner{nickname,id}``；``desc`` 才是简介
        字段（没有 ``description``）。取不到时宁可留空，也不猜。
        """
        owner = info.get("owner") if isinstance(info.get("owner"), dict) else None
        if not owner:
            brief = ((info.get("user_artist_info") or {})
                     if isinstance(info.get("user_artist_info"), dict) else {})
            owner = brief.get("user_brief") if isinstance(brief.get("user_brief"), dict) else {}
        name = owner.get("nickname") or owner.get("name")
        cid = owner.get("id") or owner.get("uid")
        return (name or None, str(cid) if cid else None)

    @staticmethod
    def _description(info: dict) -> str | None:
        return info.get("desc") or info.get("description") or None

    def _from_ssr(self, parts: dict, ctx: Ctx, warnings: list[str]):
        info, medias = parse_ssr(parts.get("ssr_page") or "")
        if info is None:
            raise UpstreamChanged("qishui: 原始响应里找不到 _ROUTER_DATA / OUTER_DATA - 无法复算")
        declared = clean_int(info.get("count_tracks"))
        if declared and len(medias) < declared:
            warnings.append(f"SSR 页面只嵌入 {len(medias)} 首（声明 {declared} 首）")
        tracks: list[Track] = []
        i = 0
        for media in medias:
            track = ((media or {}).get("entity") or {}).get("track") or {}
            if not track or not track.get("name"):
                tracks.append(Track(index=i, status=TRACK_UNAVAILABLE,
                                    reason="该条位已被下架（SSR 只留空占位）"))
                i += 1
                continue
            tracks.append(self._track(i, track))
            i += 1
        creator_name, creator_id = self._creator(info)
        meta = PlaylistMeta(id=str(info.get("id") or ctx.ref.key), title=info.get("title"),
                           creator_name=creator_name, creator_id=creator_id,
                           description=self._description(info), declared_count=declared,
                           extra=self._times(info))
        return meta, tracks, declared

    def _from_service(self, payload: dict, warnings: list[str]):
        data = payload.get("data") or {}
        playlist = data.get("playlist") or {}
        declared = clean_int(playlist.get("count_tracks"))
        resources = data.get("media_resources") or []
        if data.get("next_cursor"):
            warnings.append(f"服务仍在翻页（next_cursor={data['next_cursor']}），结果可能不完整")
        tracks: list[Track] = []
        i = 0
        for res in resources:
            track = ((res or {}).get("entity") or {}).get("track") or {}
            if not track or not track.get("name"):
                tracks.append(Track(index=i, status=TRACK_UNAVAILABLE,
                                    reason="该条位已被下架（接口返回空 track）"))
                i += 1
                continue
            tracks.append(self._track(i, track))
            i += 1
        creator_name, creator_id = self._creator(playlist)
        meta = PlaylistMeta(id=str(playlist.get("id") or ""), title=playlist.get("title"),
                           creator_name=creator_name, creator_id=creator_id,
                           description=self._description(playlist),
                           declared_count=declared,
                           extra=self._times(playlist))
        return meta, tracks, declared

    @staticmethod
    def _times(info: dict) -> dict[str, Field]:
        """歌单级的创建/更新时间（平台给的是秒 epoch）。

        ``stats``（收藏/评论/分享数）与 ``resource_cnt`` 刻意不取：前者每次请求
        都在动、后者与真声明数冲突（见 docs/schema.md 第 11.5 节）。
        """
        return {"created_at": epoch_field(info.get("create_time"), unit="sec"),
                "updated_at": epoch_field(info.get("update_time"), unit="sec")}

    def _track(self, index: int, track: dict) -> Track:
        album = track.get("album") or {}
        released = iso_date(album.get("release_date"))
        duration = clean_int(track.get("duration"))
        return Track(
            index=index, id=str(track.get("id")) if track.get("id") else None,
            title=track.get("name"),
            artists=[Artist(name=a.get("name") or "",
                            id=str(a["id"]) if a.get("id") else None)
                     for a in track.get("artists") or []
                     if isinstance(a, dict) and a.get("name")]
                    or [Artist(name="<汽水未返回歌手>")],
            album=Album(name=album.get("name"), id=str(album.get("id")) if album.get("id") else None,
                        release_date=released),
            duration_ms=(Field.platform(duration, raw={"ms": duration})
                         if duration else Field.unavailable("该曲目没有 duration")),
            language=self._language(track),
            released_at=(Field.platform(released, raw=album.get("release_date"))
                         if released else self.unavailable("released_at")),
            collected_at=self.unavailable("collected_at"),
            extra={**self._genres(track), **self._credits(track)},
        )

    def _credits(self, track: dict) -> dict[str, Field]:
        """``track.song_maker_team`` -> ``composers`` / ``lyricists``（平台署名）。

        平台给的是 ``{"composers": [{"name": …}], "lyricists": [{"name": …}]}``；
        我们只把 ``name`` 挑出来（和 ``Track.artists`` 取 ``artists[].name`` 一样，
        是读字段，不是归类）。它记的是**平台这次写的署名**，不是「作者是谁」的权威
        答案——缺键就是没给，不会用空列表顶替。
        """
        team = track.get("song_maker_team")
        if not isinstance(team, dict):
            return {}
        out: dict[str, Field] = {}
        for key in ("composers", "lyricists"):
            names = names_of(team.get(key))
            if names:
                out[key] = Field.platform(names, raw=team.get(key))
        return out

    def _genres(self, track: dict) -> dict[str, Field]:
        """``track.tags`` 里 ``category.tag_name == "Genre"`` 的标签 → ``extra``。

        平台每条曲目给 1~2 个 Genre 标签：粗的一个只带 ``first_level_tag``（``"Pop"``），
        细的一个再带 ``second_level_tag``（``"Chinese Pop"``）。fixture 里 65/65 都有，
        旧代码把整段 ``tags`` 丢掉，于是「曲风」这个歌单分析里最常用的字段一个都没有。
        这里不做归类、不做映射，只把平台的两级标签各放一个键：``genre`` = 一级、
        ``subgenre`` = 二级（没有二级就不出这个键），标签 id 留在 ``raw`` 里以便回查。
        标签名两侧的空白会被去掉（和 ``_language`` 丢掉空白 code 一致）；去掉之后是空的
        就不出这个键，绝不把空白当成平台值。若 ``category`` 或某一级标签不再是对象，
        抛 ``UpstreamChanged``——形状对不上时猜一个值比报错更糟。
        """
        out: dict[str, Field] = {}
        for tag in track.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            category = tag.get("category")
            if not category:
                continue
            if not isinstance(category, dict):
                # 以前这里会 AttributeError 逃出去，一个上游改字段就变成 traceback。
                # 形状对不上就明说是上游变了：猜一个「这大概就是标签名」才是更坏的做法。
                raise UpstreamChanged(
                    "qishui: track.tags[].category 不再是对象"
                    f"（是 {type(category).__name__}）- 上游结构可能已变化",
                    context={"field": "track.tags[].category"})
            if str(category.get("tag_name") or "").strip().lower() != "genre":
                continue
            pairs = (("genre", "first_level_tag"), ("subgenre", "second_level_tag"))
            for key, level in pairs:
                node = tag.get(level)
                if not node:
                    continue
                if not isinstance(node, dict):
                    raise UpstreamChanged(
                        f"qishui: track.tags[].{level} 不再是对象"
                        f"（是 {type(node).__name__}）- 上游结构可能已变化",
                        context={"field": f"track.tags[].{level}"})
                name = str(node.get("tag_name") or "").strip()
                if key in out or not name:
                    continue
                raw = {"tag_id": str(node["tag_id"])} if node.get("tag_id") else None
                out[key] = Field.platform(name, raw=raw)
            if len(out) == len(pairs):
                break
        return out

    def _language(self, track: dict) -> Field:
        """``track.lang_codes`` 是平台给的语种代码，原样透传，不做代码→显示名映射。

        覆盖率是 65/65（64 个 ``ZH``、1 个 ``EN``）：这里曾经写死 unavailable，把平台
        明明给了的字段丢掉——和同一文件里丢掉 ``desc``/``owner`` 是同一类错误。取值保持
        平台形态（``"ZH"``、多语种用 ``+`` 连接），原始数组留在 ``raw`` 里，这样「语种」
        字段既不靠歌名猜，也不用本工具的语言表去解释。
        """
        codes = track.get("lang_codes")
        if isinstance(codes, str):
            codes = [codes]
        values = [str(c).strip() for c in codes or [] if str(c).strip()]
        if not values:
            return self.unavailable("language")
        return Field.platform("+".join(values), raw={"lang_codes": codes})


SSR_MARKERS = ("_ROUTER_DATA", "OUTER_DATA")


def _find_marker(html: str) -> int:
    """Locate the SSR payload marker.

    实测页面里的变量名是 ``_ROUTER_DATA``，而 ``OUTER_DATA`` 只是它的子串——过去
    靠这个巧合命中。这里先找真实名，再退回旧名，免得哪天变量改名后 substring 不再
    命中（或者命中到别处）而无声地换个解析起点。
    """
    hits = [(html.find(m), m) for m in SSR_MARKERS]
    hits = [(i, m) for i, m in hits if i >= 0]
    if not hits:
        return -1
    return min(hits)[0]


def parse_ssr(html: str):
    """Pull ``loaderData.playlist_page`` out of the share page's SSR payload."""
    i = _find_marker(html)
    if i < 0:
        return None, []
    start = html.find("{", i)
    if start < 0:
        return None, []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start:])
    except json.JSONDecodeError:
        return None, []
    page = (data.get("loaderData") or {}).get("playlist_page") or {}
    return page.get("playlistInfo"), page.get("medias") or []
