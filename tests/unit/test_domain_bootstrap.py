"""Exercise a cancelled first DSH connection with real venv and loopback-only pip."""
import base64
import fcntl
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest


def wheel(directory, name, version, files):
    prefix = f"{name}-{version}.dist-info"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    files = {**files,
             f"{prefix}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
             f"{prefix}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    rows = []
    with zipfile.ZipFile(path, "w") as archive:
        for key, value in files.items():
            data = value.encode()
            archive.writestr(key, data)
            sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            rows.append(f"{key},sha256={sha},{len(data)}")
        archive.writestr(f"{prefix}/RECORD", "\n".join([*rows, f"{prefix}/RECORD,,", ""]))
    return path


def test_killed_launcher_cannot_release_pip_lock_or_remove_an_incomplete_prefix(tmp_path):
    bootstrap = tmp_path / "domain_bootstrap.py"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "integrations/dsh/domain_bootstrap.py", bootstrap)
    companion = tmp_path / "companion"
    companion.mkdir()
    module = ("import sys\nfrom pathlib import Path\n"
              "def main():\n    print(Path(sys.prefix))\n"
              "if __name__ == '__main__':\n    main()\n")
    artifact = wheel(companion, "ark_fixture", "1.0", {
        "ark/orchestrator/native_domain.py": module,
        "ark_fixture-1.0.dist-info/entry_points.txt": "[console_scripts]\ncompanion-check = ark.orchestrator.native_domain:main\n",
    })
    dependency = wheel(tmp_path, "google_genai", "2.0", {"google/genai/__init__.py": ""})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (companion / "manifest.json").write_text(json.dumps({"wheel": artifact.name, "sha256": digest}))
    cache = tmp_path / "cache"
    version = f"py{sys.version_info.major}{sys.version_info.minor}-{digest}"
    old = cache / "idea2paper/companions" / version
    old.mkdir(parents=True)
    orphan = old / "still-open"
    orphan.write_text("An earlier pip owns this prefix")
    downloading, release = threading.Event(), threading.Event()
    fetches = []

    class Repo(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/index.html":
                data = f'<a href="/{dependency.name}">{dependency.name}</a>'.encode()
                content_type = "text/html"
            elif self.path == "/" + dependency.name:
                fetches.append(self.path)
                downloading.set()
                if not release.wait(60):
                    self.send_error(408)
                    return
                data = dependency.read_bytes()
                content_type = "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Repo) as server, orphan.open("rb") as old_handle:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(cache), "PATH": os.environ["PATH"],
               "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_NO_CACHE_DIR": "1",
               "PIP_FIND_LINKS": f"http://127.0.0.1:{server.server_port}/index.html"}
        cmd = [sys.executable, "-I", str(bootstrap)]
        first = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = None
        try:
            assert downloading.wait(35), "First installer never reached the loopback-only wheel"
            first.kill()
            first.wait(timeout=10)
            second = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # The pipe is flushed immediately before the second launcher starts its installer.
            assert b"Setting up" in second.stderr.readline()
            with (old.parent / (version + ".lock")).open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not list(old.parent.glob("*/.ready"))
            release.set()
            stdout, stderr = second.communicate(timeout=45)
            assert second.returncode == 0, stderr.decode(errors="replace")
            installed = Path(stdout.decode().strip())
            assert installed.parent == old.parent and installed != old
            assert (installed / ".ready").read_text() == digest
            entrypoint = installed / "bin/companion-check"
            assert subprocess.check_output([str(entrypoint)], env=env, text=True).strip() == str(installed)
            assert len(fetches) == 1
            assert orphan.read_bytes() == old_handle.read()
        finally:
            release.set()
            for p in (first, second):
                if p is not None:
                    if p.poll() is None:
                        p.kill()
                    p.communicate(timeout=15)
            server.shutdown()
            worker.join(5)
