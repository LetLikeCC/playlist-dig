"""字段覆盖统计：每个规范字段有多少条有值、这些值是谁给的。

这是**取数侧的诊断**，不是分析：它只回答「平台这次给了哪些字段、给了多少条、值从哪来」，
不做任何推断、不产生任何结论。`mpa doctor --live`、README 的可得性表、契约测试都用它，
所以它必须和 `playlist.json` 的口径完全一致——数字只从数据集里数出来。

四条规范字段的定义在 `mpa/model.TRACK_FIELDS`；平台专属字段在 `Track.extra` 里，
按同一个口径统计（`extra_coverage`）。
"""

from __future__ import annotations

from collections import Counter

from .model import Playlist, Track


def values_of(tracks: list[Track], field: str) -> list:
    """某个规范字段的所有已知取值（平台值或推导值），保持曲目顺序。"""
    out = []
    for track in tracks:
        f = getattr(track, field, None)
        if f is not None and f.known and f.value not in (None, ""):
            out.append(f.value)
    return out


def coverage(tracks: list[Track], field: str) -> dict:
    """`{n, of, share}`：这个字段有多少条有值。"""
    known = [t for t in tracks
             if getattr(t, field, None) is not None and getattr(t, field).known]
    return {"n": len(known), "of": len(tracks),
            "share": round(len(known) / len(tracks), 4) if tracks else None}


def field_sources(tracks: list[Track], field: str) -> dict[str, int]:
    """`{platform: n, derived: n, unavailable: n}`——值是谁给的。

    「平台没给」（`unavailable`）和「平台给了但这一条没有」是两件事，前者在
    `playlist.capabilities` 里另有记录；两者都不算有值。
    """
    counter: Counter = Counter()
    for track in tracks:
        f = getattr(track, field, None)
        counter[f.source if f is not None else "missing"] += 1
    return dict(counter)


def extra_coverage(playlist: Playlist, key: str) -> dict:
    """平台专属字段（`Track.extra[key]`）的覆盖情况。"""
    tracks = playlist.tracks_ok()
    known = [t for t in tracks if key in t.extra and t.extra[key].known]
    return {"n": len(known), "of": len(tracks),
            "share": round(len(known) / len(tracks), 4) if tracks else None,
            "declared": bool(playlist.capabilities.get(key))}


def extra_keys(playlist: Playlist) -> list[str]:
    """这份数据集里出现过的所有专属字段名（去重、排序）。"""
    return sorted({k for track in playlist.tracks for k in track.extra})
