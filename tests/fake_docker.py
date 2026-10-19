"""Stand-in for the docker CLI in tests: records each invocation and runs `sh -c <cmd>` on the
host in the mounted workspace. It checks the plumbing, not the isolation."""

import json
import os
import subprocess
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
if argv[0] == "version":
    print("27.0-fake")
    sys.exit(0)
if argv[0] == "kill":
    sys.exit(0)
assert argv[0] == "run" and argv[-3] == "sh" and argv[-2] == "-c", argv
mount = argv[argv.index("-v") + 1]
host_dir = mount.rsplit(":/work", 1)[0]
sys.exit(subprocess.run(argv[-1], shell=True, cwd=host_dir).returncode)
