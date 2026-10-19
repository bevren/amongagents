"""Run task commands in a locked-down container instead of directly on the player's machine.

Everything that executes code someone else wrote goes through here: the room creator's setup,
eval and holdout commands, and through them any teammate's solver that arrived via
`sync_baseline` or `checkout`. The container gets:

    - no network (setup is the exception: it may need to download dependencies)
    - only the workspace mounted (read-write at /work); nothing else from the host
    - a read-only root filesystem, no Linux capabilities, no privilege escalation
    - process, CPU, and memory limits; GPUs only when the task asks for them
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Sandbox:
    kind: str = "none"  # "none" | "docker"
    image: str = "python:3.12-slim"
    gpus: bool = False
    cpus: str | None = None
    memory: str | None = "8g"
    pids: int = 1024
    docker: list[str] = field(default_factory=lambda: ["docker"])  # overridable for tests

    @property
    def enabled(self) -> bool:
        return self.kind == "docker"

    def new_name(self) -> str:
        return f"amongagents-{uuid.uuid4().hex[:12]}"

    def wrap(self, cmd: str, cwd: Path, network: bool, name: str) -> list[str]:
        """The `docker run` argv that executes `cmd` in `cwd` inside the sandbox."""
        args = [
            *self.docker, "run", "--rm", "--name", name,
            "--network", "bridge" if network else "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,exec,size=2g",
            "--pids-limit", str(self.pids),
            # HOME lives in the workspace so what setup caches there (e.g. datasets) survives into
            # the eval containers; /tmp is scratch space that disappears with each container
            "-e", "HOME=/work/.home", "-e", "UV_CACHE_DIR=/work/.cache/uv", "-e", "PIP_CACHE_DIR=/work/.cache/pip",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{Path(cwd).resolve()}:/work", "-w", "/work",
        ]
        if self.gpus:
            args += ["--gpus", "all"]
        if self.cpus:
            args += ["--cpus", self.cpus]
        if self.memory:
            args += ["--memory", self.memory]
        if os.name != "nt" and hasattr(os, "getuid"):  # files created in /work stay owned by the player
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        return [*args, self.image, "sh", "-c", cmd]

    def kill(self, name: str) -> list[str]:
        return [*self.docker, "kill", name]

    def check(self) -> list[str]:
        return [*self.docker, "version", "--format", "{{.Server.Version}}"]
