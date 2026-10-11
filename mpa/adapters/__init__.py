"""Adapter registry."""

from __future__ import annotations

from .base import Adapter, Ctx, Ref
from .kugou import KugouAdapter
from .kuwo import KuwoAdapter
from .netease import NeteaseAdapter
from .qishui import QishuiAdapter
from .qq import QQAdapter

__all__ = ["Adapter", "Ctx", "Ref", "all_adapters", "find_adapter", "adapter_by_name",
           "NeteaseAdapter", "QQAdapter", "KuwoAdapter", "KugouAdapter", "QishuiAdapter"]


def all_adapters(**kwargs) -> list[Adapter]:
    """Every adapter, in a stable order. kwargs go to the ones that take them."""
    return [
        NeteaseAdapter(),
        QQAdapter(),
        KuwoAdapter(),
        KugouAdapter(**kwargs),
        QishuiAdapter(**kwargs),
    ]


def find_adapter(raw: str, adapters: list[Adapter] | None = None) -> Adapter:
    """Pick an adapter from the input alone - refusing to guess when ambiguous.

    A bare numeric id exists on 网易云, QQ音乐, 酷我 and 汽水 alike, and the same
    digits can mean four different playlists. Guessing here is how a tool ends up
    fetching the wrong thing and reporting success, so we refuse and say so.
    """
    from ..errors import InputError
    adapters = adapters if adapters is not None else all_adapters()
    matches = [a for a in adapters if a.match(raw)]
    if not matches:
        raise InputError(
            f"没有适配器认识这个输入: {raw[:120]}"
            "（支持 网易云 / QQ音乐 / 酷我 / 酷狗 / 汽水）")
    if len(matches) == 1:
        return matches[0]
    bare_digits = raw.strip().isdigit()
    unambiguous = [a for a in matches
                   if not (bare_digits and a.digits_are_ambiguous)]
    if len(unambiguous) == 1:
        return unambiguous[0]
    names = " / ".join(a.name for a in matches)
    raise InputError(
        f"裸数字 id {raw.strip()!r} 不能确定平台（可能是 {names} 中的任意一个），"
        "请加 --provider 指定；或者直接粘贴带域名的歌单链接。")


def adapter_by_name(name: str, adapters: list[Adapter] | None = None) -> Adapter:
    from ..errors import InputError
    for adapter in adapters or all_adapters():
        if adapter.name == name:
            return adapter
    raise InputError(f"未知的 provider: {name}")
