"""Canonical serialisation and export formats.

The canonical form is ``json.dumps(..., ensure_ascii=False, indent=2)`` with
dataclass field order preserved: stable bytes in, stable diffs out, so a dataset
can live in a git repository and a reviewer can see what actually changed.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from .model import SRC_PLATFORM, TRACK_FIELDS, Playlist, Track

#: 支持的导出格式，全部是数据格式
FORMATS = ("json", "jsonl", "csv", "txt")


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_json(path: Path | str, obj) -> Path:
    """同目录写临时文件再 ``os.replace``：读者要么看到旧内容，要么看到新内容。

    数据集是要进 git、要被人反复复算的东西，半写出来的 JSON 比没有更糟。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(canonical_json(obj), encoding="utf-8")
    os.replace(tmp, path)
    return path


def provenance(track: Track) -> str:
    """Compact 'field=source' map, so exported rows carry their own caveats."""
    bits = []
    for name in TRACK_FIELDS:
        bits.append(f"{name}={getattr(track, name).source}")
    return ";".join(bits)


def extra_cell(track: Track) -> str:
    """``extra`` 压成一格：``key=value;key=value``（键名排序，列表用 ``/`` 连接）。

    CSV 的表头是**固定**的（同一套列在任何平台、任何数据集上都成立），所以不可能
    「每个 extra 键一列」。这里用一列把平台专属字段全带上：键名按字典序排序，保证
    同一份数据每次都一样；列表摊平成 ``a/b``。要看结构化的 ``extra`` 用
    ``--format json`` 或 ``jsonl``；这一列的目标是「打开表格就能按曲风/厂牌筛选」，
    不假装无损（值里本来就可能含 ``/`` 或 ``;``）。
    """
    if not track.extra:
        return ""
    bits = []
    for name in sorted(track.extra):
        field = track.extra[name]
        if not field.known:
            continue
        value = field.value
        if isinstance(value, list):
            value = "/".join(str(v) for v in value)
        bits.append(f"{name}={value}")
    return ";".join(bits)


def _row(track: Track) -> dict:
    return {
        "index": track.index,
        "status": track.status,
        "title": track.title or "",
        "artists": " / ".join(track.artist_names),
        "album": (track.album.name if track.album else "") or "",
        "duration_ms": track.duration_ms.value if track.duration_ms.known else "",
        "language": track.language.value if track.language.known else "",
        "released_at": track.released_at.value if track.released_at.known else "",
        "collected_at": track.collected_at.value if track.collected_at.known else "",
        "id": track.id or "",
        "extra": extra_cell(track),
        "reason": track.reason or "",
        "provenance": provenance(track),
    }


def to_jsonl(playlist: Playlist) -> str:
    buf = io.StringIO()
    for track in playlist.tracks:
        obj = track.to_obj()
        obj["_playlist"] = {"provider": playlist.source.provider,
                            "id": playlist.playlist.id, "title": playlist.playlist.title}
        buf.write(json.dumps(obj, ensure_ascii=False, sort_keys=False) + "\n")
    return buf.getvalue()


def to_csv(playlist: Playlist) -> str:
    buf = io.StringIO()
    fields = list(_row(playlist.tracks[0]).keys()) if playlist.tracks else [
        "index", "status", "title", "artists", "album", "duration_ms", "language",
        "released_at", "collected_at", "id", "extra", "reason", "provenance"]
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for track in playlist.tracks:
        writer.writerow(_row(track))
    return buf.getvalue()


def to_txt(playlist: Playlist, *, limit: int | None = None) -> str:
    """给人看的曲目清单。``limit`` 只截这个清单，数据格式永远给全量。

    截断时末尾写明「共 N 条、只显示前 M 条」——列表被截短却不说，读的人会以为
    这张歌单只有这么多首。
    """
    tracks = playlist.tracks
    shown = tracks if not limit else tracks[:limit]
    lines = []
    for track in shown:
        artists = " / ".join(track.artist_names) or "?"
        if track.ok:
            lines.append(f"{track.index + 1:>4}. {artists} - {track.title}")
        else:
            lines.append(f"{track.index + 1:>4}. [缺失] {track.reason or '不可用'}")
    if len(shown) < len(tracks):
        lines.append(f"…（共 {len(tracks)} 条，只显示前 {len(shown)} 条；"
                     f"`--limit 0` 显示全部）")
    return "\n".join(lines) + "\n" if lines else ""


def export(playlist: Playlist, fmt: str, *, limit: int | None = None) -> str:
    """把数据集渲染成某种**数据**格式。

    只有数据格式（json/jsonl/csv/txt）；要别的排版自己拿这些数据去渲染。

    ``limit`` 只影响 txt 列表，且**默认不截断**（库调用少给数据是更坏的事）；
    命令行上的默认 50 条由 CLI 传入，并在截断处写明「共 N 条」。
    """
    fmt = fmt.lower()
    if fmt == "json":
        return canonical_json(playlist.to_obj())
    if fmt == "jsonl":
        return to_jsonl(playlist)
    if fmt == "csv":
        return to_csv(playlist)
    if fmt == "txt":
        return to_txt(playlist, limit=limit)
    raise ValueError(f"unknown format: {fmt}；可用：{', '.join(FORMATS)}")


def export_to(playlist: Playlist, fmt: str, out: str | Path | None = None, *,
              limit: int | None = None) -> str:
    """Render ``fmt`` and write it to ``out`` unless ``out`` means stdout.

    ``-`` is the usual "write to stdout" convention and is honoured here rather
    than in the CLI alone: writing a 51 KB file literally named ``-`` was what
    ``mpa export --out -`` used to do, and no caller of this function wants that.
    A caller that really wants a file named ``-`` has to pass an absolute path.
    """
    text = export(playlist, fmt, limit=limit)
    if not is_stdout(out):
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def is_stdout(out: str | Path | None) -> bool:
    """``None``, ``""`` and ``"-"`` all mean "hand the text back, write nothing".

    An empty ``--out`` was already treated as "no path" before this function
    existed (the write used to be behind ``if out:``); making it explicit keeps
    that behaviour instead of trying to write a file called ``""``.
    """
    if out is None:
        return True
    return str(out) in ("", "-")
