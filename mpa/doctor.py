"""`mpa doctor`: check the environment and, optionally, the real endpoints.

Three questions, in this order:

1. Can this machine run the tool at all? (python, node/npm/pnpm, writable cache)
2. For the platforms that need a local node service, is one available?
3. With ``--live``: does the provider still answer, and does it still return the
   fields the adapter claims? This is the check that turns "upstream changed" from
   a user complaint into a failing test.

Rows are ``{name, ok, detail, skipped}``. Nothing here writes outside the cache
directory except a temporary fetch when ``--live`` is used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .adapters import all_adapters
from .adapters.node_service import default_home, free_port, which_all
from .errors import MPError
from .coverage import coverage
from .model import TRACK_FIELDS
from .pipeline import Reporter, fetch_playlist

#: public playlists used by --live. Kept small on purpose: doctor should be cheap.
#: Only ids that were actually fetched successfully are listed here; a platform
#: without a confirmed id gets ``None`` and is reported as skipped instead of
#: being "verified" against a guessed id.
SAMPLES = {
    "netease": "3778678",
    "qq": "7011264340",
    "kuwo": None,  # 未实测：手头没有公开的波点歌单 id，见 docs/references/kuwo.md
    "kugou": "collection_3_2132041455_14_0",
    "qishui": "7397692920558452788",
}


def run_doctor(*, provider: str | None = None, live: bool = False,
               playlist_id: str | None = None, out_root: Path | None = None) -> dict:
    report: dict = {}
    rows: list[dict] = []
    rows.append(_row("python", True, sys.version.split()[0]))
    py_ok = sys.version_info >= (3, 10)
    rows.append(_row("python>=3.10", py_ok,
                     "需要 3.10+（用到 X | Y 类型标注）" if not py_ok else "ok"))
    toolchain = which_all("node", "npm", "pnpm")
    for tool, path in toolchain.items():
        rows.append({"name": tool, "ok": bool(path), "skipped": not path,
                     "detail": path or "未安装（酷狗需要它来跑本地服务；汽水只在超过 175 首时用）"})
    home = Path(os.environ.get("PLAYLIST_DIG_HOME") or default_home())
    writable = _writable(home)
    rows.append({"name": "缓存目录可写", "ok": writable, "skipped": False,
                 "detail": f"{home}（可用 PLAYLIST_DIG_HOME 覆盖）"
                           + ("" if writable else " - 不可写，请设置 PLAYLIST_DIG_HOME")})
    rows.append(_row("临时目录", _writable(Path(tempfile.gettempdir())),
                     tempfile.gettempdir()))
    report["environment"] = rows

    # 环境检查也参与退出码：`mpa doctor` 常被脚本当成闸门用，
    # 缓存目录不可写这类问题必须让它非 0，而不是只打印一行 FAIL。
    exit_code = 0
    if any(not r["ok"] and not r.get("skipped") for r in rows):
        exit_code = 2

    adapters = [a for a in all_adapters() if not provider or a.name == provider]
    rows = []
    for adapter in adapters:
        needs_service = adapter.requires_node or adapter.default_mode.startswith("node")
        detail = f"模式={adapter.default_mode}"
        if needs_service:
            detail += "；需要 node 服务" + ("（可用）" if toolchain.get("node")
                                       else "（缺 node，且不允许自动下载）")
        rows.append({"name": adapter.name, "ok": True, "skipped": False,
                     "detail": detail + "；可选字段: "
                     + (", ".join(k for k, v in adapter.capabilities.items() if v) or "无")
                     + ("；最后实测 " + adapter.verified_on if adapter.verified_on
                        else "；**未实测**：" + (adapter.verified_note or "没有实测记录"))})
    report["adapters"] = rows

    if live:
        rows = []
        for adapter in adapters:
            key = playlist_id or SAMPLES.get(adapter.name)
            if not key:
                rows.append({"name": adapter.name, "ok": True, "skipped": True,
                             "detail": "没有确认可用的公开样例 id（见 "
                                       + adapter.reference + "），不拿猜的 id 去「验证」"})
                continue
            row = _live_check(adapter, key, out_root)
            rows.append(row)
            if not row["ok"] and not row.get("skipped"):
                exit_code = 2
        report["live"] = rows
    report["_exit"] = exit_code
    return report


def _live_check(adapter, key: str, out_root: Path | None) -> dict:
    out = Path(out_root or tempfile.gettempdir()) / f"mpa-doctor-{adapter.name}"
    try:
        result = fetch_playlist(key, out, provider=adapter.name, save_raw=True,
                                min_interval=0.5, reporter=Reporter(quiet=True))
    except MPError as exc:
        return {"name": adapter.name, "ok": False, "skipped": False,
                "detail": f"{exc.kind}: {exc.message}"[:300]}
    pl = result.playlist
    tracks = pl.tracks_ok()
    caps = {k: v for k, v in adapter.capabilities.items()
            if v and k in TRACK_FIELDS}
    # 「声明可用但一条都没有」是最容易被忽略的一类问题：平台说有，实际没给。
    missing = [f for f in caps if coverage(tracks, f)["n"] == 0]
    detail = (f"{pl.ledger.ok}/{pl.ledger.declared if pl.ledger.declared is not None else '?'} 首，"
              f"模式={pl.source.mode}")
    if missing:
        detail += f"；**声明可用但一条都没有**: {', '.join(missing)}"
    return {"name": adapter.name, "ok": not missing, "skipped": False, "detail": detail}


def _row(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "skipped": False, "detail": detail}


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mpa-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
