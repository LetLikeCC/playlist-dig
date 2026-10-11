"""Which adapter claims which input, and what ``resolve`` turns it into.

Every test here is offline: the two adapters that can make a request *while
resolving* (kugou's gcid share lookup, qishui's short link) get a patched
``urlopen``, so the SSRF guard, the redirect handling and the "no id in the page"
path are all exercised without a socket.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

from mpa.adapters import (KugouAdapter, KuwoAdapter, NeteaseAdapter,  # noqa: E402
                          QQAdapter, QishuiAdapter, adapter_by_name, all_adapters,
                          find_adapter)
from mpa.adapters.base import Adapter, Ref  # noqa: E402
from mpa.errors import (KIND_NETWORK, KIND_NOTFOUND, InputError,  # noqa: E402
                        RouteUnavailable)

NETEASE_LINKS = (
    "https://music.163.com/#/playlist?id=3778678",
    "https://music.163.com/playlist/3778678",
    "http://music.163.com/discover/toplist?id=3778678",
)


class TestFindAdapter(unittest.TestCase):
    def test_the_recorded_inputs_pick_their_own_adapter(self):
        """A recorded run's input must be claimed by the adapter that ran it.

        Three of the four fixtures were fetched from a bare numeric id, which
        `find_adapter` deliberately refuses to attribute - that is the ambiguity
        contract, so those are checked against `adapter_by_name` instead.
        """
        for provider in S.PROVIDERS:
            raw = S.EXPECTED[provider]["input"]
            with self.subTest(provider=provider, raw=raw):
                adapter = adapter_by_name(provider)
                self.assertTrue(adapter.match(raw))
                if raw.strip().isdigit():
                    with self.assertRaises(InputError):
                        find_adapter(raw)
                else:
                    self.assertEqual(find_adapter(raw).name, provider)

    def test_every_provider_url_shape_picks_one_adapter(self):
        cases = {
            "netease": NETEASE_LINKS,
            "qq": ("https://y.qq.com/n/ryqq/playlist/7011264340",
                   "https://c.y.qq.com/qzone/fcg-bin/"
                   "fcg_ucc_getcdinfo_byids_cp.fcg?disstid=7011264340"),
            "kuwo": ("https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=12345",
                     "https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=12345"
                     "&source=5"),
            "kugou": ("https://www.kugou.com/yy/special/single/123.html?collection_3_1_2_0",
                      "collection_3_2132041455_14_0"),
            "qishui": ("https://music.douyin.com/qishui/share/playlist"
                       "?playlist_id=7397692920558452788",
                       "https://qishui.douyin.com/s/abcdef/"),
        }
        for provider, raws in cases.items():
            for raw in raws:
                with self.subTest(provider=provider, raw=raw):
                    self.assertEqual(find_adapter(raw).name, provider)

    def test_a_bare_number_refuses_to_guess(self):
        with self.assertRaises(InputError) as ctx:
            find_adapter("3778678")
        self.assertIn("3778678", ctx.exception.message)
        # it must name every candidate and the way out
        for provider in ("netease", "qq", "kuwo", "qishui"):
            self.assertIn(provider, ctx.exception.message)
        self.assertIn("--provider", ctx.exception.message)
        self.assertIn("带域名的歌单链接", ctx.exception.message)

    def test_numeric_ids_are_ambiguous_for_every_digit_provider_but_kugou(self):
        flags = {a.name: a.digits_are_ambiguous for a in all_adapters()}
        self.assertEqual(flags, {"netease": True, "qq": True, "kuwo": True,
                                 "kugou": False, "qishui": True})

    def test_kugou_refuses_a_bare_number_so_it_can_never_win_that_race(self):
        """`kugou` rejects numeric ids itself, so only real digit providers match."""
        self.assertFalse(KugouAdapter().match("12345"))
        self.assertEqual(find_adapter("12345", [NeteaseAdapter(), KugouAdapter()]).name,
                         "netease")

    def test_a_single_unambiguous_candidate_wins_even_with_digits(self):
        class Digits(Adapter):
            name, label = "digits", "数字"

            def match(self, raw):
                return raw.isdigit()

        class MoreDigits(Digits):
            name, label = "more-digits", "更多数字"

        self.assertEqual(find_adapter("42", [NeteaseAdapter(), Digits()]).name,
                         "digits")
        # ... but two of them are still ambiguous
        with self.assertRaises(InputError):
            find_adapter("42", [Digits(), MoreDigits()])

    def test_an_unrecognised_input_says_which_platforms_exist(self):
        with self.assertRaises(InputError) as ctx:
            find_adapter("https://example.com/playlist/1")
        self.assertIn("没有适配器认识这个输入", ctx.exception.message)
        for provider in ("网易云", "QQ音乐", "酷我", "酷狗", "汽水"):
            self.assertIn(provider, ctx.exception.message)

    def test_a_long_garbage_input_is_truncated_in_the_message(self):
        with self.assertRaises(InputError) as ctx:
            find_adapter("x" * 500)
        self.assertIn("x" * 120, ctx.exception.message)
        self.assertNotIn("x" * 121, ctx.exception.message)

    def test_an_unknown_provider_name_is_a_usage_error(self):
        with self.assertRaises(InputError) as ctx:
            adapter_by_name("spotify")
        self.assertEqual(ctx.exception.message, "未知的 provider: spotify")

    def test_looking_up_a_name_finds_the_same_class_as_the_registry(self):
        for adapter in all_adapters():
            with self.subTest(provider=adapter.name):
                self.assertIs(type(adapter_by_name(adapter.name)), type(adapter))


class ResolveCase(unittest.TestCase):
    def resolve(self, adapter, raw):
        return adapter.resolve(raw, None)


class TestNeteaseResolve(ResolveCase):
    def test_a_playlist_id_is_read_out_of_every_share_shape(self):
        for raw in NETEASE_LINKS:
            with self.subTest(raw=raw):
                ref = self.resolve(NeteaseAdapter(), raw)
                self.assertEqual(ref, Ref(provider="netease", key="3778678"))

    def test_a_bare_id_needs_no_network(self):
        ref = self.resolve(NeteaseAdapter(), " 3778678 ")
        self.assertEqual(ref.key, "3778678")
        self.assertEqual(ref.params, {})

    def test_a_netease_link_without_an_id_is_refused(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(NeteaseAdapter(), "https://music.163.com/#/playlist")
        self.assertIn("无法从输入中解析歌单 id", ctx.exception.message)


class TestQQResolve(ResolveCase):
    def test_both_link_shapes_yield_the_disstid(self):
        self.assertEqual(
            self.resolve(QQAdapter(), "https://y.qq.com/n/ryqq/playlist/7011264340"),
            Ref(provider="qq", key="7011264340"))
        self.assertEqual(
            self.resolve(QQAdapter(),
                         "https://c.y.qq.com/x.fcg?type=1&disstid=7011264340&json=1"),
            Ref(provider="qq", key="7011264340"))

    def test_the_disstid_query_wins_over_a_path_number(self):
        """`/playlist/1?disstid=2` is ambiguous on purpose: the query is the id."""
        ref = self.resolve(QQAdapter(),
                           "https://y.qq.com/n/ryqq/playlist/999?disstid=7011264340")
        self.assertEqual(ref.key, "7011264340")

    def test_a_qq_link_without_an_id_is_refused(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(QQAdapter(), "https://y.qq.com/n/ryqq/playlist")
        self.assertIn("无法从输入中解析歌单 id", ctx.exception.message)


class TestKuwoResolve(ResolveCase):
    def test_the_bodian_link_carries_the_source_parameter(self):
        ref = self.resolve(
            KuwoAdapter(),
            "https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=2289086040"
            "&source=7")
        self.assertEqual(ref.key, "2289086040")
        self.assertEqual(ref.params, {"source": "7"})

    def test_a_missing_source_falls_back_to_the_default(self):
        ref = self.resolve(
            KuwoAdapter(),
            "https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=2289086040")
        self.assertEqual(ref.params, {"source": "5"})

    def test_a_bare_id_is_accepted_with_the_default_source(self):
        ref = self.resolve(KuwoAdapter(), "2289086040")
        self.assertEqual(ref, Ref(provider="kuwo", key="2289086040",
                                  params={"source": "5"}))

    def test_www_kuwo_cn_is_refused_on_purpose(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(KuwoAdapter(),
                         "https://www.kuwo.cn/playlist_detail/2289086040")
        message = ctx.exception.message
        self.assertIn("www.kuwo.cn", message)
        self.assertIn("空成功", message)
        self.assertIn("h5app.kuwo.cn/m/bodian/collection.html?playlistId=", message)

    def test_a_kuwo_link_without_an_id_is_refused(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(KuwoAdapter(), "https://h5app.kuwo.cn/m/bodian/collection.html")
        self.assertIn("无法从输入中解析歌单 id", ctx.exception.message)


class TestKugouResolve(ResolveCase):
    def test_the_plaintext_collection_id_is_taken_verbatim(self):
        ref = self.resolve(KugouAdapter(), " collection_3_2132041455_14_0 ")
        self.assertEqual(ref, Ref(provider="kugou", key="collection_3_2132041455_14_0"))

    def test_it_is_found_inside_a_link(self):
        ref = self.resolve(
            KugouAdapter(),
            "https://www.kugou.com/share/collection_3_2132041455_14_0.html")
        self.assertEqual(ref.key, "collection_3_2132041455_14_0")

    def test_a_link_that_spells_the_api_field_still_picks_up_the_id(self):
        """Regression: `_COLLECTION` used to match the *field name* first.

        `collection_[0-9a-zA-Z_]+` was unanchored, so in
        `?global_collection_id=collection_3_...` it matched `collection_id` and
        `resolve` handed back a value no provider has. It now requires the match
        not to start inside an identifier.
        """
        raw = ("https://www.kugou.com/yy/html/special.html?global_collection_id="
               "collection_3_2132041455_14_0")
        ref = self.resolve(KugouAdapter(), raw)
        self.assertEqual(ref.key, "collection_3_2132041455_14_0")

    def test_a_share_page_that_spells_the_api_field_yields_the_real_id(self):
        """The same pattern is what `_share_lookup` scrapes with."""
        html = ('<script>window.$output = {"data":[{"global_collection_id":'
                '"collection_3_2132041455_14_0"}]}</script>')
        S.install_urlopen(self, lambda request, attempt:
                          S.text_response(html, url=request.full_url))
        ref = self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_abc/")
        self.assertEqual(ref.key, "collection_3_2132041455_14_0")

    def test_a_field_name_alone_is_still_not_an_id(self):
        """The guard keyword: only a *preceded-by-identifier* name is skipped.

        `collection_id` as a query value (`...?global_collection_id=collection_id`)
        still matches the pattern - it is a `collection_...`-shaped string. That
        is not a shape this tool can tell apart from a real id, and saying
        "cannot parse" there would be wrong too, so the behaviour is left as is
        and this test only documents it.
        """
        ref = self.resolve(
            KugouAdapter(),
            "https://www.kugou.com/yy/html/special.html"
            "?global_collection_id=collection_id")
        self.assertEqual(ref.key, "collection_id")

    def test_a_bare_specialid_is_refused_because_it_returns_empty_data(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "123456")
        message = ctx.exception.message
        self.assertIn("collection_3_<uid>_<idx>_0", message)
        self.assertIn("空成功", message)
        self.assertIn("global_collection_id", message)

    def test_a_gcid_share_page_is_scraped_for_the_plaintext_id(self):
        html = ('<script>window.$output = {"data":{"info":'
                '"collection_3_2132041455_14_0"}}</script>')
        calls = []

        def handler(request, attempt):
            calls.append(request.full_url)
            return S.text_response(html, url=request.full_url)

        S.install_urlopen(self, handler)
        ref = self.resolve(KugouAdapter(),
                           "https://m.kugou.com/songlist/gcid_abc123/?uid=99")
        self.assertEqual(ref.key, "collection_3_2132041455_14_0")
        self.assertEqual(ref.params["gcid"], "abc123")
        self.assertEqual(ref.params["resolved_from"],
                         "https://m.kugou.com/songlist/gcid_abc123/?uid=99")
        self.assertEqual(calls, ["https://m.kugou.com/songlist/gcid_abc123/?uid=99"])

    def test_a_gcid_page_without_any_payload_says_so_precisely(self):
        """页面真没有 window.$output 时才说「没有内嵌数据」。"""
        S.install_urlopen(self, lambda request, attempt: S.text_response("<html/>"))
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_deadbeef/")
        message = ctx.exception.message
        self.assertIn("没有可用的 collection_ 明文 id", message)
        self.assertIn("没有 window.$output", message)
        self.assertIn("collection_3_<uid>_<idx>_0", message)
        self.assertIn("docs/references/kugou.md", message)

    def test_a_page_that_hides_the_plaintext_id_says_that_instead(self):
        """2026-10-03 实测的真实形状：$output 在、歌单在，明文 id 是空串。

        这类歌单带 `no_show_gid=1`（用户自己的「喜欢」），页面只嵌前 10 首，
        服务那条路也走不通——报错必须说清是哪一种，别把「平台不给明文 id」
        说成「页面是空壳」。
        """
        # 用**合成**的名字/uid：这条测试的形状来自真实页面，但真实歌单属于用户私有，
        # 不允许把它的名字或 uid 写进仓库（fixture 与测试都只能放可以公开的东西）。
        payload = {
            "encode_gic": "gcid_abc",
            "global_collection_id": "",
            "info": {
                "listinfo": {"name": "示例歌单", "count": 228,
                             "list_create_username": "示例用户", "list_create_userid": 123456789,
                             "list_create_gid": "", "no_show_gid": 1},
                "songs": [{"name": f"第{i}首"} for i in range(10)],
            },
        }
        html = f"<script>window.$output = {json.dumps(payload, ensure_ascii=False)}</script>"
        S.install_urlopen(self, lambda request, attempt:
                          S.text_response(html, url=request.full_url))
        _no_service(self)
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_abc/?uid=123456789")
        message = ctx.exception.message
        self.assertIn("歌单「示例歌单」", message)
        self.assertIn("平台声明 228 首", message)
        self.assertIn("创建者 示例用户", message)
        self.assertIn("no_show_gid=1", message)
        self.assertIn("页面只嵌了前 10 首", message)
        self.assertIn("没有一份的名字与 uid同时对得上", message)
        self.assertNotIn("空壳", message)

    def test_a_hidden_id_is_recovered_by_probing_with_name_and_uid(self):
        """v1 的老办法：在 idx 1..5 里试，名字与 uid **同时**相等才算命中。

        这不是猜 id：候选空间有界，而且每个候选都要通过身份校验（名字单独相同
        不算——不同用户完全可以给歌单起同一个名字）。
        """
        html = _share_page(name="示例歌单", uid=123456789)
        asked = []

        def handler(request, attempt):
            url = request.full_url
            if "m.kugou.com" in url:
                return S.text_response(html, url=url)
            asked.append(url)
            idx = int(url.rsplit("_", 2)[1])
            # idx 1 是同一个名字、但 uid 不同 → 必须被拒
            owner = 999 if idx == 1 else 123456789
            return S.json_response({"data": [{"name": "示例歌单",
                                              "list_create_userid": owner}]}, url=url)

        S.install_urlopen(self, handler)
        _fake_service(self)
        ref = self.resolve(KugouAdapter(),
                           "https://m.kugou.com/songlist/gcid_abc/?uid=123456789")
        self.assertEqual(ref.key, "collection_3_123456789_2_0")
        self.assertEqual(ref.params["resolved_by"], "probe(name+uid)")
        self.assertEqual(ref.params["probed_idx"], 2)
        self.assertEqual(len(asked), 2, "idx 1 被 uid 校验挡下后才试 idx 2")

    def test_a_probe_that_finds_nothing_reports_that_it_probed(self):
        html = _share_page(name="示例歌单", uid=123456789)
        calls = []

        def handler(request, attempt):
            url = request.full_url
            if "m.kugou.com" in url:
                return S.text_response(html, url=url)
            calls.append(url)
            return S.json_response({"data": []}, url=url)

        S.install_urlopen(self, handler)
        _fake_service(self)
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_abc/?uid=123456789")
        self.assertEqual(len(calls), 5, "idx 1..5 都试过")
        self.assertIn("没有一份的名字与 uid同时对得上", ctx.exception.message)

    def test_no_uid_means_no_probe_at_all(self):
        """链接和页面都没有 uid 时不探测——没有校验依据的试探就是猜。"""
        html = _share_page(name="示例歌单", uid=None)
        calls = []

        def handler(request, attempt):
            calls.append(request.full_url)
            return S.text_response(html, url=request.full_url)

        S.install_urlopen(self, handler)
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_abc/")
        self.assertEqual(len(calls), 1, "只取了一次分享页，没有去试任何 collection_ id")
        self.assertNotIn("试过", ctx.exception.message)

    def test_a_failing_share_lookup_says_the_page_was_not_reached(self):
        def handler(request, attempt):
            raise S.url_error("no route to host")

        S.install_urlopen(self, handler)
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://m.kugou.com/songlist/gcid_x/")
        message = ctx.exception.message
        self.assertIn("分享页这次没取到", message)
        self.assertIn("collection_3_<uid>_<idx>_0", message)

    def test_a_kugou_link_with_neither_id_is_refused(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(KugouAdapter(), "https://www.kugou.com/")
        self.assertIn("无法从输入中解析歌单 id", ctx.exception.message)


class TestQishuiResolve(ResolveCase):
    def test_the_share_link_and_the_api_link_both_carry_the_playlist_id(self):
        share = ("https://music.douyin.com/qishui/share/playlist"
                 "?playlist_id=7397692920558452788")
        api = "https://music.douyin.com/qishui/api/playlist?playlist_id=7397692920558452788"
        self.assertEqual(self.resolve(QishuiAdapter(), share).key,
                         "7397692920558452788")
        self.assertEqual(self.resolve(QishuiAdapter(), api).key,
                         "7397692920558452788")

    def test_a_short_link_is_followed_without_a_client(self):
        target = ("https://music.douyin.com/qishui/share/playlist"
                  "?playlist_id=7397692920558452788")
        seen = []

        def handler(request, attempt):
            seen.append(request.full_url)
            return S.text_response("<html/>", url=target)

        S.install_urlopen(self, handler)
        ref = self.resolve(QishuiAdapter(), "https://qishui.douyin.com/s/abc123/")
        self.assertEqual(ref.key, "7397692920558452788")
        self.assertEqual(ref.params["resolved_from"],
                         "https://qishui.douyin.com/s/abc123/")
        self.assertEqual(ref.params["final"], target)
        self.assertEqual(seen, ["https://qishui.douyin.com/s/abc123/"])

    def test_a_short_link_code_without_a_scheme_is_expanded(self):
        target = ("https://music.douyin.com/qishui/share/playlist"
                  "?playlist_id=42")
        seen = []

        def handler(request, attempt):
            seen.append(request.full_url)
            return S.text_response("<html/>", url=target)

        S.install_urlopen(self, handler)
        ref = self.resolve(QishuiAdapter(), "qishui.douyin.com/s/xyz/")
        self.assertEqual(ref.key, "42")
        self.assertEqual(seen, ["https://qishui.douyin.com/s/xyz/"],
                         "a bare share code must be given the https scheme")

    def test_a_short_link_that_does_not_land_on_a_playlist_is_a_notfound(self):
        S.install_urlopen(self, lambda request, attempt:
                          S.text_response("<html/>",
                                          url="https://music.douyin.com/qishui/home"))
        with self.assertRaises(Exception) as ctx:
            self.resolve(QishuiAdapter(), "https://qishui.douyin.com/s/abc/")
        self.assertEqual(getattr(ctx.exception, "kind", None), KIND_NOTFOUND)
        self.assertIn("短链没有跳转到歌单页", str(ctx.exception))

    def test_a_short_link_http_error_keeps_its_status(self):
        def handler(request, attempt):
            raise S.http_error(request.full_url, 410)

        S.install_urlopen(self, handler)
        with self.assertRaises(Exception) as ctx:
            self.resolve(QishuiAdapter(), "https://qishui.douyin.com/s/gone/")
        self.assertEqual(getattr(ctx.exception, "kind", None), KIND_NOTFOUND)
        self.assertIn("HTTP 410", str(ctx.exception))

    def test_a_short_link_transport_error_is_a_network_error(self):
        def handler(request, attempt):
            raise S.url_error("connection refused")

        S.install_urlopen(self, handler)
        with self.assertRaises(Exception) as ctx:
            self.resolve(QishuiAdapter(), "https://qishui.douyin.com/s/abc/")
        self.assertEqual(getattr(ctx.exception, "kind", None), KIND_NETWORK)

    def test_an_off_domain_short_link_is_refused_before_any_request(self):
        """SSRF guard: the hop target must be a douyin host."""
        raw = "https://evil.example/?u=qishui.douyin.com/s/abc/"
        calls = []
        S.install_urlopen(self, lambda request, attempt:
                          calls.append(request.full_url))
        with self.assertRaises(InputError) as ctx:
            self.resolve(QishuiAdapter(), raw)
        self.assertEqual(calls, [], "the guard runs before the request")
        self.assertEqual(ctx.exception.message,
                         f"qishui: 拒绝请求非抖音域名（SSRF 防护）: {raw}")

    def test_a_douyin_link_without_an_id_is_refused(self):
        with self.assertRaises(InputError) as ctx:
            self.resolve(QishuiAdapter(), "https://music.douyin.com/qishui/home")
        self.assertIn("无法从输入中解析 playlist_id", ctx.exception.message)


class TestMatchIsAboutTheInputAlone(unittest.TestCase):
    def test_match_never_raises_and_never_looks_like_a_resolved_ref(self):
        for adapter in all_adapters():
            for raw in ("", " ", "not a link", "https://example.com/", "123"):
                with self.subTest(provider=adapter.name, raw=raw):
                    self.assertIsInstance(adapter.match(raw), bool)

    def test_a_bare_number_matches_exactly_the_digit_providers(self):
        matches = sorted(a.name for a in all_adapters() if a.match("3778678"))
        self.assertEqual(matches, ["kuwo", "netease", "qishui", "qq"])

    def test_every_adapter_claims_its_own_fixture_input(self):
        for provider in S.PROVIDERS:
            adapter = S.adapter(provider)
            raw = S.EXPECTED[provider]["input"]
            with self.subTest(provider=provider):
                self.assertTrue(adapter.match(raw),
                                f"{adapter.name} must claim {raw!r}")


if __name__ == "__main__":
    unittest.main()

def _share_page(*, name: str, uid, count: int = 228, songs: int = 10) -> str:
    """构造一份与真实分享页同形状的 `window.$output`（名字/uid 都是合成的）。"""
    listinfo = {"name": name, "count": count, "list_create_username": "示例用户",
                "list_create_gid": "", "no_show_gid": 1}
    if uid is not None:
        listinfo["list_create_userid"] = uid
    payload = {"encode_gic": "gcid_abc", "global_collection_id": "",
               "info": {"listinfo": listinfo,
                        "songs": [{"name": f"第{i}首"} for i in range(songs)]}}
    return f"<script>window.$output = {json.dumps(payload, ensure_ascii=False)}</script>"


def _fake_service(case, port: int = 9999):
    """把 NodeService 换掉：ensure() 直接给一个 loopback handle（不下载任何东西）。"""
    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def ensure(self):
            return contextlib.nullcontext(types.SimpleNamespace(
                base_url=f"http://127.0.0.1:{port}", mode="node-service-local",
                install_method="fake", resolved_digest=None, upstream_commit=None))

    S.patch_attr(case, "mpa.adapters.kugou.NodeService", FakeService)


def _no_service(case):
    """让服务起不来（模拟没装 node / 不允许 provision），探测应当安静地放弃。"""
    class DeadService:
        def __init__(self, *args, **kwargs):
            pass

        def ensure(self):
            raise RouteUnavailable("kugou: 本地服务不可用（测试桩）")

    S.patch_attr(case, "mpa.adapters.kugou.NodeService", DeadService)

