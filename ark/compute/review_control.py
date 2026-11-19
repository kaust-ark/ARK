"""Run bounded method controls and independent row reproductions in the science image."""
import ast
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time

from ark.compute.native_experiment import apptainer_command

class ReproductionFormatError(ValueError):
    """The program ran but its output cannot represent the requested scalar row."""


_WALL_SECONDS = 30
_OUTPUT_BYTES = 8192


# The two operations have separate inputs. A reproduction receives just its own
# program and row identifiers, with no project directory mounted in either backend.
_LIMITS = """
import contextlib, json, os, resource, sys, tempfile
resource.setrlimit(resource.RLIMIT_CPU, (15, 16))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024*1024, 1024*1024))
resource.setrlimit(resource.RLIMIT_AS, (1536*1024**2, 1536*1024**2))
case = json.load(sys.stdin)
"""

DRIVER = _LIMITS + """
import runpy
with tempfile.TemporaryDirectory() as directory, open(os.devnull, 'w') as quiet:
    os.chdir(directory)
    with open('submission.py', 'w') as f:
        f.write(case['source'])
    with contextlib.redirect_stdout(quiet):
        space = runpy.run_path('submission.py', run_name='reviewed_submission')
        args = case['inputs']
        if case['array_inputs']:
            import numpy
            for name in case['array_inputs']:
                args[name] = numpy.asarray(args[name])
        if case['function']:
            result = space[case['target']](**args)
        else:
            result = eval(compile(case['target'], 'frozen expression', 'eval'), space, args)
        if hasattr(result, 'tolist'):
            result = result.tolist()
    print(json.dumps(result, allow_nan=False))
"""


_REPRODUCTION_DRIVER = _LIMITS + """
with tempfile.TemporaryDirectory() as directory, open(os.devnull, 'w') as quiet:
    os.chdir(directory)
    with contextlib.redirect_stdout(quiet):
        namespace = {'__name__': 'independent_reproduction'}
        exec(compile(case['program'], '<independent reproduction>', 'exec'), namespace)
        result = namespace['reproduce'](case['identifiers'])
        if isinstance(result, dict):
            result = {k: v.item() if getattr(v, 'shape', None) == () else v for k, v in result.items()}
        # Only return valid scalar candidates across the sandbox boundary.
        if not isinstance(result, dict) or not 1 <= len(result) <= 16 or any(
                type(v) not in (int, float) for v in result.values()):
            result = None
    print(json.dumps(result, allow_nan=False))
"""


def _numbers(value):
    items = value if isinstance(value, list) else [value]
    if not 1 <= len(items) <= 32 or any(type(x) not in (int, float) or abs(x) > 1.7976931348623157e308
                                         or not math.isfinite(x) for x in items):
        raise ValueError("Supply one finite number or a flat array of up to 32 finite numbers.")
    return items


def _matches(prediction, result):
    left, right = _numbers(prediction), _numbers(result)
    return (isinstance(prediction, list) == isinstance(result, list) and len(left) == len(right)
            and all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9) for a, b in zip(left, right)))


