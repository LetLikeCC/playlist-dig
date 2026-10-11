"""Shared helpers for the offline suite.  Not a test module.

``unittest discover`` only collects ``test*.py``, so this file is imported by the
test modules and never run on its own.

It holds three things:

* fixture access (``playlist``, ``fetch_log``, ``raw_store``, ``derive`` ...),
  all read-only - tests that need to corrupt data copy the fixture first;
* the fake ``urllib.request.urlopen`` plumbing used by the runtime tests, so no
  test can accidentally reach the network;
* ``EXPECTED``: the recorded numbers of each live run.  Those numbers are the
  point of the fixture tests - if a fixture or an adapter drifts, the suite must
  fail instead of quietly agreeing with itself.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(HERE), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mpa.adapters import adapter_by_name  # noqa: E402
from mpa.adapters.base import Ctx, Ref, as_track  # noqa: E402
from mpa.model import Playlist, PlaylistMeta  # noqa: E402
from mpa.rawstore import RawStore  # noqa: E402

FIXTURES = HERE / "fixtures"

EXPECTED: dict[str, dict] = {
    "netease": {
        "input": "3778678",
        "mode": "direct-http",
        "fetched_at": "2026-09-29T17:39:35Z",
        "adapter_version": "2.0.0",
        "upstream_commit": None,
        "declared": 200,
        "fetched": 200,
        "ok": 200,
        "digest": "7ced5461838b68e64420ad5c6ab7e5890206fd05793b01996088b4a919c1103c",
        "requests": 3,
        "parts": ("playlist_detail_v3", "song_detail_000", "song_detail_001"),
        "title": "热歌榜",
        "creator_name": "网易云音乐",
        "warnings": (),
        "capabilities": {"duration_ms": True, "language": False, "released_at": True,
                         "collected_at": False,
                         "popularity": True, "alias": True, "trans_name": True,
                         "playlist.created_at": True, "playlist.updated_at": True,
                         "playlist.track_updated_at": True},
        "extra_keys": ("popularity", "alias", "trans_name"),
        "playlist_extra_keys": ("created_at", "updated_at", "track_updated_at"),
    },
    "qq": {
        "input": "https://y.qq.com/n/ryqq/playlist/7011264340",
        "mode": "direct-http",
        "fetched_at": "2026-09-29T17:41:26Z",
        "adapter_version": "2.0.0",
        "upstream_commit": None,
        "declared": 76,
        "fetched": 76,
        "ok": 76,
        "digest": "69f8a4b184a0693795feb19d24768c8112950f6e3602a7be2de9310ae9677255",
        "requests": 4,
        "parts": ("album_detail_000", "album_detail_000_req", "album_detail_001",
                  "album_detail_001_req", "album_detail_002", "album_detail_002_req",
                  "playlist_fcg"),
        "title": "电子 · 房间里的单人舞池",
        "creator_name": "穿着皮鞋跳大神",
        "warnings": ("语种为专辑级字段，同一专辑内所有曲目相同：英语 40、纯音乐 24、国语 10、其他 2",),
        "capabilities": {"duration_ms": True, "language": True, "released_at": True,
                         "collected_at": False,
                         "album_type": True, "label": True,
                         "playlist.created_at": True},
        "extra_keys": ("album_type", "label"),
        "playlist_extra_keys": ("created_at",),
    },
    "kugou": {
        "input": "collection_3_2132041455_14_0",
        "mode": "node-service-external",
        "fetched_at": "2026-09-29T17:53:05Z",
        "adapter_version": "2.0.0",
        "upstream_commit": "9b6a779abfc9b4590ed671836c82db398fde5929",
        "declared": 50,
        "fetched": 50,
        "ok": 50,
        "digest": "e8924c06650a84b498be8a676621d4d106e9659f0a4791d8793f1cf93e9a8eab",
        "requests": 2,
        "parts": ("playlist_detail", "track_all_01"),
        "title": "2026浪漫情歌：情人节许愿，春节就实现！",
        "creator_name": "酷狗文化",
        "warnings": (),
        "capabilities": {"duration_ms": True, "language": True, "released_at": True,
                         "collected_at": True,
                         "fileid": True, "remark": True, "bpm": True,
                         "playlist.tags": True, "playlist.created_at": True,
                         "playlist.updated_at": True, "playlist.published_at": True},
        "extra_keys": ("fileid", "remark", "bpm"),
        "playlist_extra_keys": ("tags", "created_at", "updated_at", "published_at"),
    },
    "qishui": {
        "input": ("https://music.douyin.com/qishui/share/playlist"
                  "?playlist_id=7397692920558452788"),
        "mode": "ssr-partial",
        "fetched_at": "2026-09-29T17:46:36Z",
        "adapter_version": "2.0.0",
        "upstream_commit": None,
        "declared": 65,
        "fetched": 65,
        "ok": 65,
        "digest": "43791ffa78e558a903e1d8adde970f6072e866f24b615592a8fa1fda7d4f5fe5",
        "requests": 1,
        "parts": ("ssr_page",),
        "title": "2024抖音超火💗氛围感情歌🔥精选70首（更新超快）",
        "creator_name": "汽水音乐APP",
        "creator_id": "2331429247649368",
        "description": "在你给的爱里沉溺💗",
        "warnings": (),
        "capabilities": {"duration_ms": True, "language": True, "released_at": True,
                         "collected_at": False, "genre": True, "subgenre": True,
                         "composers": True, "lyricists": True,
                         "playlist.created_at": True, "playlist.updated_at": True},
        "extra_keys": ("genre", "subgenre", "composers", "lyricists"),
        "playlist_extra_keys": ("created_at", "updated_at"),
    },
}

PROVIDERS = tuple(EXPECTED)

#: canonical field -> the four provider responses in which it is unavailable
UNAVAILABLE_FIELDS = {
    "netease": ("language", "collected_at"),
    "qq": ("collected_at",),
    "kugou": (),
    "qishui": ("collected_at",),
}


# --- fixture access ---------------------------------------------------------

def fixture_dir(provider: str) -> Path:
    return FIXTURES / provider


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def playlist_obj(provider: str) -> dict:
    return read_json(fixture_dir(provider) / "playlist.json")


def playlist(provider: str) -> Playlist:
    return Playlist.from_obj(playlist_obj(provider))


def fetch_log(provider: str) -> dict:
    return read_json(fixture_dir(provider) / "fetch.log.json")


def provenance(provider: str) -> dict:
    return read_json(fixture_dir(provider) / "provenance.json")


def manifest(provider: str) -> dict:
    return read_json(fixture_dir(provider) / "raw" / "_manifest.json")


def adapter(provider: str):
    return adapter_by_name(provider)


def ctx(provider: str) -> Ctx:
    """The exact ctx `verify` rebuilds: taken from the fetch log, not re-derived."""
    return Ctx.from_obj(fetch_log(provider)["ctx"])


def raw_store(provider: str) -> RawStore:
    store = RawStore(fixture_dir(provider) / "raw", write=False)
    store.load_all()
    return store


def derive(provider: str) -> Playlist:
    """Offline re-derivation, identical to what `mpa verify` does."""
    return adapter(provider).derive(raw_store(provider).parts, ctx(provider))


def track_field_sources(provider: str) -> dict[str, dict[str, int]]:
    """`{field: {source: count}}` as recorded in playlist.json."""
    out: dict[str, dict[str, int]] = {}
    for name in ("duration_ms", "language", "released_at", "collected_at"):
        counts: dict[str, int] = {}
        for track in playlist_obj(provider)["tracks"]:
            source = (track.get(name) or {}).get("source", "missing")
            counts[source] = counts.get(source, 0) + 1
        out[name] = counts
    return out


@contextlib.contextmanager
def copied_fixture(provider: str):
    """A writable copy of one fixture; the original is never touched."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / provider
        shutil.copytree(fixture_dir(provider), dest)
        yield dest


