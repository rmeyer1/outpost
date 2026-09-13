#!/usr/bin/env python3
"""Tests for the docker / apple container backends (subprocess mocked).

Covers:
- docker command construction (mounts, CA_* env, --rm, --add-host)
- apple command construction (no mounts, -c/-m)
- backend selection by platform and by explicit config / env
- CA_CONTAINER_BIN override
- log-tail and rm -f flows
- docker inspect running-state parse
- docker seed-before-start + collect via bind mounts
- proxy URL rewrite for host.docker.internal

No live Docker daemon is required. Live Docker / Raspberry Pi verification
is pending on a real Docker host.

Usage: python3 tests/test_docker_backend.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SRC, "service"))

import containers  # noqa: E402
import runner as runner_mod  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f" — {detail}" if detail and not cond else ""))


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ------------------------------------------------------------------ selection

check("default darwin -> apple", containers.default_backend("darwin") == "apple")
check("default linux -> docker", containers.default_backend("linux") == "docker")
check("default win32 -> docker", containers.default_backend("win32") == "docker")

check("resolve no config linux -> docker",
      containers.resolve_backend({}, platform="linux") == "docker")
check("resolve no config darwin -> apple",
      containers.resolve_backend({}, platform="darwin") == "apple")
check("resolve None config linux -> docker",
      containers.resolve_backend(None, platform="linux") == "docker")

check("explicit docker on darwin wins",
      containers.resolve_backend({"worker": {"backend": "docker"}},
                                 platform="darwin") == "docker")
check("explicit apple on linux wins",
      containers.resolve_backend({"worker": {"backend": "apple"}},
                                 platform="linux") == "apple")
check("unknown backend ignored, falls to platform",
      containers.resolve_backend({"worker": {"backend": "kvm"}},
                                 platform="linux") == "docker")
check("empty backend falls to platform",
      containers.resolve_backend({"worker": {"backend": ""}},
                                 platform="darwin") == "apple")
check("case-insensitive explicit",
      containers.resolve_backend({"worker": {"backend": "Docker"}},
                                 platform="darwin") == "docker")

old_env = os.environ.get("CA_WORKER_BACKEND")
os.environ["CA_WORKER_BACKEND"] = "docker"
try:
    check("env CA_WORKER_BACKEND=docker on darwin",
          containers.resolve_backend({}, platform="darwin") == "docker")
    check("explicit config beats env",
          containers.resolve_backend({"worker": {"backend": "apple"}},
                                     platform="linux") == "apple")
finally:
    if old_env is None:
        os.environ.pop("CA_WORKER_BACKEND", None)
    else:
        os.environ["CA_WORKER_BACKEND"] = old_env

# --------------------------------------------------------- CA_CONTAINER_BIN

old_bin = os.environ.get("CA_CONTAINER_BIN")
os.environ["CA_CONTAINER_BIN"] = "/tmp/fake-docker"
try:
    check("CA_CONTAINER_BIN wins for docker",
          containers.container_bin("docker") == "/tmp/fake-docker")
    check("CA_CONTAINER_BIN wins for apple",
          containers.container_bin("apple") == "/tmp/fake-docker")
finally:
    if old_bin is None:
        os.environ.pop("CA_CONTAINER_BIN", None)
    else:
        os.environ["CA_CONTAINER_BIN"] = old_bin

apple_bin = containers.container_bin("apple")
check("apple default bin ends with /rt/bin/container",
      apple_bin.endswith("/cloud-agents/rt/bin/container"), apple_bin)

# ------------------------------------------------- docker run construction

calls = []


def capture_run(cmd, **kw):
    calls.append(list(cmd))
    return FakeProc(0, "ok")


tmp = tempfile.mkdtemp(prefix="ca-docker-")
work = Path(tmp) / "work"
out = Path(tmp) / "out"
work.mkdir()
out.mkdir()

env = {
    "CA_JOB_ID": "job_20260913_abc",
    "CA_TYPE": "coding",
    "CA_REPO": "scratch",
    "CA_BASE": "main",
    "CA_BRANCH": "agent/job_20260913_abc",
    "CA_TASK_B64": "dGFzaw==",
    "CA_ENGINE": "hermes",
    "CA_MODEL": "grok-4.6",
    "CA_PROXY_URL": "http://192.168.64.1:19645/v1",
    "CA_CLIENT_TOKEN": "tok",
    "CA_MAX_MINUTES": "30",
}

docker = containers.DockerBackend("/usr/bin/docker", runner=capture_run)
cmd = docker.build_run_cmd(
    name="ca-job-20260913-abc",
    image="ca-worker:latest",
    env=env,
    work_dir=work,
    out_dir=out,
    cpus=2,
    memory="2g",
    label="ca.job=job_20260913_abc",
)

check("docker argv[0] is bin", cmd[0] == "/usr/bin/docker")
check("docker run --rm", cmd[1] == "run" and "--rm" in cmd)
check("docker detached", "-d" in cmd)
check("docker --name", cmd[cmd.index("--name") + 1] == "ca-job-20260913-abc")
check("docker work mount", f"{work.resolve()}:/work" in cmd)
check("docker out mount", f"{out.resolve()}:/out" in cmd)
check("docker -v precedes work mount",
      cmd[cmd.index("-v") + 1].endswith(":/work")
      or cmd[cmd.index(f"{work.resolve()}:/work") - 1] == "-v")
check("docker host-gateway",
      "--add-host" in cmd
      and cmd[cmd.index("--add-host") + 1] == "host.docker.internal:host-gateway")
check("docker --cpus", "--cpus" in cmd and cmd[cmd.index("--cpus") + 1] == "2")
check("docker --memory", "--memory" in cmd and cmd[cmd.index("--memory") + 1] == "2g")
check("docker image last", cmd[-1] == "ca-worker:latest")
check("docker --label", "--label" in cmd
      and cmd[cmd.index("--label") + 1] == "ca.job=job_20260913_abc")

# Every CA_* var is passed with -e KEY=VALUE
missing_e = []
for k, v in env.items():
    pair = f"{k}={v}"
    if pair not in cmd:
        missing_e.append(pair)
    else:
        if cmd[cmd.index(pair) - 1] != "-e":
            missing_e.append(f"-e missing before {pair}")
check("docker -e for every CA_* var", not missing_e, missing_e)
check("docker does not use apple -c", "-c" not in cmd)
check("docker does not use apple -m MEMORY",
      "-m" not in cmd or cmd[cmd.index("-m") + 1] != "2g")

# run_job uses the same argv
calls.clear()
r = docker.run_job(
    name="ca-job-20260913-abc", image="ca-worker:latest", env=env,
    work_dir=work, out_dir=out, cpus=2, memory="2g",
    label="ca.job=job_20260913_abc",
)
check("run_job invokes runner", len(calls) == 1 and calls[0][0] == "/usr/bin/docker")
check("run_job --rm present", "--rm" in calls[0])

# -------------------------------------------------- apple run construction

apple = containers.AppleBackend("/opt/container", runner=capture_run)
acmd = apple.build_run_cmd(
    name="ca-job-20260913-abc",
    image="ca-worker:latest",
    env=env,
    work_dir=work,
    out_dir=out,
    cpus=4,
    memory="8g",
    label="ca.job=job_20260913_abc",
)
check("apple no --rm", "--rm" not in acmd)
check("apple no -v mounts", "-v" not in acmd)
check("apple -c cpus", "-c" in acmd and acmd[acmd.index("-c") + 1] == "4")
check("apple -m memory", "-m" in acmd and acmd[acmd.index("-m") + 1] == "8g")
check("apple still -e CA_*", all(f"{k}={v}" in acmd for k, v in env.items()))
check("apple image last", acmd[-1] == "ca-worker:latest")
check("apple seed_before_start is False", apple.seed_before_start() is False)
check("docker seed_before_start is True", docker.seed_before_start() is True)

# -------------------------------------------------------------- logs + rm

calls.clear()


def rec_run(cmd, **kw):
    calls.append(list(cmd))
    if cmd[1] == "logs":
        return FakeProc(0, "line-a\nline-b\n")
    if cmd[1] == "rm":
        return FakeProc(0, "")
    if cmd[1] == "inspect":
        return FakeProc(0, json.dumps([{
            "State": {"Status": "running", "Running": True},
        }]))
    if cmd[1] == "info":
        return FakeProc(0, "Server Version: 27")
    if cmd[1] == "ps":
        return FakeProc(0, "ca-job-aaa\nnginx\nca-bbb\n")
    return FakeProc(1, "", "nope")


d2 = containers.DockerBackend("docker", runner=rec_run)
lr = d2.logs("ca-job-aaa", tail=300)
check("logs -n 300", calls[-1] == ["docker", "logs", "-n", "300", "ca-job-aaa"])
check("logs stdout captured", lr.stdout == "line-a\nline-b\n")

calls.clear()
lr = d2.logs("ca-job-aaa")
check("logs without -n", calls[-1] == ["docker", "logs", "ca-job-aaa"])

calls.clear()
rr = d2.remove("ca-job-aaa")
check("rm -f", calls[-1] == ["docker", "rm", "-f", "ca-job-aaa"])
check("rm ok", rr.returncode == 0)

check("docker is_running true", d2.is_running("ca-job-aaa") is True)
check("docker system_ready via info", d2.system_ready() is True)
names = d2.list_job_names()
check("docker list filters ca-*", names == ["ca-job-aaa", "ca-bbb"], names)

# inspect not running
def rec_dead(cmd, **kw):
    if cmd[1] == "inspect":
        return FakeProc(0, json.dumps([{
            "State": {"Status": "exited", "Running": False},
        }]))
    return FakeProc(1, "", "")


d3 = containers.DockerBackend("docker", runner=rec_dead)
check("docker is_running false on exited", d3.is_running("x") is False)


def rec_missing(cmd, **kw):
    return FakeProc(1, "", "No such container")


d4 = containers.DockerBackend("docker", runner=rec_missing)
check("docker is_running false on missing", d4.is_running("x") is False)

# Apple inspect schema
def rec_apple_inspect(cmd, **kw):
    return FakeProc(0, json.dumps([{
        "status": {"state": "running"},
    }]))


a2 = containers.AppleBackend("container", runner=rec_apple_inspect)
check("apple is_running true", a2.is_running("ca-x") is True)

# ---------------------------------------------------------- get_backend

b = containers.get_backend({"worker": {"backend": "docker"}},
                           runner=capture_run, platform="darwin")
check("get_backend explicit docker class",
      isinstance(b, containers.DockerBackend) and b.name == "docker")
b = containers.get_backend({"worker": {"backend": "apple"}},
                           runner=capture_run, platform="linux")
check("get_backend explicit apple class",
      isinstance(b, containers.AppleBackend) and b.name == "apple")
b = containers.get_backend({}, runner=capture_run, platform="linux")
check("get_backend linux default docker", b.name == "docker")
b = containers.get_backend({}, runner=capture_run, platform="darwin")
check("get_backend darwin default apple", b.name == "apple")

# ------------------------------------------------ seed + collect (docker)

seed = Path(tmp) / "seed"
seed.mkdir()
(seed / ".git").mkdir()
(seed / "README").write_text("hi")
work2 = Path(tmp) / "work2"
work2.mkdir()
spr = docker.place_seed(name="n", seed_dir=str(seed), work_dir=work2)
check("docker seed copies into work/repo",
      (work2 / "repo" / "README").read_text() == "hi"
      and (work2 / "repo" / ".git").is_dir())
check("docker seed returncode 0", spr.returncode == 0)

(out / "result.json").write_text(json.dumps({"job_id": "job_x", "status": "completed"}))
got = docker.read_result_json(name="n", out_dir=out, scratch_path=Path(tmp) / "chk.json")
check("docker read_result_json from bind mount",
      got and got.get("job_id") == "job_x", got)
docker.discard_result(name="n", out_dir=out)
check("docker discard_result unlinks", not (out / "result.json").exists())
check("docker read_result_json missing -> None",
      docker.read_result_json(name="n", out_dir=out, scratch_path="x") is None)

(out / "result.json").write_text("{}")
(out / "artifacts").mkdir()
(out / "artifacts" / "a.txt").write_text("art")
dest = Path(tmp) / "collect"
cr = docker.collect_out(name="n", out_dir=out, dest=dest)
check("docker collect_out copies bind-mounted /out",
      cr.returncode == 0 and (dest / "artifacts" / "a.txt").read_text() == "art")

# ------------------------------------------------ apple logs/rm via backend

calls.clear()
a3 = containers.AppleBackend("/opt/container", runner=rec_run)
a3.logs("ca-x", tail=50)
check("apple logs argv",
      calls[-1] == ["/opt/container", "logs", "-n", "50", "ca-x"], calls[-1])
a3.remove("ca-x")
check("apple rm -f", calls[-1] == ["/opt/container", "rm", "-f", "ca-x"])

# ---------------------------------------------- proxy rewrite for docker

check("rewrite apple subnet host",
      runner_mod._rewrite_proxy_for_docker("http://192.168.64.1:19645/v1")
      == "http://host.docker.internal:19645/v1")
check("rewrite leaves loopback",
      runner_mod._rewrite_proxy_for_docker("http://127.0.0.1:9/v1")
      == "http://127.0.0.1:9/v1")
check("rewrite leaves host.docker.internal",
      runner_mod._rewrite_proxy_for_docker("http://host.docker.internal:1/v1")
      == "http://host.docker.internal:1/v1")
check("rewrite empty", runner_mod._rewrite_proxy_for_docker("") == "")
check("rewrite leaves OpenRouter https",
      runner_mod._rewrite_proxy_for_docker("https://openrouter.ai/api/v1")
      == "https://openrouter.ai/api/v1")

# --- shipped install files -------------------------------------------------
root = Path(SRC)
check("bin/install.sh exists", (root / "bin" / "install.sh").is_file())
check("service/outpost.service exists",
      (root / "service" / "outpost.service").is_file())
unit = (root / "service" / "outpost.service").read_text()
check("systemd unit starts outpost-service",
      "bin/outpost-service" in unit and "[Service]" in unit)
check("Containerfile is multi-arch python slim",
      "FROM python:3.12-slim" in (root / "images" / "worker" / "Containerfile").read_text())
cfile = (root / "images" / "worker" / "Containerfile").read_text()
check("Containerfile handles amd64 and arm64",
      "amd64" in cfile and "arm64" in cfile)
check("containers.py has no cross-host polling",
      "ssh" not in (root / "service" / "containers.py").read_text().lower()
      and "tailscale" not in (root / "service" / "containers.py").read_text().lower())

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
sys.exit(0)
