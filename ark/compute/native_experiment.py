"""Execute, reconcile and cancel one sealed experiment or scientific plot."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta
import fcntl
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import tarfile

from ark import measurement_table
from ark.orchestrator.state import _atomic_write_text
from ark.sandbox import apptainer_bin, sandbox_sif_path


BASE_IMAGE = "python@sha256:ae52c5bef62a6bdd42cd1e8dffef86b9cd284bde9427da79839de7a4b983e7ca"
SCIENCE_IMAGE = "idea2paper-science:py311-1"
IMAGE_RECIPE = f"""FROM {BASE_IMAGE}
RUN python -m pip install --no-cache-dir numpy==2.2.6 matplotlib==3.10.3 scikit-learn==1.7.2
"""
MAX_BYTES = 12 * 1024 * 1024
WIRE_LIMIT = ((MAX_BYTES + 2) // 3) * 4 + 64
PLOT_READ_HEADER = b"results.json read v1\n"
ACTIVE_SLURM = {"PENDING", "RUNNING", "REQUEUED", "REQUEUE_HOLD", "CONFIGURING", "COMPLETING",
                "SUSPENDED", "RESIZING", "SIGNALING", "STAGE_OUT", "STOPPED"}
# No host output directory is mounted in either backend. The trusted wrapper
# returns one bounded result and captures child stdout without log rotation.
RUNNER = r"""
import base64, os, resource, stat, subprocess, sys, tempfile
limit = 12 * 1024 * 1024
limit_fsize = (limit, limit)
def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, limit_fsize)
plot = len(sys.argv) == 2
if plot:
    # Observe reads in the outer worker, independent of the plotting library.
    import ctypes, struct
    IN_ACCESS = 0x00000001
    libc = ctypes.CDLL(None)
    watch = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if watch < 0 or libc.inotify_add_watch(watch, b"/study/results.json", IN_ACCESS) < 0:
        sys.exit("Could not observe reads of the sealed /study/results.json.")
