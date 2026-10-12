"""Fetch / verify orchestration.

``fetch_playlist`` is the only place that writes to an output directory, and it
writes the four things that make the dataset auditable later:

    <outdir>/playlist.json     canonical data (mpa/playlist@1)
    <outdir>/raw/              every response body, named deterministically
    <outdir>/raw/_manifest.json  method/url/status/bytes/sha256 per part
    <outdir>/fetch.log.json    how it was fetched (provider, ref, mode, tools)

``verify_playlist`` reads those back and re-runs the *pure* derive step, so it
needs no network, no node and no clock. If verification fails, either the stored
data was edited or derive is no longer pure - both are bugs worth failing loudly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from .adapters import adapter_by_name, find_adapter
from .adapters.base import Ctx, Ref, now_iso
from .errors import (EXIT_OK, EXIT_PARTIAL, InputError, MPError, DatasetError)
from .export import canonical_json, write_json
from .model import Playlist
from .rawstore import RawStore
from .runtime import HttpClient

PLAYLIST_FILE = "playlist.json"
LOG_FILE = "fetch.log.json"
RAW_DIR = "raw"


@dataclass
class FetchResult:
    playlist: Playlist
    outdir: Path
    log: dict
    exit_code: int = EXIT_OK
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


@dataclass
class VerifyResult:
    ok: bool
    outdir: Path
    checks: dict
    diffs: list[str] = dc_field(default_factory=list)
    # Which checks decided ``ok``. Anything outside this list still shows up, but
    # only as a warning: e.g. 适配器版本不同 in a non-strict run.
    required: tuple[str, ...] = ()


class Reporter:
    """Tiny progress sink: stderr by default, silent under --quiet."""

    def __init__(self, stream=None, quiet: bool = False):
        self.stream = stream
        self.quiet = quiet

    def __call__(self, message: str) -> None:
        if self.quiet or self.stream is None:
            return
        try:
            print(message, file=self.stream, flush=True)
        except BrokenPipeError:
            # 进度输出断了不该毁掉一次抓取：数据完不完整、退出码是几，都不该由
            # 「读的人还在不在」决定。后面的调用直接变静默。
            self.stream = None


def _exit_for(playlist: Playlist) -> int:
    """0 only when we can prove nothing is missing.

    ``declared is None`` counts as a gap: without a total there is nothing to
    compare against, so "全取到了" is not a claim we can make. ``--partial-ok``
    deliberately does *not* soften this: its job is to fall back to partial data
    when a route dies (see the qishui service path), not to turn an incomplete
    fetch into a success. 退出码 1 是留给调用方「有缺口，必须转述」的信号。
    """
    if playlist.ledger.declared is None or (playlist.ledger.gap or 0) > 0:
        return EXIT_PARTIAL
    return EXIT_OK


def fetch_playlist(input_ref: str, outdir: str | Path, *, provider: str | None = None,
                   save_raw: bool = True, timeout: float = 25.0, retries: int = 2,
                   min_interval: float = 1.0, service_url: str | None = None,
                   service_home: str | Path | None = None,
                   allow_provision: bool = True, partial_ok: bool = False,
                   reporter: Reporter | None = None) -> FetchResult:
    """Fetch one playlist into ``outdir``.

    Nothing here is cached: every call really reads the platform, because a
    cached "dataset" would be indistinguishable from a fabricated one.
    """
    outdir = Path(outdir)
    reporter = reporter or Reporter()
    adapter_kwargs = {"service_url": service_url, "service_home": service_home,
                      "allow_provision": allow_provision, "log": lambda m: reporter(m)}
    adapter = (adapter_by_name(provider, adapters=_all(**adapter_kwargs))
               if provider else find_adapter(input_ref, adapters=_all(**adapter_kwargs)))
    reporter(f"[{adapter.name}] 使用适配器 {adapter.version}，模式 {adapter.default_mode}")

    store = RawStore(outdir / RAW_DIR, write=save_raw)
    client = HttpClient(adapter.name, store, timeout=timeout, retries=retries,
                        min_interval=min_interval)
    started = now_iso()
    ref = adapter.resolve(input_ref, client)
    ctx = Ctx(input=input_ref, ref=ref, mode=adapter.default_mode, fetched_at=started)
    if partial_ok:
        ctx.extra["partial_ok"] = True
    t0 = time.monotonic()
    playlist = adapter.fetch(client, ref, ctx)
    elapsed = time.monotonic() - t0

    playlist.source.raw_saved = bool(save_raw and store.parts)
    playlist.source.raw_digest = store.digest() if save_raw else None
    playlist.source.requests = len(client.log)
    if partial_ok and (playlist.ledger.declared is None or (playlist.ledger.gap or 0) > 0):
        # Not a Field and not a warning: warnings must be reproducible from the
        # recorded data, so "the caller accepted a gap" belongs in the log only.
        gap_text = ("平台没有给出总数" if playlist.ledger.declared is None
                    else f"缺口 {playlist.ledger.gap} 首")
        reporter(f"[{adapter.name}] --partial-ok：接受不完整结果（{gap_text}），"
                 "退出码仍为 1；缺口照旧写在 ledger 和 warnings 里")

    log = {
        "schema": "mpa/fetchlog@1",
        "provider": adapter.name,
        "input": input_ref,
        "ref": ref.to_obj(),
        "mode": ctx.mode,
        "adapter_version": adapter.version,
        "upstream_commit": ctx.upstream_commit or adapter.upstream_commit,
        "started_at": started,
        "finished_at": now_iso(),
        "elapsed_s": round(elapsed, 2),
        "raw_saved": playlist.source.raw_saved,
        "raw_digest": playlist.source.raw_digest,
        "requests": client.log,
        "errors": client.errors,
        "ctx": ctx.to_obj(),
        "partial_ok": bool(partial_ok),
        "exit_code": _exit_for(playlist),
        "warnings": playlist.warnings,
    }
    if ctx.extra:
        log["extra"] = ctx.extra

    outdir.mkdir(parents=True, exist_ok=True)
    cleaned: list[str] = []
    if save_raw:
        cleaned = store.finish()
    if cleaned:
        # 同目录二次抓取：上一次留下的响应会删掉，并在日志里留下名字，
        # 免得 raw/ 里混着两代适配器的记录、谁也说不清 playlist.json 是怎么来的。
        log["raw_cleanup"] = cleaned
        reporter(f"[{adapter.name}] 清掉了 {len(cleaned)} 个上一次抓取留下的响应文件："
                 + ", ".join(cleaned[:4]) + (" 等" if len(cleaned) > 4 else ""))
    write_json(outdir / PLAYLIST_FILE, playlist.to_obj())
    write_json(outdir / LOG_FILE, log)
    reporter(f"[{adapter.name}] {len(playlist.tracks)} 首，ledger={playlist.ledger.to_obj()}")
    for w in playlist.warnings:
        reporter(f"[{adapter.name}] 警告: {w}")
    return FetchResult(playlist=playlist, outdir=outdir, log=log,
                       exit_code=log["exit_code"], warnings=list(playlist.warnings))


def _all(**kwargs):
    from .adapters import all_adapters
    return all_adapters(**kwargs)


# --- verify -----------------------------------------------------------------

def _read_dataset_json(path: Path, what: str) -> dict:
    """读数据集里的一个 JSON 文件；坏了就抛 ``DatasetError``（一行中文，不是 traceback）。

    「文件不在」是输入问题（``InputError`` → 退出码 4，用户指错了目录）；
    「文件在但读不出/不是 JSON 对象」是**数据坏了**（``DatasetError`` → 退出码 2）——
    这两件事的修复动作不同，退出码也不该一样。
    """
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InputError(f"{path} 不存在：缺少{what}，这不是一份完整的 playlist-dig 数据集") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(
            f"{path} 不是合法 JSON（{type(exc).__name__}: {exc}）：这份数据集已损坏。"
            f"如果 raw/ 与 fetch.log.json 还在，可以用 "
            f"`python3 tools/rederive_fixture.py {path.parent}` 离线重算；"
            "否则往同一个目录重跑一次 `mpa fetch` 即可自愈",
            context={"file": str(path)}) from None
    if not isinstance(obj, dict):
        raise DatasetError(
            f"{path} 的顶层不是 JSON 对象（是 {type(obj).__name__}）："
            "这不是一份 playlist-dig 数据集",
            context={"file": str(path), "got": type(obj).__name__})
    return obj


def load_dataset(outdir: str | Path) -> tuple[Playlist, dict]:
    outdir = Path(outdir)
    pl_path = outdir / PLAYLIST_FILE
    log_path = outdir / LOG_FILE
    if not pl_path.exists():
        raise InputError(f"{pl_path} 不存在：这不是一个 playlist-dig 输出目录")
    if not log_path.exists():
        raise InputError(f"{log_path} 不存在：缺少 fetch log，无法知道当初是怎么取的")
    playlist = Playlist.from_obj(_read_dataset_json(pl_path, PLAYLIST_FILE))
    log = _read_dataset_json(log_path, LOG_FILE)
    return playlist, log


def restore_fetch_meta(playlist: Playlist, log: dict) -> Playlist:
    """Put the fetch-level fields back onto a re-derived playlist.

    ``raw_saved`` / ``raw_digest`` / ``requests`` / ``adapter_version`` describe
    *the fetch*, not the derivation, and a re-derivation in memory cannot know
    them. Restoring from ``fetch.log.json`` before comparing is what makes
    ``verify`` answer "did the data change?" instead of "did the tool change?".
    ``raw_digest`` is still checked against the disk separately, and the tool
    that rewrites a fixture's ``playlist.json`` uses this same function so it
    cannot drift from ``verify``.
    """
    playlist.source.raw_saved = bool(log.get("raw_saved"))
    playlist.source.raw_digest = log.get("raw_digest")
    playlist.source.requests = len(log.get("requests") or [])
    playlist.source.adapter_version = (log.get("adapter_version")
                                       or playlist.source.adapter_version)
    return playlist


def verify_playlist(outdir: str | Path, *, strict: bool = False) -> VerifyResult:
    outdir = Path(outdir)
    playlist, log = load_dataset(outdir)
    checks: dict = {}
    diffs: list[str] = []

    checks["schema_ok"] = playlist.schema == "mpa/playlist@1"
    if not checks["schema_ok"]:
        diffs.append(f"playlist.json 的 schema 是 {playlist.schema!r}")

    raw_dir = outdir / RAW_DIR
    if not raw_dir.is_dir() or not any(raw_dir.iterdir()):
        raise InputError(
            f"{raw_dir} 没有原始响应：这份数据是用 --no-raw 取的，无法离线复算。"
            "（这正是我们坚持默认保存 raw 的原因）")

    store = RawStore(raw_dir, write=False)
    checks["manifest_ok"], manifest_msg = store.check_manifest()
    if not checks["manifest_ok"]:
        diffs.append(manifest_msg)
    checks["raw_digest_ok"] = playlist.source.raw_digest in (None, store.digest())
    if not checks["raw_digest_ok"]:
        diffs.append(f"raw 摘要不一致：playlist.json 记录 {playlist.source.raw_digest}，"
                     f"磁盘上是 {store.digest()}")

    ctx = Ctx.from_obj(log.get("ctx") or {})
    adapter = adapter_by_name(playlist.source.provider)
    checks["adapter_version_ok"] = playlist.source.adapter_version == adapter.version
    if not checks["adapter_version_ok"]:
        diffs.append(f"适配器版本不同：数据由 {playlist.source.adapter_version} 生成，"
                     f"当前是 {adapter.version}（默认仍然算通过，--strict 下算失败）")
    parts = store.load_all()
    rebuilt = adapter.derive(parts, ctx)
    restore_fetch_meta(rebuilt, log)
    got = canonical_json(rebuilt.to_obj())
    want = canonical_json(json.loads((outdir / PLAYLIST_FILE).read_text(encoding="utf-8")))
    checks["derive_deterministic"] = got == want
    if got != want:
        diffs.append(_first_diff(want, got))

    # 默认就要求这四项：schema 对、raw/ 自洽、摘要对得上、断网重算一致。
    # --strict 额外只加一条：数据必须由当前这版适配器生成。别的都不该「告一段落
    # 就算了」——一个 checks 里 FAIL 却打印「验证通过」的 verify 没有意义。
    required = ["schema_ok", "manifest_ok", "raw_digest_ok", "derive_deterministic"]
    if strict:
        required.append("adapter_version_ok")
    ok = all(checks[name] for name in required)
    return VerifyResult(ok=ok, outdir=outdir, checks=checks, diffs=diffs,
                        required=tuple(required))


def _first_diff(want: str, got: str, width: int = 120) -> str:
    """Show the *actual* differing character, not a truncated prefix.

    Truncating both sides to N chars hid a real difference once; a diff that can
    lie about two lines being equal is worse than no diff.
    """
    wl, gl = want.splitlines(), got.splitlines()
    for i, (a, b) in enumerate(zip(wl, gl)):
        if a != b:
            j = 0
            while j < min(len(a), len(b)) and a[j] == b[j]:
                j += 1
            lo = max(0, j - width // 2)
            return (f"重新推导的结果与 playlist.json 不一致（第 {i + 1} 行，第 {j + 1} 列）:\n"
                    f"  存储: …{a[lo:lo + width]}…\n"
                    f"  复算: …{b[lo:lo + width]}…")
    if len(wl) != len(gl):
        extra = (wl[len(gl):] or gl[len(wl):])[0]
        return (f"重新推导的结果行数不同: 存储 {len(wl)} 行, 复算 {len(gl)} 行"
                f"（首个多出的行: {extra[:width]}）")
    return "重新推导的结果与 playlist.json 不同，但逐行比较没有找到差异（请报告这个 bug）"
