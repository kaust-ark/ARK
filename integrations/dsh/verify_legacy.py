import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("Usage: IDEA2PAPER_PYTHON=<python> python verify_legacy.py <0.1.0-alpha.1.tgz>")
bundle = Path(sys.argv[1]).resolve(strict=True)
dsh = shutil.which("dsh")
if not dsh:
    raise SystemExit("DSH 0.2.1-rc.2 must be on PATH")
scratch = tempfile.TemporaryDirectory(prefix="idea2paper-dsh-test-")
root = Path(scratch.name)
env = {
    "HOME": str(root),
    "PATH": os.environ["PATH"],
    "DSH_HOME": str(root / "dsh"),
    "DSH_TELEMETRY_DISABLED": "1",
    "DEEPSEEK_API_KEY": "local-fixture-token",
    "IDEA2PAPER_PYTHON": os.environ.get("IDEA2PAPER_PYTHON", sys.executable),
}
if "PYTHONPATH" in os.environ:
    env["PYTHONPATH"] = os.environ["PYTHONPATH"]

requests = []
fixture = {'mode': 'success'}
roles = ('plan', 'code', 'writer', 'reviewer', 'revision', 'final-review')
plan = {
    'question': 'How many keys do sequential and binary search inspect?',
    'hypothesis': 'Binary search inspects fewer keys for absent targets.',
    'method': 'Build a sorted list, then count inspected keys for each algorithm on absent targets.',
    'metric': 'Keys inspected per query',
}
script = '''import json
rows = []
for size in (32, 128, 1024):
    values = list(range(size))
    target = -1
    baseline = 0
    for value in values:
        baseline += 1
        if value == target:
            break
    proposed = 0
    lo, hi = 0, size - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        proposed += 1
        if values[mid] == target:
            break
        if values[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    rows.append(dict(label=f'missing target, size {size}', baseline=baseline, proposed=proposed))
print(json.dumps(dict(rows=rows)))'''

draft = {
    'title': 'Comparison Costs for Sequential and Binary Search',
    'abstract': 'We compare the inspection cost of sequential and binary search on ordered arrays. Deterministic boundary and missing queries illustrate how the behavior depends on the target and the structure of the algorithm.',
    'method': 'Both algorithms receive the same ordered array and the same target. The sequential procedure checks items from the beginning until it finds a match or exhausts the array. The binary procedure inspects a midpoint and removes the half that cannot contain the target. Each inspected key contributes to the comparison count. The input generator constructs sorted, evenly spaced keys and chooses existing and absent targets in advance. All observations in the table come from the executable experiment.',
    'interpretation': 'The comparison counts show that sequential search can be competitive for an item near the beginning. For targets near the end or outside the available keys, sequential search checks the entire array. Binary search can discard a portion of the candidate positions with each unsuccessful midpoint check. These observations illustrate the difference in behavior on the tested inputs. The measured quantity is inspection count and it does not establish a difference in elapsed time or memory traffic.',
    'limitations': 'The ordered inputs and target selection are deliberately simple and represent a narrow study. The experiment does not measure preprocessing, memory locality, branch prediction, or application workloads. A different distribution of targets could favor another search policy. The results describe the selected examples under the stated count definition. General claims about runtime performance require a broader experiment with a realistic workload.',
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = json.dumps(body.get('messages', []))
        role = next((r for r in roles if f'Idea2Paper role: {r}\\n' in prompt), None)
        requests.append((role, self.path))
        if fixture['mode'] == 'hold' and role == 'writer':
            fixture['writer'].set()
            fixture['release'].wait(25)
        if fixture['mode'] == 'quota' and role == 'writer':
            data = b'{"error":{"message":"Insufficient Balance","type":"insufficient_balance_error"}}'
            self.send_response(402)
            self.send_header('Content-Type','application/json')
        else:
            assert role and not body.get('tools'), prompt[-1600:]
            if role == 'plan':
                answer = plan
            elif role == 'code':
                code = ('import time\n' + 'time.sleep(7)\n' + script
                        if fixture['mode'] == 'hold-experiment' else script)
                answer = {'code': code}
            elif role in ('writer', 'revision'):
                if fixture['mode'] == 'revise' and role == 'revision':
                    assert 'The draft reports a wall time that was not measured.' in prompt
                if fixture['mode'] == 'format' and 'failed validation: Invalid abstract' not in prompt:
                    answer = {**draft, 'abstract': 'Too short.'}
                else:
                    answer = {**draft, 'title': draft['title'].replace(' for ', ' – ')}
            else:
                accepted = fixture['mode'] != 'reject'
                unsupported = ['The draft reports a wall time that was not measured.'] if (
                    fixture['mode'] == 'revise' and role == 'reviewer'
                ) else []
                answer = {'accepted': accepted, 'feedback': (
                    'The reported conclusions match the measured comparison counts.' if accepted
                    else 'The result interpretation needs a narrower conclusion.'
                ), 'unsupported_claims': unsupported}
            assert self.path.endswith('/messages'), self.path
            frames = [
                {'type':'message_start','message':{'id':'fixture','model':body['model'],'usage':{'input_tokens':10,'output_tokens':0}}},
                {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text': (
                    '{' if fixture['mode'] == 'json' and role == 'writer' and
                    'Expected a complete JSON response' not in prompt else json.dumps(answer)
                )}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':20}},
                {'type':'message_stop'},
            ]
            data = ''.join(f"event: {f['type']}\ndata: {json.dumps(f)}\n\n" for f in frames).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

def run(*args, expected=0):
    p = subprocess.run(
        [dsh, '--profile', 'idea2paper', *args], cwd=root, env=env,
        capture_output=True, text=True, timeout=70,
    )
    print('run', args, 'exit', p.returncode, p.stdout, p.stderr, flush=True)
    assert p.returncode == expected, p.stderr
    return p

