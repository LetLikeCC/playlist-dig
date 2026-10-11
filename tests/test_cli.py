"""The CLI contract: exit codes, stdout/stderr split, and argument handling.

Everything here runs `main()` in-process.  The exit-code table in `mpa/cli.py`
is what CI scripts are wired to, so it is tested as data, not as a side effect of
running the tool.  Nothing in this file touches the network: `fetch` cases either
fail before the first request (bad provider / ambiguous id) or patch
`mpa.cli.fetch_playlist`, and `doctor --live` for kuwo has no sample id at all.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support as S  # noqa: E402

import mpa.cli as cli  # noqa: E402
from mpa import __version__  # noqa: E402
from mpa.errors import (EXIT_OK, EXIT_PARTIAL, EXIT_UPSTREAM_CHANGED, EXIT_USAGE,  # noqa: E402
                        FetchError, InputError, KIND_NETWORK)

PROVIDERS = S.PROVIDERS


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpa-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        patch = mock.patch.dict(os.environ, {"PLAYLIST_DIG_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    # -- helpers -------------------------------------------------------------
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_exiting(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
        return ctx.exception.code, out.getvalue(), err.getvalue()

    def copied(self, provider) -> Path:
        """A writable copy that outlives the test, so paths stay usable."""
        dest = self.tmp / provider
        if not dest.exists():
            shutil.copytree(S.fixture_dir(provider), dest)
        return dest

    def assert_no_network(self):
        S.install_urlopen(self, lambda request, attempt:
                          self.fail(f"the CLI made a network request: {request.full_url}"))


class TestParserContract(CliCase):
    def test_version_exits_zero_and_prints_the_package_version(self):
        code, out, err = self.run_exiting(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"mpa {__version__}")
        self.assertEqual(err, "")

    def test_no_subcommand_is_a_usage_error(self):
        code, out, err = self.run_cli([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("错误：", err)
        self.assertIn("用法见", err)
        self.assertEqual(out, "")

    def test_an_unknown_subcommand_is_a_usage_error(self):
        code, _, err = self.run_cli(["frobnicate"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("用法见", err)

    def test_a_missing_required_option_is_a_usage_error(self):
        code, _, err = self.run_cli(["fetch", "3778678"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--out", err)

    def test_usage_errors_do_not_exit_with_code_two(self):
        """2 means 'fetch failed'; argparse's default would collide with it."""
        for argv in ([], ["frobnicate"], ["fetch"], ["export"], ["verify", "--nope"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv)[0], EXIT_USAGE)

    def test_every_documented_exit_code_is_stable(self):
        self.assertEqual((EXIT_OK, EXIT_PARTIAL, EXIT_UPSTREAM_CHANGED, EXIT_USAGE),
                         (0, 1, 3, 4))

    def test_dash_o_is_an_alias_for_out(self):
        parsed = cli.build_parser().parse_args(["export", "d", "-o", "f.txt"])
        self.assertEqual((parsed.dir, parsed.out), ("d", "f.txt"))
        parsed = cli.build_parser().parse_args(["fetch", "x", "-o", "d"])
        self.assertEqual(parsed.out, "d")

    def test_the_export_format_is_restricted_to_the_implemented_ones(self):
        code, _, err = self.run_cli(["export", str(self.tmp), "--format", "yaml"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("invalid choice", err)


class TestErrorPresentation(CliCase):
    def test_an_mperror_prints_its_message_and_context_and_returns_its_code(self):
        def boom(*a, **kw):
            raise FetchError("boom", kind=KIND_NETWORK, context={"url": "http://x/"})
        S.patch_attr(self, "mpa.cli.fetch_playlist", boom)
        code, out, err = self.run_cli(["fetch", "3778678", "--out", str(self.tmp / "d")])
        self.assertEqual(code, 2)
        self.assertIn("错误：boom", err)
        self.assertIn("  url: http://x/", err)
        self.assertIn("可以重试", err)
        self.assertEqual(out, "")

    def test_a_non_retryable_error_gets_no_retry_hint(self):
        def boom(*a, **kw):
            raise InputError("nope", context={"why": "test"})
        S.patch_attr(self, "mpa.cli.fetch_playlist", boom)
        code, _, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "d")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("  why: test", err)
        self.assertNotIn("可以重试", err)

    def test_a_keyboard_interrupt_is_130(self):
        def boom(*a, **kw):
            raise KeyboardInterrupt
        S.patch_attr(self, "mpa.cli.fetch_playlist", boom)
        code, _, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "d")])
        self.assertEqual(code, 130)
        self.assertIn("已中断", err)

    def test_a_broken_pipe_is_success_not_a_traceback(self):
        def boom(*a, **kw):
            raise BrokenPipeError
        S.patch_attr(self, "mpa.cli.fetch_playlist", boom)
        code, _, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "d")])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")