def _target(source, target, inputs):
    tree = ast.parse(source)
    if any(isinstance(n, ast.FunctionDef) and n.name == target for n in tree.body):
        return True
    # Limit expressions to exactly the calculation in a return or assignment.
    # The reviewer can supply operands but cannot replace its operators or calls.
    try:
        selected = ast.dump(ast.parse(target, mode="eval").body)
    except SyntaxError:
        raise ValueError("Use a top-level function name or quote an exact calculation from the frozen worker.") from None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
            continue
        expr = node.value
        if not isinstance(expr, (ast.Call, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Subscript)):
            continue
        if ast.dump(expr) != selected or ast.get_source_segment(source, expr) != target:
            continue
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        imports = {a.asname or a.name.split(".")[0] for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        definitions = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        calls = {n.func.id for n in ast.walk(expr) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if inputs.keys() <= names and not inputs.keys() & (imports | definitions | calls):
            return False
    raise ValueError("Choose a top-level function name or the exact return/assignment expression from the frozen worker.")


def _execute(backend, image, payload, *, driver=DRIVER):
    if backend == "slurm":
        cmd, image = apptainer_command()
        cmd += ["--pwd", "/tmp", "--env", "OPENBLAS_NUM_THREADS=1,OMP_NUM_THREADS=1",
                image, "timeout", "-k", "2", "22", "python3", "-I", "-B", "-c", driver]
    elif backend == "local" and re.fullmatch(r"sha256:[0-9a-f]{64}", str(image)):
        cmd = ["docker", "run", "--rm", "-i", "--pull=never", "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "32",
               "--memory", "768m", "--cpus", "1", "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777", "--workdir", "/tmp",
               "-e", "OPENBLAS_NUM_THREADS=1", "-e", "OMP_NUM_THREADS=1", "--entrypoint", "timeout",
               image, "-k", "2", "22", "python", "-I", "-B", "-c", driver]
    else:
        raise ValueError("The frozen worker has no supported science runtime for an independent control.")
    # Docker and Apptainer forward the guest's file descriptors to host pipes.
    # Limiting the guest's regular-file size alone cannot bound those streams.
    with tempfile.TemporaryFile() as request, selectors.DefaultSelector() as poll:
        request.write(json.dumps(payload, allow_nan=False).encode())
        request.seek(0)
        try:
            proc = subprocess.Popen(cmd, stdin=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as err:
            raise ValueError("Could not start the isolated science runtime.") from err
        with proc:
            streams = {proc.stdout: bytearray(), proc.stderr: bytearray()}
            for pipe in streams:
                poll.register(pipe, selectors.EVENT_READ)
            deadline = time.monotonic() + _WALL_SECONDS
            try:
                while poll.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, _WALL_SECONDS)
                    for key, _ in poll.select(remaining):
                        part = os.read(key.fd, _OUTPUT_BYTES)
                        if not part:
                            poll.unregister(key.fd)
                            continue
                        buf = streams[key.fileobj]
                        if len(buf) + len(part) > _OUTPUT_BYTES:
                            raise ValueError("The isolated calculation exceeded its output limit.")
                        buf.extend(part)
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, ValueError) as err:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if isinstance(err, subprocess.TimeoutExpired):
                    raise ValueError("The isolated calculation exceeded its time limit.") from None
                raise
            if proc.returncode:
                detail = streams[proc.stderr][-600:].decode(errors="replace")
                raise ValueError("Isolated calculation did not produce a measurement: " + detail)
            try:
                return json.loads(streams[proc.stdout])
            except (ValueError, RecursionError):
                raise ValueError("The isolated calculation did not return a valid JSON result.") from None


def reproduce(runtime, program, identifiers, columns):
    """Recompute the requested row in isolation; the stored values stay on the host."""
    if not isinstance(program, str) or not 1 <= len(program) <= 16000 or len(program.encode()) > 16000:
        raise ValueError("Supply a Python program of at most 16000 bytes defining reproduce(identifiers).")
    if not isinstance(identifiers, dict) or len(identifiers) > 8:
        raise ValueError("Supply at most eight scalar row identifiers.")
    for name, value in identifiers.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError("Use short, named row identifiers.")
        if (isinstance(value, str) and len(value) <= 256) or type(value) is bool:
            continue
        if type(value) not in (int, float):
            raise ValueError("Row identifiers must be finite numbers, booleans or short strings.")
        _numbers(value)
    if (not isinstance(columns, (list, tuple)) or not 1 <= len(columns) <= 16
            or not all(isinstance(name, str) and 1 <= len(name) <= 120 for name in columns)
            or len(set(columns)) != len(columns) or not set(columns).isdisjoint(identifiers)):
        raise ValueError("Choose one to sixteen distinct numeric columns separate from row identifiers.")
    if not isinstance(runtime, dict):
        raise ValueError("Supply the verified worker's science runtime.")
    row = _execute(runtime.get("backend"), runtime.get("image"), {"program": program, "identifiers": identifiers},
                   driver=_REPRODUCTION_DRIVER)
    if not isinstance(row, dict) or row.keys() != set(columns):
        raise ReproductionFormatError("Return one flat dict for a single condition, with keys " +
                                      ", ".join(columns) + " and one finite number per key.")
    for value in row.values():
        if type(value) not in (int, float):
            raise ReproductionFormatError("Return one finite number per column in a single flat dict.")
        _numbers(value)
    return {name: row[name] for name in columns}


def run(source, runtime, args):
    if len(source) > 256_000:
        raise ValueError("The sealed method exceeds the control size limit.")
    target = args.get("target")
    rationale = args.get("rationale")
    if not isinstance(target, str) or not 1 <= len(target) <= 640 or not isinstance(rationale, str) or not 1 <= len(rationale.strip()) <= 1200:
        raise ValueError("Supply the frozen calculation and an independent derivation of its expected answer.")
    def parse(name):
        raw = args.get(name)
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError(f"Supply a small JSON string for {name}.")
        return json.loads(raw)
    inputs, expected = parse("inputs_json"), parse("expected_json")
    if not isinstance(inputs, dict) or not inputs or not all(k.isidentifier() and not k.startswith("__") for k in inputs):
        raise ValueError("Bind named operands or function arguments to a small JSON object.")
    arrays = args.get("array_inputs", [])
    if not isinstance(arrays, list) or not all(isinstance(k, str) and isinstance(inputs.get(k), list) for k in arrays):
        raise ValueError("array_inputs must name list inputs that need NumPy conversion.")
    alternative = parse("alternative_json")
    if _matches(expected, alternative):
        raise ValueError("Both explanations predict the same answer on this input. Choose a distinguishing case.")
    source = source.decode("utf-8")
    function = _target(source, target, inputs)
    result = _execute(runtime["backend"], runtime.get("image"), dict(source=source, function=function,
                      target=target, inputs=inputs, array_inputs=arrays))
    return dict(target=target, inputs=inputs, array_inputs=arrays, rationale=rationale.strip(), expected=expected,
                alternative=alternative, observed=result, matches=_matches(expected, result), backend=runtime["backend"],
                rel_tol=1e-6, abs_tol=1e-9)