# --- fake urlopen -----------------------------------------------------------

class FakeResponse:
    """Just enough of an ``http.client.HTTPResponse`` for HttpClient."""

    def __init__(self, body=b"", *, status: int = 200, url: str = "http://fake/",
                 headers: dict | None = None):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status = status
        self._url = url
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def json_response(obj, *, status: int = 200, url: str = "http://fake/") -> FakeResponse:
    return FakeResponse(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        status=status, url=url)


def text_response(text: str, *, status: int = 200, url: str = "http://fake/") -> FakeResponse:
    return FakeResponse(text.encode("utf-8"), status=status, url=url)


def http_error(url: str, status: int) -> urllib.error.HTTPError:
    # A real fp, not None: HTTPError.__del__ closes it, and passing None makes
    # CPython print "ResourceWarning: Implicitly cleaning up <HTTPError ...>"
    # for every faked failure, drowning the real warnings.
    err = urllib.error.HTTPError(url, status, f"HTTP {status}", {},
                                 io.BytesIO(b""))
    # urllib's addbase is a tempfile._TemporaryFileWrapper: leaving it unclosed
    # makes CPython warn "ResourceWarning: Implicitly cleaning up <HTTPError ...>"
    # at GC time.  Nothing reads a failed response body, so close it now.
    err.close()
    return err