class TestFetchCommand(CliCase):
    def fake_result(self, exit_code: int = EXIT_OK):
        playlist = S.playlist("qishui")
        return types.SimpleNamespace(playlist=playlist, outdir=self.tmp / "out",
                                     exit_code=exit_code)

    def test_the_summary_json_carries_the_ledger_and_the_output_directory(self):
        S.patch_attr(self, "mpa.cli.fetch_playlist",
                     lambda *a, **kw: self.fake_result())
        code, out, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "out"),
                                       "--json"])
        self.assertEqual(code, EXIT_OK)
        summary = json.loads(out)
        self.assertEqual(summary["provider"], "qishui")
        self.assertEqual(summary["ledger"]["ok"], 65)
        self.assertEqual(summary["out"], str(self.tmp / "out"))
        self.assertEqual(summary["exit_code"], EXIT_OK)
        self.assertEqual(err, "")

    def test_quiet_stays_quiet_and_the_default_mentions_the_written_file(self):
        S.patch_attr(self, "mpa.cli.fetch_playlist",
                     lambda *a, **kw: self.fake_result())
        code, out, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "out")])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("playlist.json", err)
        self.assertEqual(out, "")
        code, out, err = self.run_cli(["fetch", "x", "--out", str(self.tmp / "out"),
                                       "-q"])
        self.assertEqual((code, out, err), (EXIT_OK, "", ""))

    def test_a_partial_fetch_is_reported_as_exit_code_one(self):
        S.patch_attr(self, "mpa.cli.fetch_playlist",
                     lambda *a, **kw: self.fake_result(EXIT_PARTIAL))
        self.assertEqual(self.run_cli(["fetch", "x", "--out", "d", "-q"])[0], EXIT_PARTIAL)

    def test_the_options_are_forwarded_to_the_pipeline(self):
        seen = {}

        def capture(input_ref, outdir, **kwargs):
            seen["input"] = input_ref
            seen["outdir"] = outdir
            seen.update(kwargs)
            return self.fake_result()
        S.patch_attr(self, "mpa.cli.fetch_playlist", capture)
        code, _, _ = self.run_cli([
            "fetch", "http://x/", "--out", str(self.tmp / "out"), "--provider", "qq",
            "--no-raw", "--partial-ok", "--no-node", "--timeout", "3.5",
            "--retries", "1", "--min-interval", "0", "--service-url", "http://s/",
            "--service-home", str(self.tmp / "sh"), "-q"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(seen["input"], "http://x/")
        self.assertEqual(seen["provider"], "qq")
        self.assertEqual(seen["save_raw"], False)
        self.assertEqual(seen["partial_ok"], True)
        self.assertEqual(seen["allow_provision"], False)
        self.assertEqual(seen["timeout"], 3.5)
        self.assertEqual(seen["retries"], 1)
        self.assertEqual(seen["min_interval"], 0.0)
        self.assertEqual(seen["service_url"], "http://s/")
        self.assertEqual(seen["service_home"], str(self.tmp / "sh"))

    def test_an_unknown_provider_fails_before_any_request(self):
        self.assert_no_network()
        code, _, err = self.run_cli(["fetch", "3778678", "--out", str(self.tmp / "d"),
                                     "--provider", "spotify"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("未知的 provider: spotify", err)
        self.assertFalse((self.tmp / "d").exists())

    def test_an_ambiguous_bare_id_fails_before_any_request(self):
        self.assert_no_network()
        code, _, err = self.run_cli(["fetch", "3778678", "--out", str(self.tmp / "d")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("裸数字 id", err)
        self.assertIn("--provider", err)
        self.assertFalse((self.tmp / "d").exists(), "no dataset for a refused input")


class TestVerifyCommand(CliCase):
    def test_a_recorded_fixture_verifies(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                code, out, err = self.run_cli(["verify", str(self.copied(provider))])
                self.assertEqual(code, EXIT_OK)
                self.assertIn("[ok] schema_ok", out)
                self.assertIn("[ok] raw_digest_ok", out)
                self.assertIn("验证通过", out)
                self.assertEqual(err, "")

    def test_every_gating_check_is_printed(self):
        code, out, _ = self.run_cli(["verify", str(self.copied("kugou"))])
        self.assertEqual(code, EXIT_OK)
        names = [line.split("] ", 1)[1] for line in out.splitlines()
                 if line.startswith("  [")]
        self.assertEqual(names, ["schema_ok", "manifest_ok", "raw_digest_ok",
                                 "adapter_version_ok", "derive_deterministic"])

    def test_json_mode_reports_ok_checks_required_and_diffs(self):
        code, out, _ = self.run_cli(["verify", str(self.copied("qq")), "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"ok", "checks", "required", "diffs"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["diffs"], [])
        self.assertEqual(payload["required"],
                         ["schema_ok", "manifest_ok", "raw_digest_ok",
                          "derive_deterministic"])

    def test_a_tampered_playlist_json_fails_with_exit_code_two(self):
        target = self.copied("qishui")
        playlist_file = target / "playlist.json"
        obj = json.loads(playlist_file.read_text(encoding="utf-8"))
        obj["ledger"]["ok"] = 999
        S.write_json(playlist_file, obj)
        code, out, _ = self.run_cli(["verify", str(target)])
        self.assertEqual(code, 2)
        self.assertIn("[FAIL] derive_deterministic", out)
        self.assertIn("验证失败", out)

    def test_strict_turns_a_version_warning_into_a_failure(self):
        target = self.copied("kugou")
        for name in ("playlist.json", "fetch.log.json"):
            path = target / name
            obj = json.loads(path.read_text(encoding="utf-8"))
            if name == "playlist.json":
                obj["source"]["adapter_version"] = "1.9.9"
            else:
                obj["adapter_version"] = "1.9.9"
            S.write_json(path, obj)
        code, out, _ = self.run_cli(["verify", str(target)])
        self.assertEqual(code, EXIT_OK, "warn only by default")
        self.assertIn("[warn] adapter_version_ok", out)
        self.assertIn("验证通过", out)
        code, out, _ = self.run_cli(["verify", str(target), "--strict"])
        self.assertEqual(code, 2)
        self.assertIn("[FAIL] adapter_version_ok", out)

    def test_a_directory_that_is_not_a_dataset_is_a_usage_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _, err = self.run_cli(["verify", str(empty)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("错误：", err)
        self.assertIn("playlist.json", err)

    def test_a_missing_directory_is_a_usage_error(self):
        code, _, err = self.run_cli(["verify", str(self.tmp / "nope")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("错误：", err)

    def test_a_dataset_with_a_missing_raw_part_is_one_line_and_exit_two(self):
        """Regression: this used to print a traceback out of ``cli.main``.

        ``RawStore.load_part`` raised a bare ``FileNotFoundError``, which the
        ``except MPError`` in ``main`` never saw - so the exit code was whatever
        Python chose and the message was a stack trace.
        """
        broken = self.copied("kugou")
        victim = sorted(p for p in (broken / "raw").glob("*.json")
                        if p.name != "_manifest.json")[0]
        victim.unlink()
        code, out, err = self.run_cli(["verify", str(broken)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("错误：", err)
        self.assertIn(victim.stem, err)
        self.assertNotIn("Traceback", err)

    def test_a_dataset_whose_manifest_was_deleted_is_one_line_and_exit_two(self):
        """The other half of the same regression: no manifest, no ``KeyError``."""
        broken = self.copied("qq")
        (broken / "raw" / "_manifest.json").unlink()
        code, out, err = self.run_cli(["verify", str(broken)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("_manifest.json", err)
        self.assertNotIn("Traceback", err)


class TestExportCommand(CliCase):
    def test_txt_goes_to_stdout_and_writes_no_file(self):
        code, out, err = self.run_cli(["export", str(self.copied("kugou")),
                                       "--format", "txt"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.startswith("   1. "), out[:20])
        self.assertEqual(err, "")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["kugou"])

    def test_out_writes_the_file_and_nothing_to_stdout(self):
        target = self.tmp / "deep" / "out.jsonl"
        code, out, _ = self.run_cli(["export", str(self.copied("qq")), "-f", "jsonl",
                                     "--out", str(target)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out, "")
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 76)
        self.assertEqual(json.loads(lines[0])["_playlist"]["provider"], "qq")

    def test_the_default_format_is_jsonl(self):
        code, out, _ = self.run_cli(["export", str(self.copied("kugou"))])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(out.splitlines()), 50)

    def test_limit_only_affects_the_text_listing(self):
        """`--limit` 只作用于给人看的 txt 列表；数据格式永远给全量。"""
        code, out, _ = self.run_cli(["export", str(self.copied("kugou")),
                                     "--format", "txt", "--limit", "2"])
        self.assertEqual(code, EXIT_OK)
        tracks = [l for l in out.splitlines() if l.strip() and not l.startswith("…")]
        self.assertEqual(len(tracks), 2)
        self.assertIn("共 50 条", out, "截断要说清总数")
        code, out, _ = self.run_cli(["export", str(self.copied("kugou")),
                                     "--format", "jsonl", "--limit", "2"])
        self.assertEqual(len(out.splitlines()), 50, "limit is not a data filter")

    def test_limit_zero_means_every_track(self):
        code, out, _ = self.run_cli(["export", str(self.copied("kugou")),
                                     "--format", "txt", "--limit", "0"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len([l for l in out.splitlines() if l.strip()]), 50)

    def test_every_format_is_reachable_from_the_command_line(self):
        from mpa.export import FORMATS
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                code, out, _ = self.run_cli(["export", str(self.copied("kugou")),
                                             "-f", fmt, "--limit", "1"])
                self.assertEqual(code, EXIT_OK)
                self.assertTrue(out.strip())

    def test_a_dataset_without_raw_data_still_exports(self):
        target = self.copied("qishui")
        shutil.rmtree(target / "raw")
        code, out, _ = self.run_cli(["export", str(target), "-f", "json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["schema"], "mpa/playlist@1")

    def test_a_dash_out_means_stdout_not_a_file_named_dash(self):
        """Regression: ``-o -`` used to create a 51 KB file literally called ``-``."""
        dash = Path("-")
        self.addCleanup(lambda: dash.exists() and dash.unlink())
        code, out, _ = self.run_cli(["export", str(self.copied("kugou")),
                                     "-f", "json", "-o", "-"])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(dash.exists(), "「-」是 stdout 的写法，不该落盘成一个叫 - 的文件")
        self.assertEqual(json.loads(out)["schema"], "mpa/playlist@1")


class TestAdaptersCommand(CliCase):
    def test_json_lists_all_five_adapters_with_their_contract_fields(self):
        code, out, err = self.run_cli(["adapters", "--json"])
        self.assertEqual(code, EXIT_OK)
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows],
                         ["netease", "qq", "kuwo", "kugou", "qishui"])
        for row in rows:
            with self.subTest(name=row["name"]):
                self.assertEqual(row["version"], "2.0.0")
                self.assertTrue(row["mode"])
                self.assertIn("reference", row)
        self.assertEqual(rows[0]["verified_on"], "2026-09-29")
        self.assertTrue(rows[0]["capabilities"]["popularity"])
        self.assertEqual(rows[2]["verified_on"], "2026-10-03")
        self.assertIn("id 不记录", rows[2]["verified_note"])
        self.assertEqual(err, "")

    def test_text_mode_prints_a_line_per_adapter(self):
        code, out, _ = self.run_cli(["adapters"])
        self.assertEqual(code, EXIT_OK)
        for name in ("netease", "qq", "kuwo", "kugou", "qishui"):
            self.assertIn(name, out)
        self.assertIn("依赖上游 commit", out)
        # 2026-10-03 起五个平台都有实测记录（酷我首次端到端跑通），所以「未实测」
        # 不该再出现；那一支由下一条测试用假适配器覆盖。
        self.assertEqual(out.count("最后实测: "), 5, "五个适配器各一行（kuwo 没有 fixture，但有实测记录）")
        self.assertNotIn("未实测", out)

    def test_an_adapter_without_a_verified_day_is_printed_as_unverified(self):
        """`verified_on is None` 那一支仍然可达：用假适配器把它钉住。"""
        class NeverVerified(S.ToyAdapterBase):
            name = "never"
            label = "从未实测的平台"
            verified_on = None
            verified_note = "手头没有可用的样例 id，因此从未跑通过"

        with mock.patch("mpa.cli.all_adapters", return_value=[NeverVerified()]):
            code, out, _ = self.run_cli(["adapters"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("最后实测: 未实测 - 手头没有可用的样例 id，因此从未跑通过", out)


class TestDoctorCommand(CliCase):
    def test_the_environment_check_runs_offline(self):
        self.assert_no_network()
        code, out, err = self.run_cli(["doctor"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("[environment]", out)
        self.assertIn("缓存目录可写", out)
        self.assertIn("[adapters]", out)
        self.assertEqual(err, "")
        self.assertTrue(self.home.is_dir(), "the probe ran in the patched home")

    def test_every_row_is_marked_ok_skip_or_fail(self):
        code, out, _ = self.run_cli(["doctor"])
        self.assertEqual(code, EXIT_OK)
        rows = [line for line in out.splitlines() if line.startswith("  [")]
        self.assertTrue(rows, "doctor prints one indented row per check")
        marks = {line[line.index("[") + 1:line.index("]")].strip() for line in rows}
        self.assertTrue(marks <= {"ok", "skip", "FAIL"}, marks)
        self.assertIn("ok", marks)

    def test_json_mode_hides_the_private_exit_key(self):
        code, out, _ = self.run_cli(["doctor", "--json"])
        self.assertEqual(code, EXIT_OK)
        report = json.loads(out)
        self.assertNotIn("_exit", report)
        self.assertIn("environment", report)

    def test_live_without_a_sample_id_skips_the_platform(self):
        """kuwo has no confirmed public id, so --live must not invent one."""
        self.assert_no_network()
        code, out, _ = self.run_cli(["doctor", "--provider", "kuwo", "--live"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("没有确认可用的公开样例 id", out)
        # 「跳过」与「验过」在终端上都用 `[  ok]` 打印（`[skip]` 只留给「本机缺工具」
        # 那一类，见下一条测试）。所以这里只断言 live 段里那一行是 ok、不是 FAIL，
        # 而不是断言「整个输出里没有 [skip]」——那取决于本机装没装 pnpm。
        live_section = out.split("[live]", 1)[1]
        kuwo_line = next(line for line in live_section.splitlines() if "kuwo" in line)
        self.assertIn("[  ok]", kuwo_line)
        self.assertNotIn("FAIL", kuwo_line)

    def test_a_missing_tool_is_marked_skip_and_does_not_fail_the_run(self):
        """`[skip]` 不是死代码：干净的 runner 上没有 pnpm，那一行就是它。

        这条在 macOS 上曾经被漏掉（本机装了 pnpm，`[skip]` 从不出现），
        是 GitHub runner 上第一次跑 CI 才暴露出来的——doctor 的行语义因此写明：
        `ok=False, skipped=True` = 不适用，不计入退出码；`ok=False, skipped=False` = FAIL。
        """
        fake = {"environment": [{"name": "pnpm", "ok": False, "skipped": True,
                                 "detail": "未安装（酷狗需要它来跑本地服务）"},
                                {"name": "python", "ok": True, "skipped": False,
                                 "detail": "3.13"}],
                "adapters": [], "live": [], "_exit": 0}
        S.patch_attr(self, "mpa.doctor.run_doctor", lambda **kw: fake)
        code, out, _ = self.run_cli(["doctor"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("[skip] pnpm", out)
        self.assertIn("[  ok] python", out)
        self.assertNotIn("[FAIL]", out)

    def test_a_provider_filter_only_shows_that_provider(self):
        code, out, _ = self.run_cli(["doctor", "--provider", "qq", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([r["name"] for r in json.loads(out)["adapters"]], ["qq"])

    def test_a_failing_live_check_is_exit_code_two(self):
        fake = {"environment": [{"name": "python", "ok": True, "skipped": False,
                                 "detail": "3"}],
                "adapters": [], "live": [{"name": "qq", "ok": False, "skipped": False,
                                          "detail": "network: boom"}],
                "_exit": 2}
        S.patch_attr(self, "mpa.doctor.run_doctor", lambda **kw: fake)
        code, out, _ = self.run_cli(["doctor", "--live"])
        self.assertEqual(code, 2)
        self.assertIn("[FAIL]", out)
        self.assertIn("network: boom", out)

    def test_a_skipped_live_check_still_exits_zero(self):
        fake = {"environment": [], "adapters": [],
                "live": [{"name": "kuwo", "ok": True, "skipped": True,
                          "detail": "no sample"}], "_exit": 0}
        S.patch_attr(self, "mpa.doctor.run_doctor", lambda **kw: fake)
        code, out, _ = self.run_cli(["doctor", "--live"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no sample", out)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_an_unwritable_cache_directory_fails_the_environment_check(self):
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o700)
        with mock.patch.dict(os.environ,
                             {"PLAYLIST_DIG_HOME": str(blocked / "nested")}):
            code, out, _ = self.run_cli(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("[FAIL]", out)
        self.assertIn("不可写", out)


if __name__ == "__main__":
    unittest.main()
