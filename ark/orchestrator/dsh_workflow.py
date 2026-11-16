"""Run the hosted workflow as a Cordis plugin of the pinned DSH runtime."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from ark.engines.cli import selected_agent_runtime
from ark.engines.dsh_runtime import SDK_VERSION, dsh_interpreter
from ark.workflow import WorkflowSteps


def hosted_workflow_enabled(project_id):
    choice = os.environ.get("ARK_WORKFLOW_RUNTIME", "").strip().lower()
    if choice not in ("", "python", "dsh"):
        raise ValueError("ARK_WORKFLOW_RUNTIME must be dsh or python")
    if selected_agent_runtime() != "dsh" or choice == "python":
        return False
    return choice == "dsh" or bool(project_id)


def run_workflow(project):
    steps = WorkflowSteps(project)
    with tempfile.TemporaryDirectory(prefix="idea2paper-workflow-") as directory:
        root = Path(directory)
        plugin = root / "workflow.mjs"
        plugin.write_bytes(Path(__file__).with_suffix(".mjs").read_bytes())
        profile = root / "profiles" / "idea2paper-hosted"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(json.dumps({
            "name": "dsh-profile-idea2paper-hosted", "private": True,
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        }))
        (profile / "cordis.patch.yml").write_text(json.dumps([
            {"id": name, "disabled": True} for name in (
                "hmr", "session-log-deepseek", "plugin-package-inventory-deepseek",
                "session-telemetry-otel", "deepseek-account", "llm-deepseek-account",
            )
        ] + [{"insert": [{"id": "idea2paper-hosted", "name": str(plugin)}]}]))
        parent, child = socket.socketpair()
        with parent, child:
            env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR", "XDG_CACHE_HOME")
                   if name in os.environ}
            env.update(DSH_HOME=directory, DSH_TELEMETRY_DISABLED="1", ARK_DSH_WORKFLOW_FD=str(child.fileno()))
            env["PKG_NATIVE_CACHE_PATH"] = os.environ.get("ARK_DSH_PKG_CACHE") or str(
                Path.home() / ".cache" / "idea2paper" / f"dsh-pkg-{SDK_VERSION}")
            proc = subprocess.Popen(
                [dsh_interpreter(), "-c", "from deepseek_harness_runtime import main; main()",
                 "--profile", "idea2paper-hosted"],
                cwd=directory, env=env, stdin=subprocess.DEVNULL, pass_fds=(child.fileno(),),
            )
            child.close()
            try:
                with parent.makefile("rb") as requests:
                    parent.settimeout(45)
                    if requests.readline(512) != b"ready-v1\n":
                        raise RuntimeError("DSH workflow did not start")
                    project.log("DSH hosted workflow started", "INFO")
                    while True:
                        line = requests.readline(512)
                        if line == b"done\n":
                            break
                        action = line.decode("ascii").rstrip("\n")
                        if action not in steps.actions:
                            raise RuntimeError("DSH workflow disconnected or sent an unknown step")
                        outcome = steps.actions[action]()
                        parent.sendall((json.dumps(outcome, allow_nan=False) + "\n").encode())
                if proc.wait(timeout=20) != 0:
                    raise RuntimeError("DSH workflow exited with an error")
            finally:
                parent.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    return steps.iterations_at_start
