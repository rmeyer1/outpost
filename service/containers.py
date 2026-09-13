"""Container runtime backends for the Outpost runner.

Two backends, selected by config then platform:

  apple  — Apple `container` CLI. Default on darwin.
  docker — Docker Engine. Default on linux.

Explicit ``worker.backend: apple | docker`` in config/agents.yaml always
wins over the platform default. ``CA_WORKER_BACKEND`` is a test/ops
override used only when config does not name a backend.

``CA_CONTAINER_BIN`` overrides the CLI path for either backend.

This module is the only place that builds container CLI argv. The runner
never talks to another Outpost host — each installation is standalone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

BACKENDS = ("apple", "docker")

_APPLE_DEFAULT_BIN = Path.home() / "cloud-agents" / "rt" / "bin" / "container"

RunFn = Callable[..., subprocess.CompletedProcess]


def _default_run(cmd, **kw) -> subprocess.CompletedProcess:
    timeout = kw.pop("timeout", 60)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def default_backend(platform: str | None = None) -> str:
    """Platform default: darwin → apple, anything else → docker."""
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return "apple"
    return "docker"


def resolve_backend(config: dict | None = None, platform: str | None = None) -> str:
    """Pick apple | docker.

    Precedence: explicit config ``worker.backend`` > ``CA_WORKER_BACKEND``
    env > platform default. Unknown values are ignored, not fatal.
    """
    if config:
        named = ((config.get("worker") or {}).get("backend") or "")
        named = str(named).strip().lower()
        if named in BACKENDS:
            return named
    env = (os.environ.get("CA_WORKER_BACKEND") or "").strip().lower()
    if env in BACKENDS:
        return env
    return default_backend(platform)


def container_bin(backend: str | None = None, config: dict | None = None) -> str:
    """CLI binary for the resolved backend.

    ``CA_CONTAINER_BIN`` always wins (ops/test override), matching the
    historical runner.container_bin() contract.
    """
    override = os.environ.get("CA_CONTAINER_BIN")
    if override:
        return override
    kind = backend or resolve_backend(config)
    if kind == "docker":
        return shutil.which("docker") or "docker"
    return str(_APPLE_DEFAULT_BIN)


def get_backend(config: dict | None = None, *, runner: RunFn | None = None,
                platform: str | None = None) -> "ContainerBackend":
    kind = resolve_backend(config, platform=platform)
    bin_path = container_bin(kind, config)
    if kind == "docker":
        return DockerBackend(bin_path, runner=runner)
    return AppleBackend(bin_path, runner=runner)


class ContainerBackend:
    """Operations the runner needs: run, logs, inspect, remove, seed, collect."""

    name: str = ""

    def __init__(self, bin_path: str, runner: RunFn | None = None):
        self.bin = bin_path
        self._run = runner or _default_run

    def run_job(self, *, name: str, image: str, env: dict[str, str],
                work_dir: str | Path, out_dir: str | Path,
                cpus: Any, memory: Any, label: str | None = None) -> subprocess.CompletedProcess:
        cmd = self.build_run_cmd(
            name=name, image=image, env=env,
            work_dir=work_dir, out_dir=out_dir,
            cpus=cpus, memory=memory, label=label,
        )
        return self._run(cmd, timeout=120)

    def build_run_cmd(self, *, name: str, image: str, env: dict[str, str],
                      work_dir: str | Path, out_dir: str | Path,
                      cpus: Any, memory: Any, label: str | None = None) -> list[str]:
        raise NotImplementedError

    def is_running(self, name: str) -> bool:
        r = self._run([self.bin, "inspect", name], timeout=30)
        if r.returncode != 0:
            return False
        return _inspect_is_running(r.stdout, backend=self.name)

    def logs(self, name: str, tail: int | None = None) -> subprocess.CompletedProcess:
        cmd = [self.bin, "logs"]
        if tail is not None:
            cmd += ["-n", str(tail)]
        cmd.append(name)
        return self._run(cmd, timeout=60)

    def remove(self, name: str) -> subprocess.CompletedProcess:
        return self._run([self.bin, "rm", "-f", name], timeout=60)

    def seed_before_start(self) -> bool:
        """True when the host can place the seed tree before `run` (bind mounts)."""
        return False

    def place_seed(self, *, name: str, seed_dir: str, work_dir: str | Path) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def read_result_json(self, *, name: str, out_dir: str | Path,
                         scratch_path: str | Path) -> dict | None:
        """Parsed /out/result.json if present, else None."""
        raise NotImplementedError

    def discard_result(self, *, name: str, out_dir: str | Path) -> None:
        """Drop a stale /out/result.json so the entrypoint can rewrite it."""
        raise NotImplementedError

    def collect_out(self, *, name: str, out_dir: str | Path,
                    dest: str | Path) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def list_job_names(self) -> list[str]:
        raise NotImplementedError

    def system_ready(self) -> bool:
        raise NotImplementedError

    def exec(self, name: str, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
        return self._run([self.bin, "exec", name, *args], timeout=timeout)


class AppleBackend(ContainerBackend):
    """Apple container CLI: no host mounts; seed/collect via `container cp`."""

    name = "apple"

    def build_run_cmd(self, *, name: str, image: str, env: dict[str, str],
                      work_dir: str | Path, out_dir: str | Path,
                      cpus: Any, memory: Any, label: str | None = None) -> list[str]:
        cmd = [self.bin, "run", "-d", "--name", name,
               "-c", str(cpus),
               "-m", str(memory)]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        if label:
            cmd += ["--label", label]
        cmd.append(image)
        return cmd

    def place_seed(self, *, name: str, seed_dir: str, work_dir: str | Path) -> subprocess.CompletedProcess:
        return self._run(
            [self.bin, "cp", seed_dir.rstrip("/") + "/.", f"{name}:/work/repo"],
            timeout=300,
        )

    def read_result_json(self, *, name: str, out_dir: str | Path,
                         scratch_path: str | Path) -> dict | None:
        er = self.exec(name, "test", "-f", "/out/result.json", timeout=15)
        if er.returncode != 0:
            return None
        scratch = Path(scratch_path)
        cr = self._run(
            [self.bin, "cp", f"{name}:/out/result.json", str(scratch)],
            timeout=30,
        )
        if cr.returncode != 0:
            return None
        try:
            return json.loads(scratch.read_text())
        except Exception:
            return {}

    def discard_result(self, *, name: str, out_dir: str | Path) -> None:
        self.exec(name, "rm", "-f", "/out/result.json", timeout=15)

    def collect_out(self, *, name: str, out_dir: str | Path,
                    dest: str | Path) -> subprocess.CompletedProcess:
        dest_p = Path(dest)
        dest_p.mkdir(parents=True, exist_ok=True)
        return self._run(
            [self.bin, "cp", f"{name}:/out/.", str(dest_p)],
            timeout=120,
        )

    def list_job_names(self) -> list[str]:
        try:
            r = self._run([self.bin, "list", "--format", "json"], timeout=30)
        except Exception:
            return []
        if r.returncode != 0:
            return []
        try:
            items = json.loads(r.stdout) if r.stdout.strip() else []
        except Exception:
            return []
        names = [i.get("id", "") for i in items if isinstance(i, dict)]
        return [n for n in names if n.startswith("ca-job-") or n.startswith("ca-")]

    def system_ready(self) -> bool:
        try:
            r = self._run([self.bin, "system", "status"], timeout=15)
        except Exception:
            return False
        return bool(r and r.returncode == 0 and "running" in (r.stdout or "").lower())


class DockerBackend(ContainerBackend):
    """Docker Engine: bind-mount /work and /out, ``--rm``, logs + rm -f."""

    name = "docker"

    def seed_before_start(self) -> bool:
        return True

    def build_run_cmd(self, *, name: str, image: str, env: dict[str, str],
                      work_dir: str | Path, out_dir: str | Path,
                      cpus: Any, memory: Any, label: str | None = None) -> list[str]:
        work = str(Path(work_dir).resolve())
        out = str(Path(out_dir).resolve())
        cmd = [self.bin, "run", "--rm", "-d", "--name", name,
               "-v", f"{work}:/work",
               "-v", f"{out}:/out",
               "--add-host", "host.docker.internal:host-gateway",
               "--cpus", str(cpus),
               "--memory", str(memory)]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        if label:
            cmd += ["--label", label]
        cmd.append(image)
        return cmd

    def place_seed(self, *, name: str, seed_dir: str, work_dir: str | Path) -> subprocess.CompletedProcess:
        dest = Path(work_dir) / "repo"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(seed_dir, dest)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()  # type: ignore[return-value]

    def read_result_json(self, *, name: str, out_dir: str | Path,
                         scratch_path: str | Path) -> dict | None:
        path = Path(out_dir) / "result.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def discard_result(self, *, name: str, out_dir: str | Path) -> None:
        path = Path(out_dir) / "result.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def collect_out(self, *, name: str, out_dir: str | Path,
                    dest: str | Path) -> subprocess.CompletedProcess:
        # Bind-mounted: /out is already dest when dest == out_dir.
        src = Path(out_dir)
        dest_p = Path(dest)
        dest_p.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest_p.resolve() and src.is_dir():
            for item in src.iterdir():
                target = dest_p / item.name
                if item.is_dir():
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()  # type: ignore[return-value]

    def list_job_names(self) -> list[str]:
        try:
            r = self._run(
                [self.bin, "ps", "-a", "--format", "{{.Names}}"],
                timeout=30,
            )
        except Exception:
            return []
        if r.returncode != 0:
            return []
        names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return [n for n in names if n.startswith("ca-job-") or n.startswith("ca-")]

    def system_ready(self) -> bool:
        try:
            r = self._run([self.bin, "info"], timeout=15)
        except Exception:
            return False
        return bool(r and r.returncode == 0)


def _inspect_is_running(stdout: str, backend: str) -> bool:
    """Parse `inspect` JSON from either Apple container or Docker."""
    try:
        info = json.loads(stdout)
    except Exception:
        return True  # inspect worked but schema unknown → assume alive
    item = info[0] if isinstance(info, list) and info else info
    if not isinstance(item, dict):
        return True
    # Docker: State.Status / State.Running
    state_obj = item.get("State") if isinstance(item.get("State"), dict) else None
    if state_obj:
        status = str(state_obj.get("Status") or "").lower()
        if state_obj.get("Running") is True:
            return True
        return status in ("running", "created", "starting")
    # Apple container: status.state
    status = item.get("status") or {}
    state = str(status.get("state") or item.get("state") or "").lower()
    return state in ("running", "created", "starting")
