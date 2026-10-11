"""Provisioning for the two upstream community services (KuGou, qishui).

Both are third-party Node projects that reverse-engineer a private API. Running
them is therefore a *last resort* route, and this module exists to bound the
damage the original scripts did not bound:

* node/npm are checked **before** anything is downloaded (the old code checked
  npm only after extracting a tarball);
* the upstream commit is pinned, and the pinned commit is written into the
  dataset so a later reader knows exactly which code produced it;
* pnpm-lock.yaml is honoured with `pnpm install --frozen-lockfile` when pnpm
  exists - `npm install` against a pnpm-lock project silently re-resolves every
  version range upstream, so the resulting dependency tree digest is recorded in
  the dataset as well;
* the service always binds 127.0.0.1 on a random free port, never 0.0.0.0;
* if it cannot be provisioned we raise RouteUnavailable with the manual
  instructions instead of pretending the fetch never happened.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..errors import FetchError, KIND_NETWORK, RouteUnavailable

MANUAL_HINT = (
    "you can also run the service yourself and pass --service-url http://127.0.0.1:<port>"
)


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    repo: str
    commit: str
    entry: str = "app.js"
    health_path: str = "/"

    @property
    def slug(self) -> str:
        return f"{self.name}-{self.commit[:10]}"


@dataclass
class ServiceHandle:
    base_url: str
    mode: str
    upstream_commit: str | None = None
    install_method: str | None = None
    resolved_digest: str | None = None
    managed: bool = False          # True when we started the process
    extra: dict = dc_field(default_factory=dict)


def default_home() -> Path:
    env = os.environ.get("PLAYLIST_DIG_HOME")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "playlist-dig"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def which_all(*names: str) -> dict[str, str | None]:
    return {n: shutil.which(n) for n in names}


def node_toolchain() -> dict[str, str | None]:
    """Look for the toolchain first: never download code we cannot run."""
    return which_all("node", "npm", "pnpm")


class NodeService:
    def __init__(self, spec: ServiceSpec, *, home: Path | str | None = None,
                 service_url: str | None = None, allow_provision: bool = True,
                 ready_timeout: float = 45.0, install_timeout: float = 900.0,
                 log=None):
        self.spec = spec
        self.home = Path(home) if home else default_home()
        self.service_url = service_url.rstrip("/") if service_url else None
        self.allow_provision = allow_provision
        self.ready_timeout = ready_timeout
        self.install_timeout = install_timeout
        self.log = log or (lambda msg: None)
        self.handle: ServiceHandle | None = None
        self._proc: subprocess.Popen | None = None
        self._logpath: Path | None = None

    # -- public --------------------------------------------------------------
    @contextlib.contextmanager
    def ensure(self):
        """Yield a ServiceHandle for the lifetime of the fetch."""
        if self.service_url:
            self._check_reachable(self.service_url)
            self.handle = ServiceHandle(base_url=self.service_url, mode="node-service-external",
                                        managed=False)
            yield self.handle
            return
        if not self.allow_provision:
            raise RouteUnavailable(
                f"{self.spec.name}: node service disabled (--no-node) and no --service-url given; "
                + MANUAL_HINT, context={"provider": self.spec.name})
        tools = node_toolchain()
        if not tools["node"] or not (tools["npm"] or tools["pnpm"]):
            raise RouteUnavailable(
                f"{self.spec.name}: this route needs node + npm (or pnpm); found "
                f"node={tools['node'] or '-'} npm={tools['npm'] or '-'} pnpm={tools['pnpm'] or '-'}. "
                "Install node, or " + MANUAL_HINT,
                context={"provider": self.spec.name, "tools": tools})
        services_dir = self.home / "services"
        target = services_dir / self.spec.slug
        meta = self._prepare(target, tools)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        self._start(target, port)
        try:
            self._wait_ready(base)
        except Exception:
            tail = self._log_tail()
            self._stop()
            raise RouteUnavailable(
                f"{self.spec.name}: service did not become ready on {base} within "
                f"{self.ready_timeout:.0f}s. Last output:\n{tail}",
                context={"provider": self.spec.name, "log": str(self._logpath)})
        self.handle = ServiceHandle(base_url=base, mode="node-service-local", managed=True,
                                    upstream_commit=self.spec.commit, **meta)
        yield self.handle
        self._stop()

    # -- provisioning --------------------------------------------------------
    def _prepare(self, target: Path, tools: dict[str, str | None]) -> dict:
        if (target / "node_modules").is_dir():
            self.log(f"reusing service checkout {target}")
            return {"install_method": "cached", "resolved_digest": self._resolved_digest(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        self._download(target)
        method = self._install(target, tools)
        return {"install_method": method, "resolved_digest": self._resolved_digest(target)}

    def _download(self, target: Path) -> None:
        url = f"https://codeload.github.com/{self.spec.repo}/tar.gz/{self.spec.commit}"
        self.log(f"downloading {self.spec.repo}@{self.spec.commit[:10]} ...")
        req = urllib.request.Request(url, headers={"User-Agent": "playlist-dig"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                blob = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise RouteUnavailable(
                f"{self.spec.name}: could not download {url}: {exc}. " + MANUAL_HINT,
                context={"url": url}) from exc
        tmp = target.with_suffix(".tar.gz")
        tmp.write_bytes(blob)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp) as tf:
            members = tf.getmembers()
            root = members[0].name.split("/")[0] if members else ""
            for m in members:
                if not m.name.startswith(root + "/"):
                    continue
                m.name = m.name[len(root) + 1:]
                if m.name:
                    tf.extract(m, target, filter="data")
        tmp.unlink(missing_ok=True)
        self.log(f"extracted to {target}")

    def _install(self, target: Path, tools: dict[str, str | None]) -> str:
        env = dict(os.environ)
        env.setdefault("npm_config_cache", str(self.home / "npm-cache"))
        env["npm_config_update_notifier"] = "false"
        if tools["pnpm"] and (target / "pnpm-lock.yaml").exists():
            cmd, method = [tools["pnpm"], "install", "--frozen-lockfile"], "pnpm-frozen-lockfile"
        else:
            cmd, method = [tools["npm"] or "npm", "install", "--no-audit", "--no-fund"], \
                "npm-install-unpinned"
            if (target / "pnpm-lock.yaml").exists():
                self.log("WARNING: pnpm-lock.yaml present but pnpm missing - "
                         "npm will re-resolve every version range")
        self.log(f"installing dependencies ({method}) ...")
        proc = subprocess.run(cmd, cwd=target, env=env, capture_output=True,
                              text=True, timeout=self.install_timeout)
        if proc.returncode != 0 or not (target / "node_modules").is_dir():
            tail = (proc.stderr or proc.stdout or "")[-800:]
            raise RouteUnavailable(
                f"{self.spec.name}: dependency install failed ({method}): {tail}. " + MANUAL_HINT,
                context={"provider": self.spec.name, "cmd": cmd})
        return method

    def _resolved_digest(self, target: Path) -> str | None:
        """Hash the *resolved* dependency tree - audits an unpinned install."""
        h = hashlib.sha256()
        lock = target / "package-lock.json"
        if lock.exists():
            h.update(lock.read_bytes())
        mods = target / "node_modules"
        if not mods.is_dir():
            return None
        names = []
        for entry in sorted(mods.iterdir()):
            pkg = entry / "package.json"
            if entry.name.startswith(".") or not pkg.exists():
                continue
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            names.append(f"{data.get('name', entry.name)}@{data.get('version', '?')}")
        h.update("\n".join(names).encode())
        return h.hexdigest()[:32]

    # -- process control -----------------------------------------------------
    def _start(self, target: Path, port: int) -> None:
        self._logpath = self.home / "logs" / f"{self.spec.slug}-{port}.log"
        self._logpath.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({"HOST": "127.0.0.1", "PORT": str(port), "NODE_ENV": "production"})
        node = shutil.which("node") or "node"
        logf = self._logpath.open("ab")
        self._proc = subprocess.Popen([node, self.spec.entry], cwd=target, env=env,
                                      stdout=logf, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL)
        self.log(f"started {self.spec.name} service: pid {self._proc.pid} on 127.0.0.1:{port}")

    def _wait_ready(self, base: str) -> None:
        deadline = time.monotonic() + self.ready_timeout
        url = base + self.spec.health_path
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise FetchError(f"{self.spec.name} service exited with {self._proc.returncode}",
                                 kind=KIND_NETWORK)
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status < 400:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        raise FetchError(f"{self.spec.name} service not ready", kind=KIND_NETWORK)

    def _check_reachable(self, base: str) -> None:
        url = base + self.spec.health_path
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status >= 400:
                    raise RouteUnavailable(f"{self.spec.name}: {url} returned HTTP {resp.status}")
        except (urllib.error.URLError, OSError) as exc:
            raise RouteUnavailable(
                f"{self.spec.name}: nothing answering at {base} ({exc})",
                context={"url": base}) from exc

    def _log_tail(self, lines: int = 12) -> str:
        if not self._logpath or not self._logpath.exists():
            return "(no log)"
        text = self._logpath.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(text[-lines:])

    def _stop(self) -> None:
        if not self._proc:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
