#!/usr/bin/env python3
"""Write ``provenance.json`` for a fixture directory.

A fixture is only trustworthy if a reader can tell where it came from. This
turns a real ``mpa fetch`` output directory into the sidecar that tests and
``tools/gen_readme_numbers.py`` rely on::

    python3 tools/make_provenance.py tests/fixtures/kugou \
        --note "酷狗 collection_3_2132041455_14_0，经本地 KuGouMusicApi @9b6a779"

``--kind`` exists for the case where a provider cannot be reached at all and the
fixture had to be synthesised: such a fixture must never be presented as a real
recording, so it is labelled ``synthetic`` and the README table says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_NOTE = ("真实抓取记录：raw/ 是原样保存的响应，playlist.json 由 derive 生成；"
                "verify 可离线复算。")


def build(directory: Path, kind: str, note: str) -> dict:
    from mpa.model import Playlist
    from mpa.pipeline import LOG_FILE, PLAYLIST_FILE

    playlist = Playlist.from_obj(json.loads((directory / PLAYLIST_FILE).read_text(encoding="utf-8")))
    log = json.loads((directory / LOG_FILE).read_text(encoding="utf-8"))
    ledger = playlist.ledger
    return {
        "provider": playlist.source.provider,
        "fixture_kind": kind,
        "input": playlist.source.input,
        "ref": log.get("ref"),
        "mode": playlist.source.mode,
        "adapter_version": playlist.source.adapter_version,
        "upstream_commit": playlist.source.upstream_commit,
        "fetched_at": playlist.source.fetched_at,
        "declared": ledger.declared,
        "fetched": ledger.fetched,
        "ok": ledger.ok,
        "unavailable": ledger.unavailable,
        "requests": log.get("requests") or [],
        "raw_digest": playlist.source.raw_digest,
        "note": note,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dir", help="mpa fetch 的输出目录，同时也是 fixture 目录")
    parser.add_argument("--kind", default="recorded-live",
                        choices=["recorded-live", "synthetic"],
                        help="recorded-live=真跑出来的；synthetic=手写的")
    parser.add_argument("--note", default=None, help="一句话说明这份记录是怎么来的")
    parser.add_argument("--check", action="store_true",
                        help="只检查现有 provenance.json 是否与输出目录一致")
    args = parser.parse_args()

    directory = Path(args.dir)
    explicit_note = args.note is not None
    obj = build(directory, args.kind, args.note or DEFAULT_NOTE)
    path = directory / "provenance.json"
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if not explicit_note and current:
            # 没给 --note 时只核对来源本身，不因为 note 的措辞不同就报错。
            old = json.loads(current)
            old.pop("note", None)
            obj.pop("note", None)
            current = json.dumps(old, ensure_ascii=False, indent=2) + "\n"
            text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
        if current != text:
            print(f"{path} 与输出目录不一致：请运行 tools/make_provenance.py {directory}",
                  file=sys.stderr)
            return 1
        print(f"{path} 一致")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
