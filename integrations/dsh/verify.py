import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("Usage: IDEA2PAPER_PYTHON=<python> python verify.py <bundle.tgz>")
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
        role = next((r for r in ('plan','writer','reviewer') if f'Idea2Paper role: {r}' in prompt), None)
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
            answer = {'plan': {'hypothesis':'Binary search inspects fewer keys for absent targets.', 'sizes':[32,128,1024]},
                      'writer': draft,
                      'reviewer': {'accepted': fixture['mode'] != 'reject',
                                   'feedback': ('The reported conclusions match the measured comparison counts.'
                                                if fixture['mode'] != 'reject'
                                                else 'The result interpretation needs a narrower conclusion.')}}[role]
            assert self.path.endswith('/messages'), self.path
            frames = [
                {'type':'message_start','message':{'id':'fixture','model':body['model'],'usage':{'input_tokens':10,'output_tokens':0}}},
                {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':json.dumps(answer)}},
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
        capture_output=True, text=True, timeout=35,
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

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
env['DEEPSEEK_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
try:
    subprocess.run([dsh,'plugin','--profile','idea2paper','add',str(bundle)],env=env,cwd=root,check=True)
    run('run', 'first', 'Compare search inspection counts')
    assert state('first')['completed'] == ['plan','experiment','writer','reviewer','delivery']
    audit('first')
    assert [r for r, _ in requests] == ['plan', 'writer', 'reviewer'], requests
    run('resume', 'first')
    assert len(requests) == 3

    fixture.update(mode='hold', writer=threading.Event(), release=threading.Event())
    p = subprocess.Popen([dsh,'--profile','idea2paper','run','interrupted','Compare search inspection counts'],
                         cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        if not fixture['writer'].wait(25):
            p.kill()
            raise AssertionError(p.communicate(timeout=5))
        assert state('interrupted')['completed'] == ['plan','experiment']
        results = root/'interrupted/results/experiment.json'
        before = results.stat().st_mtime_ns, results.read_bytes()
        concurrent = run('resume', 'interrupted', expected=1)
        assert 'already running' in concurrent.stderr
        p.send_signal(signal.SIGKILL)
        p.wait(timeout=10)
    finally:
        fixture['release'].set()
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
    fixture['mode'] = 'success'
    run('resume','interrupted')
    audit('interrupted')
    assert (results.stat().st_mtime_ns, results.read_bytes()) == before
    assert [r for r,_ in requests[3:]] == ['plan','writer','writer','reviewer']

    fixture['mode'] = 'reject'
    run('run','rejected','Compare search inspection counts',expected=1)
    assert state('rejected')['completed'][-1] == 'reviewer'
    assert not (root/'rejected/paper/main.pdf').exists()
    fixture['mode'] = 'quota'
    count = len(requests)
    run('run','quota','Compare search inspection counts',expected=1)
    assert [role for role, _ in requests[count:]] == ['plan', 'writer']
    assert state('quota')['completed'] == ['plan', 'experiment']
    results = root / 'quota' / 'results' / 'experiment.json'
    before = results.stat().st_mtime_ns, results.read_bytes()
    fixture['mode'] = 'success'
    run('resume', 'quota')
    audit('quota')
    assert [role for role, _ in requests[count:]] == ['plan', 'writer', 'writer', 'reviewer']
    assert (results.stat().st_mtime_ns, results.read_bytes()) == before
    print('PASS: packed bundle, native model turns, real PDFs, crash resume, project lock, rejection, quota',flush=True)
finally:
    server.shutdown()
    server.server_close()
    scratch.cleanup()
