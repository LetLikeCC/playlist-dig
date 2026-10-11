#!/usr/bin/env python3
"""Re-derive a fixture's ``playlist.json`` from the ``raw/`` already on disk.

Use this **only** when the recorded responses did not change by a single byte and
what changed is how the adapter *interprets* them (a real ``derive`` bug fix: a
field that was being dropped, a wrong unit, a missed key). It is not a way to make
a red fixture green again: it re-runs the real adapter, restores the fetch-level
fields with the same helper ``mpa verify`` uses, writes the file, and then verifies
immediately. Any number that moves is your change showing up, and ``git diff`` will
show it to a reviewer.

If the upstream response itself changed shape, this is the wrong tool: re-fetch
with ``mpa fetch`` and let a new recording (new ``raw/``) be the fixture.

    python3 tools/rederive_fixture.py tests/fixtures/qishui
    python3 tools/rederive_fixture.py tests/fixtures/* --json

Exit codes: 0 = 重算并验证通过，1 = 重算后 verify 不过（别提交）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def rederive(directory: Path) -> dict:
    from mpa.adapters import adapter_by_name
    from mpa.adapters.base import Ctx
    from mpa.export import canonical_json, write_json
    from mpa.pipeline import (LOG_FILE, PLAYLIST_FILE, RAW_DIR, load_dataset,
                              restore_fetch_meta, verify_playlist)
    from mpa.rawstore import RawStore

    before, log = load_dataset(directory)
    store = RawStore(directory / RAW_DIR, write=False)
    parts = store.load_all()
    adapter = adapter_by_name(before.source.provider)
    ctx = Ctx.from_obj(log.get("ctx") or {})
    playlist = adapter.derive(parts, ctx)
    restore_fetch_meta(playlist, log)

    old_ledger = before.ledger.to_obj()
    new_ledger = playlist.ledger.to_obj()
    old_text = (directory / PLAYLIST_FILE).read_text(encoding="utf-8")
    new_obj = playlist.to_obj()
    changed = canonical_json(json.loads(old_text)) != canonical_json(new_obj)

    if changed:
        write_json(directory / PLAYLIST_FILE, new_obj)

    result = verify_playlist(directory, strict=True)
    return {
        "dir": str(directory),
        "provider": before.source.provider,
        "changed": changed,
        "ledger_before": old_ledger,
        "ledger_after": new_ledger,
        "track_count_before": len(before.tracks),
        "track_count_after": len(playlist.tracks),
        "warnings": playlist.warnings,
        "verify_ok": result.ok,
        "checks": result.checks,
        "diffs": result.diffs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从磁盘上的 raw/ 重算 fixture 的 playlist.json")
    ap.add_argument("dirs", nargs="+", help="fixture 目录（含 playlist.json / fetch.log.json / raw/）")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    args = ap.parse_args(argv)

    reports = [rederive(Path(d)) for d in args.dirs]
    if args.json:
        json.dump(reports, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if all(r["verify_ok"] for r in reports) else 1

    for r in reports:
        head = "重算并改写" if r["changed"] else "重算结果与磁盘一致，未改写"
        print(f"[{r['provider']}] {r['dir']}: {head}")
        print(f"    曲目 {r['track_count_before']} -> {r['track_count_after']}"
              f"  ledger {r['ledger_before']} -> {r['ledger_after']}")
        for w in r["warnings"] or []:
            print(f"    警告: {w}")
        for name, ok in r["checks"].items():
            print(f"    [{'ok' if ok else 'FAIL'}] {name}")
        for d in r["diffs"]:
            print(f"    diff: {d}")
    return 0 if all(r["verify_ok"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
