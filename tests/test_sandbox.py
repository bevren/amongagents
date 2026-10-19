import asyncio
import json
import shutil
import sys
from pathlib import Path

from amongagents.bridge import Session
from amongagents.sandbox import Sandbox
from amongagents.tasks import TaskSpec

ROOT = Path(__file__).resolve().parents[1]
FAKE = [sys.executable, str(Path(__file__).with_name("fake_docker.py"))]


def test_container_is_locked_down(tmp_path):
    sb = Sandbox(kind="docker", image="python:3.12-slim", gpus=True, cpus="4", memory="8g")
    argv = sb.wrap("python eval.py", tmp_path, network=False, name="amongagents-x")
    flag = lambda f: argv[argv.index(f) + 1]  # noqa: E731
    assert argv[:3] == ["docker", "run", "--rm"]
    assert flag("--network") == "none" and flag("--cap-drop") == "ALL"
    assert flag("--security-opt") == "no-new-privileges" and "--read-only" in argv
    assert flag("-v") == f"{tmp_path.resolve()}:/work" and flag("-w") == "/work"
    assert argv.count("-v") == 1  # nothing but the workspace is mounted
    assert flag("--gpus") == "all" and flag("--cpus") == "4" and flag("--memory") == "8g"
    assert argv[-4:] == ["python:3.12-slim", "sh", "-c", "python eval.py"]
    setup = sb.wrap("uv sync", tmp_path, network=True, name="n")  # only setup may use the network
    assert setup[setup.index("--network") + 1] == "bridge"
    assert "--gpus" not in Sandbox(kind="docker").wrap("x", tmp_path, False, "n")


def make_session(tmp_path, monkeypatch, docker=FAKE):
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    ws = tmp_path / "ws"
    shutil.copytree(ROOT / "tasks" / "tsp" / "files", ws)
    s = Session("ws://unused", "R", "alice", ws, trust=True, sandbox=Sandbox(kind="docker", docker=docker))
    s.task = TaskSpec.from_dir(ROOT / "tasks" / "tsp")
    return s


def docker_calls(tmp_path):
    return [json.loads(l) for l in (tmp_path / "docker.log").read_text().splitlines()]


def test_evals_run_through_the_sandbox(tmp_path, monkeypatch):
    s = make_session(tmp_path, monkeypatch)

    async def main():
        assert await s.check_sandbox() is None
        code, out = await s._run(s.task.eval_command, 60)
        assert code == 0 and "score: 18.845409" in out
        code, _ = await s._run("python -c \"import time; time.sleep(30)\"", 1)  # timeout -> container killed
        assert code is None

    asyncio.run(main())
    calls = docker_calls(tmp_path)
    assert calls[0][0] == "version"
    run = calls[1]
    assert run[0] == "run" and run[run.index("--network") + 1] == "none" and run[-1] == "python eval.py"
    name = calls[2][calls[2].index("--name") + 1]
    assert calls[-1] == ["kill", name]


def test_missing_docker_is_reported_clearly(tmp_path, monkeypatch):
    s = make_session(tmp_path, monkeypatch, docker=["definitely-not-docker-xyz"])
    problem = asyncio.run(s.check_sandbox())
    assert problem and "Docker isn't usable" in problem