with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
    rc = subprocess.run([sys.executable, "-I", "-B", "/study/main.py"], cwd="/output",
                        stdout=out, stderr=err, preexec_fn=limits).returncode
    if rc:
        err.seek(max(0, err.tell()-1000))
        sys.stderr.buffer.write(err.read(1000))
        if plot:
            sys.stderr.buffer.write(b"\nRead /study/results.json and save the requested figure under /output/.\n")
        sys.exit(rc if rc > 0 else 128-rc)
    if plot:
        try:
            events = os.read(watch, 4096)
        except BlockingIOError:
            events = b""
        os.close(watch)
        # A watch on a single file has zero-length event names.
        if not any(mask & IN_ACCESS for _, mask, _, _ in struct.iter_unpack("iIII", events)):
            sys.exit("Plot did not read /study/results.json. Load the frozen measurements before plotting.")
        fd = os.open("/output/" + sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                sys.exit("Missing or oversized plot")
            payload = b"results.json read v1\n" + base64.b64encode(f.read(limit + 1))
    else:
        out.seek(0)
        payload = out.read(limit+1)
    sys.stdout.buffer.write(payload)
"""


# Only the field selection and labels are embedded. Each worker reads all
# coordinates from its immutable results.json at render time.
MEASURED_PLOT = '''
import json
from functools import reduce
from operator import getitem
from textwrap import fill
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

with open("/study/results.json") as source:
    rows = json.load(source)
for key in spec["rows_pointer"].split("/")[1:]:
    rows = rows[key.replace("~1", "/").replace("~0", "~")]

def values(column):
    return [reduce(getitem, column["path"].split("."), row) for row in rows]

xs = values(spec["x"])
kind = spec["kind"]
categorical = kind == "bar" or isinstance(xs[0], str)
positions = list(range(len(xs))) if categorical else xs
series = spec["series"]
width = 0.76 / len(series)
markers = ("o", "s", "^", "D", "v", "P")
plt.rcParams.update({"text.parse_math": False, "font.size": 9, "pdf.fonttype": 42})
wide = len(series) > 3 or categorical and (len(xs) > 5 or max(len(str(x)) for x in xs) > 12)
fig, ax = plt.subplots(figsize=(6.6, 3.0) if wide else (3.35, 2.6), constrained_layout=True)
for index, column in enumerate(series):
    ys = values(column)
    label = fill(column["label"], 32)
    if kind == "line":
        order = sorted(range(len(xs)), key=positions.__getitem__)
        ax.plot([positions[j] for j in order], [ys[j] for j in order],
                marker=markers[index], markersize=4, linewidth=1.5, label=label)
    else:
        offset = (index - (len(series) - 1) / 2) * width if categorical else 0
        xx = [v + offset for v in positions]
        if kind == "bar":
            ax.bar(xx, ys, width=width, label=label)
        else:
            ax.scatter(xx, ys, marker=markers[index], s=22, label=label)
if categorical:
    ticks = list(range(0, len(xs), max(1, (len(xs) + 9) // 10)))
    labels = [fill(str(xs[i]), 18) for i in ticks]
    rotate = len(ticks) > 6 or any(len(s) > 8 for s in labels)
    ax.set_xticks(ticks, labels, rotation=35 if rotate else 0, ha="right" if rotate else "center")
ax.set_xlabel(fill(spec["x"]["label"], 52))
ax.set_ylabel(fill(spec["y_label"], 38))
ax.grid(axis="y", alpha=0.22)
ax.set_axisbelow(True)
if len(series) > 1:
    ax.legend(frameon=False, fontsize=8)
metadata = {"CreationDate": None, "ModDate": None} if filename.endswith(".pdf") else {}
fig.savefig("/output/" + filename, dpi=300, metadata=metadata)
plt.close(fig)
'''


def _run(args, *, check=True, timeout=60, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"{Path(args[0]).name}: {result.stderr.strip()[-1200:] or result.returncode}")
    return result


def _safe(root, path):
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Expected an unlinked artifact inside this project")
    return path


def _read(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"Expected a bounded regular file: {path.name}")
        data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Artifact exceeded its byte limit")
        return data


def _save(path, obj):
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


@contextlib.contextmanager
def _locked(root):
    runs = _safe(root, root / "auto_research/state/native_runs")
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _safe(root, runs / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield runs


def _docker_image():
    image = os.getenv("IDEA2PAPER_EXPERIMENT_IMAGE") or SCIENCE_IMAGE
    found = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], check=False)
    if found.returncode:
        if image != SCIENCE_IMAGE:
            raise RuntimeError("Configured experiment image is missing from the local Docker daemon.")
        _run(["docker", "build", "--tag", image, "-"], input=IMAGE_RECIPE, timeout=600)
        found = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    return found.stdout.strip()


def _inspect(name):
    result = _run(["docker", "container", "inspect", name, "--format", "{{json .}}"], check=False)
    if result.returncode:
        if "no such" in result.stderr.lower():
            return None
        raise RuntimeError(result.stderr[-1000:])
    info = json.loads(result.stdout)
    if (info["Config"].get("Labels") or {}).get("idea2paper.identity") != name:
        raise RuntimeError("Container identity does not match this experiment")
    return info


def _slurm_ids(name, since=None):
    ids = set()
    since = (datetime.fromisoformat(since) if since else datetime.now()) - timedelta(days=1)
    for command in (["squeue", "-h", "-u", getpass.getuser(), "-n", name, "-o", "%A|%j"],
                    ["sacct", "-X", "-nP", "-u", getpass.getuser(), "-S", since.strftime("%Y-%m-%d"),
                     "--name", name, "-o", "JobIDRaw,JobName%128"]):
        for line in _run(command).stdout.splitlines():
            values = line.strip().split("|")
            if len(values) >= 2 and values[0].isdigit() and values[1] == name:
                ids.add(values[0])
    if len(ids) > 1:
        raise RuntimeError("Multiple Slurm IDs match this submission identity")
    return next(iter(ids), None)


def _job_state(job, name):
    for line in _run(["sacct", "-X", "-nP", "-j", job, "-o", "JobIDRaw,JobName%128,State,ExitCode"]).stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) >= 4 and fields[0] == job:
            if fields[1] != name:
                # Accounting can expose an incomplete allocation row before its
                # name arrives. Never act on this identity or resubmit it.
                continue
            return fields[2].split()[0].rstrip("+"), fields[3]
    return None, None


def _slurm_command(staging, config, name, plot=None):
    apptainer, image = apptainer_bin(), sandbox_sif_path()
    if not apptainer or not Path(image).is_file():
        raise RuntimeError("Slurm experiments require a configured Apptainer science image (ARK_SANDBOX_SIF).")
    run = ["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", f"HOME={Path.home()}",
           f"USER={getpass.getuser()}", apptainer, "exec", "--cleanenv", "--containall", "--no-home", "--no-privs",
           "--net", "--network", "none", "--no-mount", "hostfs,bind-paths,cwd",
           "--bind", f"{staging / 'input'}:/study:ro", "--scratch", "/output", "--pwd", "/output",
           "--env", "MPLCONFIGDIR=/tmp/mpl,OPENBLAS_NUM_THREADS=1,OMP_NUM_THREADS=1",
           image, "python3", "-I", "-B", "/study/runner.py"]
    if plot:
        run.append(plot)
    args = ["sbatch", "--parsable", "--export=NONE", "--job-name", name, "--chdir", str(staging),
            "--output", str(staging / "stdout"), "--error", str(staging / "stderr"),
            "--ntasks=1", "--cpus-per-task", str(config.get("cpus_per_task") or 1),
            "--mem", str(config.get("memory") or "1G"), "--time", str(config.get("time") or "00:10:00")]
    for key, flag in (("partition", "--partition"), ("account", "--account"), ("gres", "--gres")):
        if config.get(key):
            args += [flag, str(config[key])]
    # Redirect in the limited shell so the writer, not slurmstepd, owns the
    # byte limit on NFS. Scheduler startup diagnostics also remain bounded by
    # the wrapper's short argument surface.
    args[args.index("--output") + 1] = "/dev/null"
    args[args.index("--error") + 1] = str(staging / "launcher.stderr")
    bounded = ("ulimit -f 18000; exec " + shlex.join(run)
               + " >" + shlex.quote(str(staging / "stdout"))
               + " 2>" + shlex.quote(str(staging / "stderr")))
    return args + ["--wrap", "exec bash -c " + shlex.quote(bounded)]


def _slurm(staging, config, row, path, plot):
    name, job = row["identity"], row.get("job_id")
    if not job:
        if row.get("submitted_intent"):
            job = _slurm_ids(name, row["created"])
            if not job:
                return "submission_unknown"
        else:
            args = _slurm_command(staging, config, name, plot)
            row["submitted_intent"] = True
            _save(path, row)
            result = _run(args, check=False)
            if result.returncode:
                reason = result.stderr.strip()[-1200:]
                if re.search(r"invalid (account|partition|qos|time|resource)|requested node configuration is not available",
                             reason, re.I):
                    row.update(status="failed", error=reason)
                else:
                    row.update(status="submission_unknown", error=reason)
                _save(path, row)
                if row["status"] == "failed":
                    raise RuntimeError(reason)
                return row["status"]
            job = result.stdout.strip().split(";")[0]
            if not job.isdigit():
                return "submission_unknown"
        row["job_id"] = job
        _save(path, row)
    state, rc = _job_state(job, name)
    if not state:
        return "submitted"
    if state in ACTIVE_SLURM:
        return state.lower()
    if state == "COMPLETED" and rc == "0:0":
        return "computed"
    row.update(status="failed", error=f"Slurm job {job} ended {state} ({rc}): " + _tail(staging / "stderr"))
    _save(path, row)
    raise RuntimeError(row["error"])


def _tail(path):
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 1000))
        return f.read(1000).decode(errors="replace")


def _check_daemon(row, path):
    identity = _run(["docker", "info", "--format", "{{.ID}}"]).stdout.strip()
    if not identity:
        raise RuntimeError("Docker daemon did not provide its identity")
    saved = row.get("docker_daemon")
    if saved and identity != saved:
        raise RuntimeError("Reconnect to the Docker daemon that accepted this experiment")
    if not saved:
        row["docker_daemon"] = identity
        _save(path, row)


def _docker_input(staging, row):
    # Copy over Docker's archive API. The daemon cannot traverse private,
    # root-squashed NFS workspaces. A managed volume also eliminates host binds.
    name = row["identity"] + "-input"
    volume = _run(["docker", "volume", "create", "--label", f"idea2paper.identity={name}", name]).stdout.strip()
    identity = _run(["docker", "volume", "inspect", volume, "--format",
                     '{{index .Labels "idea2paper.identity"}}']).stdout.strip()
    if identity != name:
        raise RuntimeError("Input volume identity does not match")
    helper = _inspect(name)
    if helper is None:
        helper_id = _run(["docker", "create", "--name", name, "--label", f"idea2paper.identity={name}",
                          "--network", "none", "--read-only", "--mount", f"type=volume,src={name},dst=/study",
                          "--entrypoint", "/bin/true", row["image"]]).stdout.strip()
    else:
        helper_id = helper["Id"]
    data = io.BytesIO()
    with tarfile.open(mode="w", fileobj=data) as archive:
        for file in sorted((staging / "input").iterdir()):
            if file.name not in {"main.py", "runner.py", "results.json"}:
                raise ValueError("Unexpected file in sealed experiment inputs")
            content = _read(file)
            entry = tarfile.TarInfo(file.name)
            entry.size = len(content)
            entry.mode = 0o444
            archive.addfile(entry, io.BytesIO(content))
    copied = subprocess.run(["docker", "cp", "-", helper_id + ":/study"], input=data.getvalue(),
                            capture_output=True, timeout=60)
    if copied.returncode:
        raise RuntimeError("Could not stage immutable experiment inputs: " + copied.stderr.decode(errors="replace")[-1000:])
    _run(["docker", "rm", helper_id])
    return name


def _remove_input(row):
    name = row["identity"] + "-input"
    helper = _inspect(name)
    if helper:
        _run(["docker", "rm", helper["Id"]])
    info = _run(["docker", "volume", "inspect", name, "--format", '{{index .Labels "idea2paper.identity"}}'], check=False)
    if info.returncode == 0 and info.stdout.strip() == name:
        _run(["docker", "volume", "rm", name])


def _docker(staging, row, path, plot):
    _check_daemon(row, path)
    name = row["identity"]
    info = _inspect(name)
    if not info:
        if row.get("job_id"):
            raise RuntimeError("Recorded experiment container disappeared before output was captured")
        image = row.get("image") or _docker_image()
        row.update(submitted_intent=True, image=image)
        _save(path, row)
        volume = _docker_input(staging, row)
        args = ["docker", "create", "--name", name, "--label", f"idea2paper.identity={name}", "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--memory", "512m", "--cpus", "1", "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs", "/output:rw,nosuid,nodev,size=32m,mode=1777", "--env", "MPLCONFIGDIR=/tmp/mpl",
                "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
                "--mount", f"type=volume,src={volume},dst=/study,readonly", "--workdir", "/output",
                "--log-driver", "json-file", "--log-opt", "max-size=24m", "--log-opt", "max-file=1", image,
                "timeout", "-k", "2", "240", "python", "-I", "-B", "/study/runner.py"]
        if plot:
            args.append(plot)
        container_id = _run(args).stdout.strip()
        status = "created"
    else:
        container_id, status = info["Id"], info["State"]["Status"]
    if row.get("job_id", container_id) != container_id:
        raise RuntimeError("Experiment container ID changed")
    row["job_id"] = container_id
    _save(path, row)
    if status == "created":
        _run(["docker", "start", container_id])
        return "running"
    if status in ("running", "restarting"):
        return status
    if status == "exited" and info["State"]["ExitCode"] == 0:
        with (staging / "stdout").open("wb") as target:
            subprocess.run(["docker", "logs", container_id], stdout=target, stderr=subprocess.DEVNULL, check=True, timeout=30)
        return "computed"
    row.update(status="failed", cleanup_pending=True, error=f"Container ended {status}: " +
               _run(["docker", "logs", "--tail", "4", container_id], check=False).stderr[-1000:])
    _save(path, row)
    raise RuntimeError(row["error"])


def _publish(root, staging, artifact, plot, row):
    payload = _read(staging / "stdout", WIRE_LIMIT if plot else MAX_BYTES)
    if plot:
        if row.get("plot_protocol") == 1:
            if not payload.startswith(PLOT_READ_HEADER):
                raise ValueError("Worker did not confirm reading the frozen /study/results.json.")
            payload = payload[len(PLOT_READ_HEADER):]
        data = base64.b64decode(payload, validate=True)
        if len(data) > MAX_BYTES:
            raise ValueError("Oversized plot")
        if plot.endswith(".pdf"):
            import fitz
            with fitz.open(stream=data, filetype="pdf") as doc:
                if len(doc) != 1:
                    raise ValueError("A scientific plot must contain one page")
        else:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                if im.format != "PNG":
                    raise ValueError("Plot does not contain PNG bytes")
                im.verify()
    else:
        result = json.loads(payload)
        if not isinstance(result, (dict, list)) or not result:
            raise ValueError("Print a nonempty JSON object or array of measured results")
        data = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if len(data) > MAX_BYTES:
            raise ValueError("Serialized measurements exceeded their byte limit")
        if artifact.exists() and _read(artifact) != data:
            raise ValueError("A different experiment already owns this result path; declare a new filename")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    _write(artifact, data)
    if plot and row.get("plot_protocol") == 1:
        row["input_access"] = "worker_observed_read"
    row.update(status="completed", artifact=str(artifact.relative_to(root)), sha256=hashlib.sha256(data).hexdigest())


def _cleanup_terminal(row, path):
    if row.get("cleanup_pending") and row["backend"] == "local":
        _check_daemon(row, path)
        info = _inspect(row["identity"])
        if info:
            if info["Id"] != row.get("job_id") or info["State"]["Status"] not in ("created", "exited", "dead"):
                raise RuntimeError("Cannot remove a running or different container")
            _run(["docker", "rm", info["Id"]])
        _remove_input(row)
        row.pop("cleanup_pending", None)
        _save(path, row)


def _cancel_one(row, path):
    if row["status"] in {"completed", "cancelled", "failed"}:
        _cleanup_terminal(row, path)
        return
    row["status"] = "cancelling"
    _save(path, row)
    name = row["identity"]
    if row["backend"] == "slurm":
        job = row.get("job_id") or (_slurm_ids(name, row["created"]) if row.get("submitted_intent") else None)
        if job:
            row["job_id"] = job
            state, _ = _job_state(job, name)
            if state in ACTIVE_SLURM or state is None:
                # A known numeric ID without an accounting row might already
                # have completed; verify its unique name before cancelling it.
                if _slurm_ids(name, row["created"]) != job:
                    return
                result = _run(["scancel", job], check=False)
                if result.returncode and "invalid job id" not in result.stderr.lower():
                    raise RuntimeError(result.stderr[-1000:])
                state, _ = _job_state(job, name)
                if state in ACTIVE_SLURM or state is None:
                    return
        elif row.get("submitted_intent"):
            return
    else:
        _check_daemon(row, path)
        info = _inspect(name)
        if info:
            if row.get("job_id", info["Id"]) != info["Id"]:
                raise RuntimeError("Cannot cancel mismatched container identity")
            row["job_id"] = info["Id"]
            if info["State"]["Status"] in ("running", "restarting"):
                _run(["docker", "stop", "--time", "2", info["Id"]])
            _run(["docker", "rm", info["Id"]])
        _remove_input(row)
    row["status"] = "cancelled"
    _save(path, row)


def execute(root, config, plan, script, *, plot=None, selection=None, measurement_sha256=None):
    """Start or poll exactly one operation for a fixed plan, script, and input."""
    root = Path(root).resolve()
    if selection is None:
        if not isinstance(script, str) or not re.fullmatch(r"scripts/[\w-]+\.py", script, flags=re.ASCII):
            raise ValueError("Choose a Python file directly under scripts/")
    elif not plot or script is not None:
        raise ValueError("Choose either measured columns or a custom plotting script.")
    if plot and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(?:pdf|png)", plot):
        raise ValueError("Choose a simple PDF or PNG filename")
    expected = plan["expected"]
    if not isinstance(expected, str) or not re.fullmatch(r"results/[\w-]+\.json", expected, flags=re.ASCII):
        raise ValueError("Expected a JSON file directly under results/")
    backend = config.get("experiment_compute_backend") or config.get("compute_backend") or {"type": "local"}
    kind = backend.get("type", "local")
    if kind not in {"local", "slurm"}:
        raise ValueError(f"Native experiments support Docker and Slurm, received {kind}")
    with _locked(root) as runs:
        measurement = _safe(root, root / expected)
        inputs = _read(measurement) if plot else b""
        if plot and measurement_sha256 and hashlib.sha256(inputs).hexdigest() != measurement_sha256:
            raise ValueError("Measured artifact does not match its worker receipt.")
        if selection is None:
            code = _read(_safe(root, root / script))
        else:
            spec = measurement_table.plot_spec(json.loads(inputs), selection)
            code = (f"spec = {spec!r}\nfilename = {plot!r}\n" + MEASURED_PLOT).encode()
        payload = json.dumps([str(root), plan, kind, plot], sort_keys=True).encode()
        token = hashlib.sha256(payload + b"\0" + code + b"\0" + inputs).hexdigest()[:24]
        staging = _safe(root, runs / token)
        staging.mkdir(mode=0o700, exist_ok=True)
        path = _safe(root, staging / "receipt.json")
        artifact = _safe(root, root / "paper/figures" / plot) if plot else measurement
        if path.exists():
            row = json.loads(_read(path))
            if row.get("identity") != "i2p-" + token or row.get("backend") != kind:
                raise ValueError("Invalid experiment receipt")
        else:
            if not plot and artifact.exists():
                raise ValueError("This result path is already in use; choose a distinct filename")
            directory = _safe(root, staging / "input")
            directory.mkdir(mode=0o700, exist_ok=True)
            _write(_safe(root, directory / "main.py"), code)
            _write(_safe(root, directory / "runner.py"), RUNNER.encode())
            if plot:
                _write(_safe(root, directory / "results.json"), inputs)
            row = {"identity": "i2p-" + token, "backend": kind, "status": "prepared",
                   "created": datetime.now().isoformat(timespec="seconds"),
                   "script": script, "code_sha256": hashlib.sha256(code).hexdigest(),
                   "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
                   "expected": expected}
            if plot:
                row["input_sha256"] = hashlib.sha256(inputs).hexdigest()
                row["plot_protocol"] = 1
                if selection is not None:
                    row["plot_spec"] = spec
            _save(path, row)
        if row["status"] in ("cancelling", "cancelled"):
            _cancel_one(row, path)
            return row
        if row["status"] == "failed":
            _cleanup_terminal(row, path)
            raise RuntimeError(row.get("error", "Experiment previously failed"))
        if row["status"] == "completed":
            if not artifact.is_file() or hashlib.sha256(_read(artifact)).hexdigest() != row["sha256"]:
                raise ValueError("Published artifact changed since this experiment completed")
            _cleanup_terminal(row, path)
            return row
        if row["status"] != "computed":
            state = (_slurm(staging, backend, row, path, plot) if kind == "slurm" else _docker(staging, row, path, plot))
            row["status"] = state
            _save(path, row)
        if row["status"] == "computed":
            try:
                _publish(root, staging, artifact, plot, row)
            except (ValueError, OSError) as exc:
                row.update(status="failed", error=f"Invalid experiment artifact: {exc}", cleanup_pending=kind == "local")
                _save(path, row)
                raise
            if kind == "local":
                row["cleanup_pending"] = True
            _save(path, row)
            _cleanup_terminal(row, path)
        return row


def cancel(root):
    """Record Stop before touching a backend and never restart a cancelled operation."""
    root = Path(root).resolve()
    with _locked(root) as runs:
        for path in runs.glob("*/receipt.json"):
            row = json.loads(_read(_safe(root, path)))
            if row.get("identity") != "i2p-" + path.parent.name:
                raise ValueError("Invalid experiment receipt identity")
            _cancel_one(row, path)


def measurement_receipt(root, plan, code_sha256):
    """Recover the unique measured operation for a sealed plan and script."""
    root = Path(root).resolve()
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    with _locked(root) as runs:
        matched = []
        for path in runs.glob("*/receipt.json"):
            row = json.loads(_read(_safe(root, path)))
            if (row.get("identity") == "i2p-" + path.parent.name and "input_sha256" not in row
                    and row.get("script") == plan.get("script") and row.get("expected") == plan.get("expected")
                    and row.get("plan_sha256") == plan_hash and row.get("code_sha256") == code_sha256):
                matched.append(row)
        if len(matched) > 1:
            raise ValueError("Several measured operations match this plan; inspect their backend identities")
        return matched[0] if matched else None
