"""Command line interface.

Exit codes are part of the contract, because this tool is meant to run in CI:

    0  ok             全部取到，且数量对得上
    1  partial        取到了数据，但有缺口（差额/不可用/警告）
    2  failed         抓取失败（网络、被拦截、解析失败）
    3  upstream_changed  上游结构变了（契约测试失败，需要改适配器）
    4  usage          输入/参数不对，或目录不是有效的输出目录
    5  route_unavailable  这条路线当前不可用（例如共享页已失效、本地服务起不来）

Only this module calls sys.exit. The library raises typed errors, so it can also
be used as a library without killing the process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .adapters import all_adapters
from .errors import EXIT_OK, EXIT_USAGE, InputError, MPError, exit_code_for
from .export import FORMATS, export_to, is_stdout
from .pipeline import Reporter, fetch_playlist, load_dataset, verify_playlist


class _Parser(argparse.ArgumentParser):
    """把 usage 错误也变成 InputError。

    argparse 默认 `exit(2)`，但本工具的退出码表里 2 是「抓取失败」，参数错误是
    4。同一个程序里两套意思的 2，脚本没法用。
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        raise InputError(f"{message}（用法见 `mpa <命令> --help`）")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="mpa", description="把音乐平台的歌单取成一份可复算的数据（不分析、不猜测）",
        epilog="详细说明见 README.md / docs/schema.md")
    p.add_argument("--version", action="version", version=f"mpa {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="抓取一个歌单并写成数据集目录")
    f.add_argument("input", help="歌单链接或 id")
    f.add_argument("--out", "-o", required=True, help="输出目录")
    f.add_argument("--provider", "-p", help="强制指定平台（默认按输入自动判断）")
    f.add_argument("--no-raw", action="store_true",
                   help="不保存原始响应（会让数据无法离线复算，不建议）")
    f.add_argument("--service-url", help="自建服务地址（酷狗/汽水用），例如 http://127.0.0.1:3210")
    f.add_argument("--service-home", help="自建服务的缓存目录（默认 ~/.cache/playlist-dig）")
    f.add_argument("--no-node", action="store_true", help="不允许自动下载/启动 node 服务")
    f.add_argument("--partial-ok", action="store_true", help="路线不可用（例如汽水服务起不来）时退回到部分数据，而不是整条命令失败；缺口仍然会让退出码变成 1")
    f.add_argument("--timeout", type=float, default=25.0, help="单次请求超时秒数")
    f.add_argument("--min-interval", type=float, default=1.0, help="请求之间的最小间隔秒数")
    f.add_argument("--retries", type=int, default=2, help="网络类失败的重试次数")
    f.add_argument("--json", action="store_true", help="把结果摘要以 JSON 打印到 stdout")
    f.add_argument("--quiet", "-q", action="store_true", help="不打印进度")

    v = sub.add_parser("verify", help="离线复算：用 raw/ 里的原始响应重新推导并比对")
    v.add_argument("dir", help="fetch 的输出目录")
    v.add_argument("--strict", action="store_true", help="适配器版本不同也算失败")
    v.add_argument("--json", action="store_true")

    e = sub.add_parser("export", help="把数据集导出成其他格式")
    e.add_argument("dir")
    e.add_argument("--format", "-f", default="jsonl", choices=list(FORMATS))
    e.add_argument("--out", "-o", help="写入文件；`-` 或省略都表示 stdout")
    e.add_argument("--limit", type=int, default=50,
                   help="txt 列表显示多少条，0 表示全部（默认 50）；数据格式不受影响")

    doc = sub.add_parser("doctor", help="自检：环境、平台连通性、字段可用性")
    doc.add_argument("--provider", "-p", help="只检查某个平台")
    doc.add_argument("--live", action="store_true", help="真的发请求（默认只检查环境）")
    doc.add_argument("--id", help="用指定的歌单 id 做实测")
    doc.add_argument("--json", action="store_true")

    ad = sub.add_parser("adapters", help="列出适配器及其能力")
    ad.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return _dispatch(args)
    except MPError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        for key, value in (exc.context or {}).items():
            print(f"  {key}: {value}", file=sys.stderr)
        if getattr(exc, "retryable", False):
            print("  （这类失败通常可以重试）", file=sys.stderr)
        return exit_code_for(exc)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK


def _dispatch(args) -> int:
    if args.command == "fetch":
        return _fetch(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "export":
        return _export(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "adapters":
        return _adapters(args)
    return EXIT_USAGE


# --- subcommands ------------------------------------------------------------

def _fetch(args) -> int:
    reporter = Reporter(sys.stderr, quiet=args.quiet)
    result = fetch_playlist(
        args.input, args.out, provider=args.provider, save_raw=not args.no_raw,
        timeout=args.timeout, retries=args.retries, min_interval=args.min_interval,
        service_url=args.service_url, service_home=args.service_home,
        allow_provision=not args.no_node, partial_ok=args.partial_ok,
        reporter=reporter)
    pl = result.playlist
    summary = {
        "provider": pl.source.provider,
        "playlist": pl.playlist.to_obj(),
        "ledger": pl.ledger.to_obj(),
        "warnings": pl.warnings,
        "out": str(result.outdir),
        "exit_code": result.exit_code,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"已写入 {result.outdir}/playlist.json", file=sys.stderr)
    return result.exit_code


def _verify(args) -> int:
    result = verify_playlist(args.dir, strict=args.strict)
    if args.json:
        print(json.dumps({"ok": result.ok, "checks": result.checks,
                          "required": list(result.required),
                          "diffs": result.diffs}, ensure_ascii=False, indent=2))
    else:
        for name, ok in result.checks.items():
            # 不参与判定的那几项（默认跑时的适配器版本）只提示，不写成 FAIL——
            # 一边打印 [FAIL] 一边说「验证通过」没人看得懂。
            mark = "ok" if ok else ("FAIL" if name in result.required else "warn")
            print(f"  [{mark}] {name}")
        for d in result.diffs:
            print(f"\n{d}")
        print("验证通过：这条数据的每个字段都能从 raw/ 离线复算出来"
              if result.ok else "验证失败：见上面的差异")
    return EXIT_OK if result.ok else 2


def _export(args) -> int:
    playlist, _ = load_dataset(args.dir)
    text = export_to(playlist, args.format, out=args.out, limit=args.limit or None)
    if is_stdout(args.out):
        sys.stdout.write(text)
    return EXIT_OK


def _doctor(args) -> int:
    from .doctor import run_doctor
    report = run_doctor(provider=args.provider, live=args.live, playlist_id=args.id)
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if not k.startswith("_")},
                         ensure_ascii=False, indent=2))
    else:
        for section, rows in report.items():
            if section.startswith("_") or not isinstance(rows, list):
                continue
            print(f"[{section}]")
            for row in rows:
                mark = "ok" if row.get("ok") else ("skip" if row.get("skipped") else "FAIL")
                print(f"  [{mark:>4}] {row['name']}"
                      + (f" - {row['detail']}" if row.get("detail") else ""))
    return EXIT_OK if report.get("_exit", 0) == 0 else 2


def _adapters(args) -> int:
    rows = []
    for adapter in all_adapters():
        rows.append({"name": adapter.name, "label": adapter.label,
                     "version": adapter.version, "mode": adapter.default_mode,
                     "capabilities": adapter.capabilities,
                     "reference": adapter.reference,
                     "upstream_commit": adapter.upstream_commit,
                     "verified_on": adapter.verified_on,
                     "verified_note": adapter.verified_note})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    for row in rows:
        caps = ", ".join(k for k, v in row["capabilities"].items() if v) or "无"
        print(f"{row['name']:<8} {row['label']:<6} v{row['version']}  "
              f"模式={row['mode']}  可选字段: {caps}")
        if row["upstream_commit"]:
            print(f"{'':<8} 依赖上游 commit {row['upstream_commit']}")
        print(f"{'':<8} 字段说明: {row['reference']}")
        if row["verified_on"]:
            print(f"{'':<8} 最后实测: {row['verified_on']} - {row['verified_note']}")
        else:
            print(f"{'':<8} 最后实测: 未实测 - {row['verified_note']}")
    return EXIT_OK
