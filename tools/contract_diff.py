#!/usr/bin/env python3
"""Compare a fresh fetch against the committed fixture on *structure*, not values.

The canary fetches live data, and live data legitimately changes: 歌单会加歌、点赞数
会动、汽水 SSR 页面每次请求都不一样（同一首《后来》的 ``relation_media`` 两次抓取
一次是「电影《后来的我们》片尾曲」一次是「电视剧《真情》片尾曲」）。所以拿两份
``raw/`` 或两份 ``playlist.json`` 做逐字节比较只会天天报警。

不许无声改变的是**形状**：

* 曲目里原本有的键没了（平台不再返回那个字段）
* 四个规范字段之一从「平台给的」掉成「平台没给」（``language`` 从 platform 变 unavailable）
* ``ledger.declared`` 从数字变成 ``null``（平台不再告诉我们总数 = 无法证明完整）
* 请求计划变了：fixture 里有的 raw part 这次没有（少发了一次请求）
* ``extra`` 里的平台附加字段整批消失

值不在范围内：换了一首歌、时长差一秒、歌曲数变了都不是漂移，也不该让 CI 红。

    python3 tools/contract_diff.py tests/fixtures/netease /tmp/fresh-netease
    python3 tools/contract_diff.py tests/fixtures/* --json   # 不支持，请成对传参

退出码：0 = 形状一致（可能带 warning），1 = 发现漂移，2 = 用法/读取错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def summarize(directory: Path) -> dict:
    from mpa.model import Playlist, TRACK_FIELDS
    from mpa.pipeline import LOG_FILE, PLAYLIST_FILE

    pl_path, log_path = directory / PLAYLIST_FILE, directory / LOG_FILE
    if not pl_path.exists():
        raise FileNotFoundError(f"{pl_path} 不存在")
    playlist = Playlist.from_obj(json.loads(pl_path.read_text(encoding="utf-8")))
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}

    ok = [t for t in playlist.tracks if t.ok]
    key_counts: dict[str, int] = {}
    extra_keys: dict[str, int] = {}
    for t in ok:
        obj = t.to_obj()
        for k in obj:
            key_counts[k] = key_counts.get(k, 0) + 1
        for k in (obj.get("extra") or {}):
            extra_keys[k] = extra_keys.get(k, 0) + 1

    sources: dict[str, dict[str, int]] = {}
    for field in TRACK_FIELDS:
        per: dict[str, int] = {}
        for t in ok:
            f = getattr(t, field)
            per[f.source] = per.get(f.source, 0) + 1
        sources[field] = per

    return {
        "dir": str(directory),
        "schema": playlist.schema,
        "provider": playlist.source.provider,
        "mode": log.get("mode") or playlist.source.mode,
        "adapter_version": playlist.source.adapter_version,
        "ok_tracks": len(ok),
        "tracks": len(playlist.tracks),
        "ledger": playlist.ledger.to_obj(),
        "track_keys": key_counts,
        "extra_keys": extra_keys,
        "field_sources": sources,
        "parts": sorted({r.get("name") for r in (log.get("requests") or []) if r.get("name")}),
        "warnings": playlist.warnings,
    }


def compare(fixture: dict, fresh: dict, *, coverage_drop: float = 0.5) -> dict:
    """Return {"drift": [...], "warnings": [...], "notes": [...]}."""
    drift: list[str] = []
    warn: list[str] = []
    notes: list[str] = []

    if fixture["provider"] != fresh["provider"]:
        drift.append(f"平台不一致：fixture={fixture['provider']} 这次={fresh['provider']}")
    if fresh["schema"] != "mpa/playlist@1":
        drift.append(f"schema 变成了 {fresh['schema']!r}")
    if fixture["mode"] != fresh["mode"]:
        warn.append(f"路线变了：{fixture['mode']} -> {fresh['mode']}")

    fx_dec, new_dec = fixture["ledger"].get("declared"), fresh["ledger"].get("declared")
    if fx_dec is not None and new_dec is None:
        drift.append("ledger.declared 从数字变成 null：平台不再给出总数，"
                     "这份数据无法证明完整（适配器需要改，或确认平台改了接口）")
    if new_dec is None:
        warn.append("这次抓取没有声明总数（ledger.declared=null）")
    gap = fresh["ledger"].get("gap")
    if gap:
        warn.append(f"这次有缺口：gap={gap}（声明 {new_dec}，取到 {fresh['ledger'].get('fetched')}）")

    for key, n in sorted(fixture["track_keys"].items(), key=lambda kv: -kv[1]):
        if key not in fresh["track_keys"]:
            drift.append(f"曲目里的键 {key!r} 整批消失（fixture 里 {n}/{fixture['ok_tracks']} 首有）")
    for key, n in sorted(fresh["track_keys"].items(), key=lambda kv: -kv[1]):
        if key not in fixture["track_keys"]:
            notes.append(f"新增曲目键 {key!r}（这次 {n}/{fresh['ok_tracks']} 首有）")

    for key, n in sorted(fixture["extra_keys"].items(), key=lambda kv: -kv[1]):
        if key not in fresh["extra_keys"]:
            drift.append(f"extra 里的平台附加字段 {key!r} 整批消失"
                         f"（fixture 里 {n}/{fixture['ok_tracks']} 首有）")
    for key in sorted(set(fresh["extra_keys"]) - set(fixture["extra_keys"])):
        notes.append(f"extra 里新增字段 {key!r}")

    for field, per in fixture["field_sources"].items():
        fx_n, new_n = fixture["ok_tracks"] or 1, fresh["ok_tracks"] or 1
        fx_ratio = per.get("platform", 0) / fx_n
        new_ratio = fresh["field_sources"].get(field, {}).get("platform", 0) / new_n
        if fx_ratio >= 0.5 and new_ratio < fx_ratio * coverage_drop:
            drift.append(f"{field} 的平台覆盖率塌了：{fx_ratio:.0%} -> {new_ratio:.0%}")
        elif new_ratio > fx_ratio + 0.2:
            notes.append(f"{field} 的平台覆盖率涨了：{fx_ratio:.0%} -> {new_ratio:.0%}")
        elif abs(new_ratio - fx_ratio) > 0.1:
            warn.append(f"{field} 的平台覆盖率变化：{fx_ratio:.0%} -> {new_ratio:.0%}")

    fx_parts, new_parts = set(fixture["parts"]), set(fresh["parts"])
    for name in sorted(fx_parts - new_parts):
        drift.append(f"请求计划里少了 part {name!r}（少发了一次请求）")
    for name in sorted(new_parts - fx_parts):
        warn.append(f"请求计划里多了 part {name!r}")

    return {"drift": drift, "warnings": warn, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="比较一次新抓取与入库 fixture 的结构（不看具体值）")
    ap.add_argument("fixture", help="入库 fixture 目录")
    ap.add_argument("fresh", help="刚抓出来的输出目录")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印")
    args = ap.parse_args(argv)

    try:
        fx = summarize(Path(args.fixture))
        new = summarize(Path(args.fresh))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    result = compare(fx, new)
    if args.json:
        json.dump({"fixture": fx, "fresh": new, **result}, sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1 if result["drift"] else 0

    print(f"结构比较：{fx['provider']}  fixture={fx['ok_tracks']} 首 这次={new['ok_tracks']} 首")
    for line in result["drift"]:
        print(f"  [漂移] {line}")
    for line in result["warnings"]:
        print(f"  [注意] {line}")
    for line in result["notes"]:
        print(f"  [信息] {line}")
    if not result["drift"]:
        print("  形状一致：平台接口的字段与请求计划都没变")
    return 1 if result["drift"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