def url_error(reason="connection reset") -> urllib.error.URLError:
    return urllib.error.URLError(reason)


class FakeUrlopen:
    """Drop-in replacement for ``urllib.request.urlopen``.

    ``handler(request, attempt)`` returns a response, or raises an exception to
    simulate a transport failure.  ``attempt`` is 1-based so tests can make the
    first call fail and the second succeed.
    """

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list = []

    @property
    def count(self) -> int:
        return len(self.requests)

    def __call__(self, req, timeout=None, **kwargs):
        self.requests.append(req)
        self.timeouts.append(timeout)
        result = self.handler(req, len(self.requests))
        if isinstance(result, BaseException):
            raise result
        return result


def install_urlopen(case, handler) -> FakeUrlopen:
    """Patch the global ``urlopen`` for one test; restored via ``addCleanup``."""
    fake = FakeUrlopen(handler)
    real = urllib.request.urlopen
    urllib.request.urlopen = fake
    case.addCleanup(lambda: setattr(urllib.request, "urlopen", real))
    return fake


def patch_attr(case, dotted: str, value):
    """Replace ``module.attribute`` for one test; restored via ``addCleanup``."""
    modname, _, attr = dotted.rpartition(".")
    module = importlib.import_module(modname)
    old = getattr(module, attr)
    setattr(module, attr, value)
    case.addCleanup(setattr, module, attr, old)
    return old


class NullClient:
    """A minimal stand-in for HttpClient, for code paths that never do I/O."""

    def __init__(self, **attrs):
        self.log: list = []
        self.errors: list = []
        for key, value in attrs.items():
            setattr(self, key, value)


class ToyAdapterBase:
    """A tiny adapter used to drive the pipeline without the network.

    ``collect`` makes exactly one request per playlist, so nothing here depends
    on a second network round trip.
    """

    name = "toy"
    label = "玩具平台"
    version = "9.9.9"
    default_mode = "toy-direct"
    capabilities = {"playlist": True, "duration_ms": True}
    upstream_commit = "0123456789abcdef0123456789abcdef01234567"
    reference = "docs/references/toy.md"
    verified_on = "2025-01-01"

    def match(self, raw: str) -> bool:
        return raw.startswith("toy:")

    def resolve(self, raw: str, client) -> Ref:
        return Ref(self.name, raw.split(":", 1)[1])

    def collect(self, client, ref, ctx) -> dict:
        payload = client.get_json("detail", f"http://toy.invalid/{ref.key}")
        return {"detail": payload}

    def derive(self, parts, ctx):
        data = parts["detail"]
        tracks = [as_track(i, title=t["title"], id=str(t["id"]))
                  for i, t in enumerate(data["songs"])]
        meta = PlaylistMeta(id=ctx.ref.key, title=data["name"], creator_name="玩具",
                            creator_id=None, description=None,
                            declared_count=data["count"])
        return self.make(ctx, meta, tracks)
