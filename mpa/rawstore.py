"""Raw-response store: the reason this dataset is re-derivable offline.

Layout::

    <outdir>/raw/_manifest.json   list of every request, in order, with sha256
    <outdir>/raw/<name>.json|txt  the response body, as stored (see below)

What "as stored" means, exactly: a ``kind=text`` part is the body verbatim; a
``kind=json`` part is the *parsed* response re-serialised with compact
separators and ``ensure_ascii=False``, because adapters hand this store the
already-decoded object (``{"a": 1}`` and ``{"a":1}`` are the same value, and a
response with numbers like ``1e3`` comes back as ``1000.0``). So the file is
content-equivalent to the response but **not necessarily byte-identical to it**,
and ``entry["bytes"]`` is the size of the stored file. The size that actually
came off the wire is ``fetch.log.json`` -> ``requests[].bytes``; the manifest
and the log are written in the same run, so the two are always comparable.

``mpa verify`` loads these files and re-runs each adapter's *pure* derive step
against them. No network, no node, no clock - so a stored dataset can always be
re-checked, and a parse bug can be fixed years later without refetching.

A missing part is a broken dataset, not a crash: everything here raises
``DatasetError``, which ``mpa/cli.py`` turns into exit code 2 with one line of
Chinese instead of a traceback.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .errors import DatasetError

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
#: manifest ``kind`` -> file extension. One place, because three methods have to
#: agree on it: whoever writes the file, whoever reads it back, and whoever
#: decides a same-stem file is not the recorded body.
_EXT = {"json": "json", "text": "txt"}


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", name)[:120]


class RawStore:
    def __init__(self, directory: Path | str, *, write: bool = True):
        self.dir = Path(directory)
        self.write = write
        self.entries: list[dict] = []
        self._parts: dict[str, Any] = {}
        self._seq = 0

    # -- recording -----------------------------------------------------------
    def record(self, name: str, url: str, payload: Any, *, method: str = "GET",
               status: int | None = None, kind: str | None = None) -> Any:
        name = _safe_name(name)
        if kind is None:
            kind = "json" if isinstance(payload, (dict, list)) else "text"
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if kind == "json" else str(payload))
        blob = body.encode("utf-8")
        self._seq += 1
        self.entries.append({
            "seq": self._seq, "name": name, "method": method, "url": url,
            "status": status, "kind": kind, "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(), "saved_at": _now(),
        })
        self._parts[name] = payload
        if self.write:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{name}.{_EXT[kind]}").write_text(body, encoding="utf-8")
        return payload

    def load_part(self, name: str, kind: str | None = None) -> Any:
        """Read one recorded part back, without going through the manifest.

        ``kind`` comes from the manifest and decides the extension. Without it
        (a hand-written call) the two extensions are probed ``json`` first, which
        is only a convention - two files with the same stem and different
        extensions are exactly what ``stray_files`` reports.
        """
        name = _safe_name(name)
        exts = (_EXT[kind],) if kind in _EXT else ("json", "txt")
        for ext in exts:
            p = self.dir / f"{name}.{ext}"
            if p.exists():
                text = p.read_text(encoding="utf-8")
                try:
                    return json.loads(text) if ext == "json" else text
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise DatasetError(
                        f"raw/{p.name} 读不出来（{exc.__class__.__name__}）："
                        "这份数据集已损坏，重跑一次 mpa fetch 到同一个目录即可自愈",
                        context={"part": name, "file": str(p)}) from exc
        raise DatasetError(
            f"raw/ 里缺 part `{name}`（manifest 记录了这次请求，文件却不在）："
            "这份数据集已损坏，重跑一次 mpa fetch 到同一个目录即可自愈",
            context={"part": name, "looked_for": [f"{name}.{e}" for e in exts]})

    # -- offline loading -----------------------------------------------------
    @classmethod
    def load(cls, directory: Path | str) -> "RawStore":
        store = cls(directory, write=False)
        manifest = store.dir / "_manifest.json"
        if not manifest.exists():
            raise DatasetError(
                f"{manifest} 不存在：这个目录没有原始响应清单，无法离线复算"
                "（是不是把输出目录和数据集目录搞混了？）",
                context={"directory": str(store.dir)})
        store.entries = json.loads(manifest.read_text(encoding="utf-8"))["requests"]
        for entry in store.entries:
            store._parts[entry["name"]] = store.load_part(entry["name"], entry.get("kind"))
        return store

    @property
    def parts(self) -> dict[str, Any]:
        return dict(self._parts)

    def load_all(self) -> dict[str, Any]:
        """Load every recorded part from disk (used by `mpa verify`).

        Without the manifest there is nothing that says which file is which
        part, so this is a broken dataset - not "no parts".  Returning ``{}``
        here made ``verify`` hand an empty dict to ``derive``, which then died
        with a bare ``KeyError`` on the first part it asked for.
        """
        if not self.entries:
            manifest = self.dir / "_manifest.json"
            if not manifest.exists():
                raise DatasetError(
                    f"{manifest} 不存在：raw/ 里有文件却没有清单，无法离线复算"
                    "（是不是把输出目录和数据集目录搞混了？）",
                    context={"directory": str(self.dir)})
            self.entries = json.loads(manifest.read_text(encoding="utf-8"))["requests"]
        for entry in self.entries:
            if entry["name"] not in self._parts:
                self._parts[entry["name"]] = self.load_part(entry["name"], entry.get("kind"))
        return dict(self._parts)

    def check_manifest(self) -> tuple[bool, str]:
        """Re-hash every recorded body and compare with the manifest."""
        manifest = self.dir / "_manifest.json"
        if not manifest.exists():
            return False, f"{manifest} 不存在"
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        entries = recorded.get("requests") or []
        if not self.entries:
            self.entries = entries
        problems: list[str] = []
        for entry in entries:
            # The extension the manifest's ``kind`` implies comes first: if both
            # files exist, hashing the *other* one would silently bless a body
            # that nothing recorded (and derive would read the recorded one).
            want = _EXT.get(entry.get("kind"), "json")
            exts = [want] + [e for e in ("json", "txt") if e != want]
            for ext in exts:
                p = self.dir / f"{entry['name']}.{ext}"
                if not p.exists():
                    continue
                blob = p.read_bytes()
                got = hashlib.sha256(blob).hexdigest()
                if ext != want:
                    problems.append(
                        f"raw/{p.name} 不是 manifest 记录的那一份"
                        f"（manifest 记的是 {entry['name']}.{want}，它不在）")
                elif got != entry["sha256"]:
                    problems.append(f"{p.name} 的 sha256 与 manifest 不符")
                break
            else:
                problems.append(f"raw/{entry['name']}.* 缺失（manifest 里有记录）")
        extra = self.stray_files()
        if extra:
            # A leftover from an earlier recording (part names change when an
            # adapter is rewritten) makes raw/ stop being a faithful record:
            # nobody can tell which file produced playlist.json.
            problems.append("raw/ 里有 manifest 未记录的文件："
                            + ", ".join(extra[:4])
                            + f"{' 等' if len(extra) > 4 else ''}"
                            + "（可能是更早一次抓取留下的，也可能是上一次抓取中途失败了"
                              "——重跑一次 mpa fetch 到同一个目录即可自愈）")
        if recorded.get("digest") != self.digest():
            problems.append("manifest 的 digest 与各项 sha256 不一致")
        if problems:
            return False, "; ".join(problems[:4])
        return True, f"{len(entries)} 个原始响应全部通过 sha256 校验"

    def stray_files(self) -> list[str]:
        """Part stems on disk that the manifest does not account for.

        Two things count as unexplained:

        * a stem no entry mentions - a leftover from an earlier recording;
        * a recorded stem that carries *both* ``.json`` and ``.txt`` - only one of
          them is the body the manifest hashes, so the other is data sitting in
          ``raw/`` with no recorded origin (an adapter that changed a part's kind
          leaves exactly this behind).

        Only ``*.json``/``*.txt`` count: those are the extensions this store
        writes. Hidden files (``.DS_Store`` and friends) are not our business.
        """
        by_ext = {ext: {p.stem for p in self.dir.glob(f"*.{ext}")}
                  for ext in ("json", "txt")}
        bad: set[str] = set()
        for stem in by_ext["json"] | by_ext["txt"]:
            if stem.startswith(".") or stem == "_manifest":
                continue
            entries = [e for e in self.entries if e["name"] == stem]
            if not entries:
                bad.add(stem)
                continue
            want = _EXT.get(entries[-1].get("kind"), "json")
            if {ext for ext in ("json", "txt") if stem in by_ext[ext]} - {want}:
                bad.add(stem)
        return sorted(bad)

    def digest(self) -> str:
        h = hashlib.sha256()
        for e in sorted(self.entries, key=lambda e: e["seq"]):
            h.update(f'{e["seq"]}:{e["name"]}:{e["sha256"]}'.encode())
        return h.hexdigest()

    def total_bytes(self) -> int:
        return sum(e["bytes"] for e in self.entries)

    def finish(self) -> list[str]:
        """Write the manifest; return the stale part files that were removed.

        ``mpa fetch --out DIR`` into a directory that already holds a recording
        of the same playlist must not leave the previous recording's responses
        behind: ``raw/`` has to be exactly what produced this ``playlist.json``.
        """
        if not self.write:
            return []
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "requests": self.entries,
            "count": len(self.entries),
            "bytes": self.total_bytes(),
            "digest": self.digest(),
        }
        (self.dir / "_manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        removed = self.stray_files()
        kind_of = {e["name"]: _EXT.get(e.get("kind"), "json") for e in self.entries}
        for name in removed:
            keep = kind_of.get(name)
            for ext in ("json", "txt"):
                if ext == keep:
                    continue          # 这一次刚写下的那一份，删了 manifest 就成了谎话
                p = self.dir / f"{name}.{ext}"
                if p.exists():
                    p.unlink()
        return removed


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