def state(project):
    return json.loads((root / project / 'workflow.json').read_text())

def audit(project):
    path = root / project
    assert (path / 'paper/main.tex').is_file()
    assert (path / 'paper/main.pdf').read_bytes().startswith(b'%PDF')
    assert (path / 'auto_research/state/delivery_report.yaml').is_file()
    assert 'hard_violations: 0' in (path / 'auto_research/state/delivery_report.yaml').read_text()
    rows = json.loads((path / 'results/experiment.json').read_text())['rows']
    assert [(r['baseline'], r['proposed']) for r in rows] == [(32, 5), (128, 7), (1024, 10)]

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
env['DEEPSEEK_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
try:
    subprocess.run([dsh,'plugin','--profile','idea2paper','add',str(bundle)],env=env,cwd=root,check=True)
    run('run', 'first', 'Compare search inspection counts')
    assert state('first')['completed'] == ['plan', 'code', 'experiment', 'writer', 'reviewer', 'revision', 'final-review', 'delivery']
    audit('first')
    assert [r for r, _ in requests] == ['plan', 'code', 'writer', 'reviewer'], requests
    run('resume', 'first')
    assert len(requests) == 4
    assert 'Comparison Costs - Sequential' in (root / 'first' / 'paper' / 'main.tex').read_text()

    fixture.update(mode='hold', writer=threading.Event(), release=threading.Event())
    p = subprocess.Popen([dsh,'--profile','idea2paper','run','interrupted','Compare search inspection counts'],
                         cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        if not fixture['writer'].wait(25):
            p.kill()
            raise AssertionError(p.communicate(timeout=5))
        assert state('interrupted')['completed'] == ['plan', 'code', 'experiment']
        results = root/'interrupted/results/experiment.json'
        before = results.stat().st_mtime_ns, results.read_bytes()
        concurrent = run('resume', 'interrupted', expected=1)
        assert 'already running' in concurrent.stderr
        p.send_signal(signal.SIGINT)
        fixture['release'].set()
        assert p.wait(timeout=10) == 130
    finally:
        fixture['release'].set()
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
    fixture['mode'] = 'success'
    run('resume','interrupted')
    audit('interrupted')
    assert (results.stat().st_mtime_ns, results.read_bytes()) == before
    assert [r for r,_ in requests[4:]] == ['plan', 'code', 'writer', 'writer', 'reviewer']

    fixture['mode'] = 'hold-experiment'
    p = subprocess.Popen(
        [dsh, '--profile', 'idea2paper', 'run', 'during-experiment', 'Compare search inspection counts'],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    name = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if p.poll() is not None:
                raise AssertionError(p.communicate())
            checkpoint = root / 'during-experiment' / 'workflow.json'
            if checkpoint.exists():
                snapshot = state('during-experiment')
                name = 'idea2paper-' + snapshot['id']
                inspect = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.Status}} {{.State.StartedAt}}', name],
                    capture_output=True, text=True, check=False,
                )
                if inspect.stdout.startswith('running '):
                    started = inspect.stdout.split()[1]
                    break
            time.sleep(0.2)
        else:
            raise AssertionError('Experiment did not start')
        p.kill()
        p.wait(timeout=10)
        assert state('during-experiment')['completed'] == ['plan', 'code']
        fixture['mode'] = 'success'
        # Preserve the fixture container until its original start timestamp is verified.
        inspection = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.StartedAt}}', name],
            capture_output=True, text=True, check=True,
        )
        assert inspection.stdout.strip() == started
        recovered = run('resume', 'during-experiment')
        assert f'Recovering experiment container {name}' in recovered.stdout
        audit('during-experiment')
        assert not (root / 'during-experiment' / 'paper' / 'main.pdf').is_symlink()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
        if name:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)

    fixture['mode'] = 'revise'
    start = len(requests)
    run('run', 'revised', 'Compare search inspection counts')
    audit('revised')
    assert [role for role, _ in requests[start:]] == list(roles)

    fixture['mode'] = 'format'
    start = len(requests)
    run('run', 'corrected', 'Compare search inspection counts')
    audit('corrected')
    assert [role for role, _ in requests[start:]] == ['plan', 'code', 'writer', 'writer', 'reviewer']
    fixture['mode'] = 'json'
    start = len(requests)
    run('run', 'malformed', 'Compare search inspection counts')
    audit('malformed')
    assert [role for role, _ in requests[start:]] == ['plan', 'code', 'writer', 'writer', 'reviewer']

    fixture['mode'] = 'reject'
    run('run','rejected','Compare search inspection counts',expected=1)
    assert state('rejected')['completed'][-1] == 'final-review'
    assert not (root/'rejected/paper/main.pdf').exists()
    fixture['mode'] = 'quota'
    count = len(requests)
    run('run','quota','Compare search inspection counts',expected=1)
    assert [role for role, _ in requests[count:]] == ['plan', 'code', 'writer']
    assert state('quota')['completed'] == ['plan', 'code', 'experiment']
    results = root / 'quota' / 'results' / 'experiment.json'
    before = results.stat().st_mtime_ns, results.read_bytes()
    fixture['mode'] = 'success'
    run('resume', 'quota')
    audit('quota')
    assert [role for role, _ in requests[count:]] == ['plan', 'code', 'writer', 'writer', 'reviewer']
    assert (results.stat().st_mtime_ns, results.read_bytes()) == before
    print('PASS: packed bundle, native model turns, real PDFs, SIGINT and crash resume, project lock, revision, format correction, rejection, quota',flush=True)
finally:
    server.shutdown()
    server.server_close()
    scratch.cleanup()
