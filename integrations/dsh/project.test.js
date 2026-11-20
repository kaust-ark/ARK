import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { chmod, cp, lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { projectFile, projectEditor, readProjectFile, reproductionComparison, reviewForAuthor, reviewSubmission } from './project.js'

const bundle = dirname(fileURLToPath(import.meta.url))

test('worker and reviewer values retain their source and the host tolerance decision', () => {
  const row = {control:2, artifact:'results/values.json', rows_pointer:'/rows', identifiers:{seed:2},
    columns:['temperature','accuracy'], differences:['temperature'], observed:{temperature:16,accuracy:0.883333},
    expected:{temperature:64,accuracy:0.8833331}, program:'sealed program'}
  const result = {artifact:row.artifact, rows_pointer:'/rows', identifiers:{seed:2}, metrics:[
    {metric:'temperature', reported_by_worker:16, independently_recomputed_by_reviewer:64, agrees:false},
    {metric:'accuracy', reported_by_worker:0.883333, independently_recomputed_by_reviewer:0.8833331, agrees:true},
  ]}
  assert.deepEqual(reproductionComparison(row), result)
  const control = {control:1, target:'f', expected:2, observed:3}
  const record = {checks:{experiment:{status:'assessed',controls:[control,row]}}}
  assert.deepEqual(reviewForAuthor(record).checks.experiment.controls, [control,result])
  assert.strictEqual(record.checks.experiment.controls[1], row)
  const old = {checks:{experiment:{controls:[control]}}}
  assert.strictEqual(reviewForAuthor(old), old)
})
test('author can edit sources but cannot rewrite evidence, credentials or linked paths', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-files-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  assert.equal((await projectFile(root, 'paper/main.tex', 'paper source')).bytes, 12)
  assert.equal((await projectFile(root, 'paper/main.tex')).content, 'paper source')
  for (const path of ['paper/../workflow.json', 'results/raw.json', 'paper/data.png', '.env', 'scripts/../../outside.py']) {
    await assert.rejects(projectFile(root, path, 'x'))
  }
  await symlink('/tmp', join(root, 'paper', 'escape'))
  await assert.rejects(projectFile(root, 'paper/escape/out.tex', 'x'))
})

function frames(model, content) {
  const id = randomUUID()
  const isTool = typeof content !== 'string'
  const blocks = Array.isArray(content) ? content : [content]
  const data = [
    {type:'message_start',message:{id, model,usage:{input_tokens:10,output_tokens:0}}},
    ...blocks.flatMap((block, index) => [
      {type:'content_block_start',index,content_block:isTool ? {type:'tool_use',id:randomUUID(),name:block.name,input:{}} : {type:'text',text:''}},
      {type:'content_block_delta',index,delta:isTool ? {type:'input_json_delta',partial_json:JSON.stringify(block.args)} : {type:'text_delta',text:block}},
      {type:'content_block_stop',index},
    ]),
    {type:'message_delta',delta:{stop_reason:isTool?'tool_use':'end_turn'},usage:{output_tokens:5}},
    {type:'message_stop'},
  ]
  return data.map(frame => 'event: ' + frame.type + '\ndata: ' + JSON.stringify(frame) + '\n\n').join('')
}

// Provide an already installed, disposable base-backed idea2paper profile to test fully offline.
test('bounded evidence reads preserve Unicode offsets and the complete-file digest', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-read-'))
  t.after(() => rm(root, {recursive: true, force: true}))
  const path = 'paper/main.tex'
  const material = '🧪'.repeat(6100) + 'first method; revised method'
  await projectFile(root, path, material)
  const first = await readProjectFile(root, {path})
  assert.equal(first.next_offset, 6000)
  const second = await readProjectFile(root, {path, offset:first.next_offset})
  assert.equal(first.content + second.content, material)
  assert.equal(first.sha256, second.sha256)
  assert.equal(second.next_offset, null)
  const hit = await readProjectFile(root, {path, find:'method'})
  assert.equal(hit.match_offset, 6106)
  assert.equal((await readProjectFile(root, {path, offset:hit.match_offset+1, find:'method'})).match_offset, 6122)
  assert.equal((await readProjectFile(root, {path, find:'absent'})).found, false)
  await assert.rejects(readProjectFile(root, {path, offset:-1}), /Offset/)
})

test('concurrent section edits preserve other manuscript passages', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-edits-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const file = 'paper/main.tex'
  await projectFile(root, file, 'Result A. Result B.')
  await Promise.all([projectFile(root, file, 'First observation.', 'Result A.'),
                     projectFile(root, file, 'Second observation.', 'Result B.')])
  assert.equal((await projectFile(root, file)).content, 'First observation. Second observation.')
  await assert.rejects(projectFile(root, file, 'Wrong replacement', 'Result A.'), /exactly once/)
  await projectFile(root, file, 'same method; same method')
  await assert.rejects(projectFile(root, file, 'new method', 'same method'), /found 2/)
  await assert.rejects(projectFile(root, file, 'new method', 'same method', {expected_matches: 3}), /found 2/)
  await projectFile(root, file, 'new method', 'same method', {expected_matches: 2})
  assert.equal((await projectFile(root, file)).content, 'new method; new method')

})

function manuscript(body) {
  return '\\documentclass{article}\n\\begin{document}\n' + body + '\n\\end{document}\n'
}

test('a missing manuscript anchor shows the actual numbered headings for the next edit', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-heading-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  await projectFile(root, path, manuscript('\\section{Findings}\nThe two conditions were measured.'))
  const original = await projectFile(root, path)
  await assert.rejects(projectFile(root, path, 'Updated text.', undefined, {
    base_sha256:original.sha256, insert_before:'\\section{Discussion}'
  }), /found 0[\s\S]+3 \\section\{Findings\}/)
  assert.equal((await projectFile(root, path)).sha256, original.sha256)
})

test('a saved owner edit returns the page preview from its actual typesetting check', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-page-preview-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const editor = projectEditor(root)
  const path = 'paper/main.tex'
  await editor.write({path, content:manuscript('Results.')})
  const preview = {body_pages:4, target:4, next:'Run compile_paper to verify.'}
  const edit = await editor.write({path, replace:'Results.', content:'Measured results.'}, async trial => {
    assert.equal(trial.content, manuscript('Measured results.'))
    return {ok:true, first_pass_preview:preview}
  })
  assert.deepEqual(edit.first_pass_preview, preview)
  assert.equal(edit.sha256, (await editor.read({path})).sha256)
})

test('a complete main.tex survives truncated output even with its correct base hash', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-truncation-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const abstract = '\\begin{abstract}\nAblation results.\n\\end{abstract}'
  const original = manuscript(abstract + '\n\\section{Methods}\n' + 'Repeated scientific observation. '.repeat(500))
  const {sha256} = await projectFile(root, path, original)
  const fragment = original.slice(0, original.indexOf('\\section'))
  for (const ending of ['', '% \\end{document}', '\\\\end{document}', '\\verb|\\end{document}|',
                         '\\begin{verbatim}\n\\end{document}\n\\end{verbatim}']) {
    await assert.rejects(projectFile(root, path, fragment + ending, undefined, {base_sha256:sha256, allow_shrink:true}), /incomplete document/)
  }
  await assert.rejects(projectFile(root, path, '', '\\end{document}'), /incomplete document/)
  const shell = manuscript('A shorter submission.')
  for (const edit of [{base_sha256:sha256}, {allow_shrink:true}, {base_sha256:sha256, allow_shrink:'true'}]) {
    await assert.rejects(projectFile(root, path, shell, undefined, edit), /allow_shrink:true/)
  }
  assert.equal((await projectFile(root, path)).content, original)
  const longParagraph = 'Repeated scientific observation. '.repeat(500)
  for (const [passage, edit] of [
    [longParagraph, {base_sha256:sha256}],
    [undefined, {base_sha256:sha256, start_line:7, end_line:7}],
  ]) {
    await assert.rejects(projectFile(root, path, 'Short result.', passage, edit), /allow_shrink:true/)
    assert.equal((await projectFile(root, path)).sha256, sha256)
  }
  const short = await projectFile(root, path, 'Short result.', longParagraph, {base_sha256:sha256, allow_shrink:true})
  await projectFile(root, path, undefined, undefined, {base_sha256:short.sha256, restore_sha256:sha256})
  const piece = await projectFile(root, path, '\\section{Approach}', '\\section{Methods}')
  assert.equal(piece.previous_sha256, sha256)
  assert.equal((await projectFile(root, path)).content, original.replace('Methods', 'Approach'))
})

test('an acknowledged full rewrite archives the exact source and restores it with compare-and-swap', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-restore-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const original = '\uFEFF' + manuscript('Scientific observation 🧪.\r\n'.repeat(500))
  const {sha256: first} = await projectFile(root, path, original)
  const smaller = manuscript('A concise report.')
  const {sha256: second, previous_sha256} = await projectFile(root, path, smaller, undefined, {base_sha256:first, allow_shrink:true})
  assert.equal(previous_sha256, first)
  const saved = join(root, '.dsh', 'native', 'main-tex-revisions', first)
  assert.equal((await lstat(saved)).mode & 0o222, 0)
  assert.deepEqual(await readFile(saved), Buffer.from(original))
  assert.deepEqual((await readProjectFile(root, {path, list_revisions:true})).revisions, [first])
  const page = await readProjectFile(root, {path, revision_sha256:first})
  assert.equal(page.sha256, first)
  assert.equal(page.next_offset, 6000)
  await assert.rejects(projectFile(root, path, undefined, undefined, {restore_sha256:first}), /current base_sha256/)
  await assert.rejects(projectFile(root, path, undefined, undefined, {restore_sha256:first, base_sha256:first}), /changed since/)
  await assert.rejects(projectFile(root, path, 'extra', undefined, {restore_sha256:first, base_sha256:second}), /only restore_sha256/)
  await assert.rejects(projectFile(root, 'paper/other.tex', undefined, undefined, {restore_sha256:first, base_sha256:second}), /only restore_sha256/)
  await assert.rejects(projectFile(root, path, undefined, undefined, {restore_sha256:'../file', base_sha256:second}), /saved manuscript SHA-256/)
  const restored = await projectFile(root, path, undefined, undefined, {restore_sha256:first, base_sha256:second})
  assert.equal(restored.sha256, first)
  assert.equal(restored.previous_sha256, second)
  assert.deepEqual(await readFile(join(root, path)), Buffer.from(original))
  assert.equal((await readProjectFile(root, {path, revision_sha256:second})).content, smaller)
})

test('a preexisting small project can finish a draft and explicitly restore its earlier source', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-old-source-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  await mkdir(join(root, 'paper'))
  await writeFile(join(root, path), 'Legacy notes.')
  assert.deepEqual((await readProjectFile(root, {path, list_revisions:true})).revisions, [])
  const first = await projectFile(root, path, manuscript('A report.'))
  await assert.rejects(projectFile(root, path, 'Only a fragment.'), /incomplete document/)
  const second = await projectFile(root, path, manuscript('Short.'))
  assert.equal(second.previous_sha256, first.sha256)
  await projectFile(root, path, undefined, undefined, {base_sha256:second.sha256, restore_sha256:first.previous_sha256})
  assert.equal((await readProjectFile(root, {path})).content, 'Legacy notes.')
})

test('corrupt, writable or linked manuscript snapshots abort the edit or restore without changing the live file', async t => {
  const root = await mkdtemp(join(tmpdir(), 'research-invalid-backup-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const {sha256: first} = await projectFile(root, path, manuscript('First source.'))
  const current = manuscript('New source.')
  const {sha256: second} = await projectFile(root, path, current)
  const folder = join(root, '.dsh', 'native', 'main-tex-revisions')
  const archive = join(folder, first)
  const restore = () => projectFile(root, path, undefined, undefined, {base_sha256:second, restore_sha256:first})
  await chmod(archive, 0o600)
  await assert.rejects(restore(), /read-only/)
  await writeFile(archive, 'modified')
  await chmod(archive, 0o400)
  await assert.rejects(restore(), /SHA-256 check/)
  await rm(archive)
  await symlink(join(root, path), archive)
  await assert.rejects(restore(), {code:'ELOOP'})
  await symlink(join(root, path), join(folder, second))
  await assert.rejects(projectFile(root, path, manuscript('Overwritten.')), {code:'ELOOP'})
  assert.equal((await projectFile(root, path)).content, current)
  await rm(folder, {recursive:true})
  await symlink(root, folder)
  await assert.rejects(restore(), /Unsafe project directory/)
  await assert.rejects(projectFile(root, path, manuscript('Overwritten.')), /Unsafe project directory/)
  assert.equal((await projectFile(root, path)).content, current)
})

// Use the pinned CLI with a disposable profile and the candidate bundle's source.
async function installTestProfile(home) {
  const profile = join(home, 'profiles', 'idea2paper')
  await mkdir(dirname(profile), { recursive: true })
  await cp(process.env.IDEA2PAPER_TEST_PROFILE, profile, { recursive: true })
  const pkg = join(profile, 'node_modules', 'idea2paper-dsh')
  for (const file of ['index.js','workflow.js','project.js','experiment.js','cordis.patch.yml','package.json']) await cp(join(bundle,file), join(pkg,file))
  return profile
}

test('pinned native CLI scopes terminal Stop to the community project owner', {
  skip: !process.env.IDEA2PAPER_TEST_PROFILE || !process.env.IDEA2PAPER_TEST_DSH,
  timeout: 30000,
}, async t => {
  const home = await mkdtemp(join(tmpdir(), 'idea2paper-interrupt-'))
  t.after(() => rm(home, {recursive:true, force:true}))
  await installTestProfile(home)
  const helper = join(home, 'test-domain')
  // Park inspection before any model call. Stop must reach the live helper
  // while this owner's flock is still held, even after a foreground-group SIGINT.
  await writeFile(helper, `#!/usr/bin/env python3
import fcntl, json, sys
for line in sys.stdin:
    request = json.loads(line)
    op = request["op"]
    print("domain " + op, file=sys.stderr, flush=True)
    if op == "stop":
        with open(".workflow.lock", "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"id": request["id"], "ok": True, "result": {"state": "stopped"}}), flush=True)
            else:
                sys.exit("owner lost the project lock before Stop")
`, {mode:0o700})
  function launch(args, hosted = false) {
    const child = spawn(process.env.IDEA2PAPER_TEST_DSH, ['--profile','idea2paper',...args], {
      env: {HOME:home, DSH_HOME:home, PATH:process.env.PATH, PKG_NATIVE_CACHE_PATH:process.env.PKG_NATIVE_CACHE_PATH,
        DSH_TELEMETRY_DISABLED:'1', IDEA2PAPER_PYTHON:helper, ...(hosted && {IDEA2PAPER_HOSTED:'1'})},
      stdio:['ignore','ignore','pipe'], detached:true,
    })
    t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL') })
    let log = ''
    const requests = []
    let entered
    const first = new Promise(resolve => { entered = resolve })
    createInterface({input:child.stderr}).on('line', line => {
      log += line + '\n'
      if (line.startsWith('domain ')) {
        const op = line.slice(7)
        requests.push(op)
        entered(op)
      }
    })
    const exited = new Promise(resolve => child.once('close', resolve))
    const ready = Promise.race([first, exited.then(() => { throw new Error('CLI exited before its domain request: ' + log) })])
    return {child, ready, exited, requests, get log() { return log }}
  }
  for (const hosted of [false, true]) {
    const project = join(home, hosted ? 'hosted' : 'community')
    const owner = launch(['run', project, 'Measure equivalent comparisons'], hosted)
    assert.equal(await owner.ready, 'project_status')
    const observer = launch(['status', project])
    assert.equal(await observer.ready, 'inspect_project')
    process.kill(-observer.child.pid, 'SIGINT')
    assert.equal(await observer.exited, 130)
    assert.deepEqual(observer.requests, ['inspect_project'])
    assert.equal(owner.child.exitCode, null)
    assert.deepEqual(owner.requests, ['project_status'])
    const marker = join(project,'.native-stop')
    await assert.rejects(lstat(marker), {code:'ENOENT'}, observer.log)

    process.kill(-owner.child.pid, 'SIGINT')
    assert.equal(await owner.exited, 130)
    assert.deepEqual(owner.requests, hosted ? ['project_status'] : ['project_status','stop'])
    await assert.rejects(lstat(marker), {code:'ENOENT'}, owner.log)
  }
})

for (const scenario of ['program', 'unavailable', 'flattened-503', 'quota', 'no-retry', 'server-exhausted']) test('pinned native blind reviewer with ' + scenario, {
  skip: !process.env.IDEA2PAPER_TEST_PROFILE || !process.env.IDEA2PAPER_TEST_DSH,
  timeout: 90000,
}, async t => {
  const home = await mkdtemp(join(tmpdir(), 'idea2paper-native-test-'))
  if (!process.env.IDEA2PAPER_TEST_KEEP) t.after(() => rm(home, { recursive: true, force: true }))
  const profile = await installTestProfile(home)
  let phase = 'review', committed = false, attemptedAfterDisclosure = false
  let recorded, delivered = false, resumed = false, stopped = false, parked
  let scopeCommitments = 0
  const commitment = scenario === 'unavailable' ? 'unavailable' : 'program'
  const google = scenario !== 'unavailable'
  const terminal = ['flattened-503', 'quota', 'no-retry', 'server-exhausted'].includes(scenario)
  let rejectedRequests = 0
  let failedRequest, followupRequest, reviewerError
  const blocked = Promise.withResolvers()
  const events = []
  const calls = []
  let failure
  const server = createServer(async (req,res) => {
    try {
      let raw = ''
      for await (const chunk of req) raw += chunk
      const body = JSON.parse(raw)
      const tools = (google ? body.tools?.flatMap(t => t.functionDeclarations || []) : body.tools)?.map(t => t.name) || []
      const history = JSON.stringify(google ? body.contents : body.messages)
      const inference = body.contents?.flatMap(message => message.parts || [])
        .findLast(part => part.functionResponse?.name === 'test_inference')?.functionResponse
      if (inference) assert.ok(!inference.response.error, inference.response.error)
      assert.ok(!history.includes('host-only-row-provenance'))
      if (google) assert.match(req.url, /\/models\/gemini-4-hydrogen:streamGenerateContent\b/)
      if (google && !parked && tools.includes('read_evidence') && history.includes('known-answer-receipt') && !followupRequest) {
        if (!failedRequest || scenario === 'server-exhausted') {
          if (failedRequest) assert.equal(history, failedRequest)
          assert.ok(++rejectedRequests <= (scenario === 'server-exhausted' ? 2 : 1), 'provider retry budget exceeded')
          failedRequest = history
          const demand = 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.'
          const error = scenario === 'flattened-503' ? {message:'Google: ' + demand, type:'invalid_request_error'}
            : scenario === 'quota' ? {code:402, message:'Insufficient Balance', status:'PAYMENT_REQUIRED'}
            : {code:503, message:demand, status:'UNAVAILABLE'}
          res.writeHead(scenario === 'quota' ? 402 : 503, {'Content-Type':'application/json'})
          res.end(JSON.stringify({error}))
          return
        }
        assert.ok(!terminal, 'unexpected retry of ' + scenario)
        assert.equal(history, failedRequest, 'a retry must continue the failed model step')
        followupRequest = history
      }
      if (parked && tools.includes('read_evidence')) {
        assert.ok(history.includes('frozen-source-only-for-independent-reader'), 'ordinary reviews still open immediately')
        blocked.resolve()
        return // Hold the request open until DSH cancels the in-flight turn.
      }
      let response
      if (tools.includes('commit_evidence_scope') && !tools.includes('read_evidence')) {
        assert.match(history, /schema-only-scope-brief/)
        for (const secret of ['frozen-source-only-for-independent-reader', 'prior-finding-private',
             'reported-row-private', 'manuscript-private', 'conclusion-passage-private', 'inlineData']) {
          assert.ok(!history.includes(secret), secret)
        }
        response = {name:'commit_evidence_scope', args:{comparison:'paired loss', alternative:'different exposure', discriminator:'measured exposure'}}
      } else if (tools.includes('reproduce_measurement') && !tools.includes('read_evidence')) {
        assert.deepEqual(tools, ['reproduce_measurement'])
        assert.match(history, /original protocol only: derive metric for seed/)
        for (const privateText of ['parent-scientific-private', 'frozen-source-only-for-independent-reader',
             'prior-finding-private', 'reported-row-private', 'manuscript-private', 'conclusion-passage-private', '"type":"image"', 'inlineData']) {
          assert.ok(!history.includes(privateText), privateText)
        }
        const args = commitment === 'program'
          ? {program:'def reproduce(ids): return {"metric": ids["seed"] * 2}'}
          : {unavailable:'The original protocol does not identify an obtainable dataset.'}
        const submit = {name:'reproduce_measurement', args}
        response = commitment === 'unavailable' ? [submit, submit] : submit
      } else if (tools.includes('read_evidence')) {
        assert.deepEqual(tools.toSorted(), [...(scenario === 'program' ? ['commit_evidence_scope'] : []), 'read_evidence', 'reproduce_measurement', 'run_method_control', ...(scenario === 'program' ? ['test_inference'] : [])])
        assert.ok(committed)
        for (const opening of ['frozen-source-only-for-independent-reader', 'reported-row-private',
             'prior-finding-private', 'manuscript-private', 'Rendered manuscript page 2 of 2']) {
          assert.equal(history.split(opening).length - 1, 1, opening)
        }
        assert.ok(!history.includes('parent-scientific-private'))
        if (commitment === 'program') {
          assert.match(history, /conclusion-passage-private/)
          if (scenario === 'program') assert.ok(history.indexOf('archive-each-claim-once') > history.indexOf('Rendered manuscript page 2 of 2'))
          assert.match(history, /reported_by_worker/)
          assert.match(history, /independently_recomputed_by_reviewer/)
        }
        assert.match(history, google ? /"inlineData"/ : /"type":"image"/)
        if (!attemptedAfterDisclosure) {
          attemptedAfterDisclosure = true
          response = {name:'reproduce_measurement',args:{program:'def reproduce(ids): return {"metric": 1}'}}
        } else if (!history.includes('known-answer-receipt')) {
          response = {name:'run_method_control',args:{target:'mean',inputs_json:'{"values":[2,4]}',
            expected_json:'3',alternative_json:'6',rationale:'The mean of two and four is three. The sum is six.'}}
        } else if (scenario === 'program' && !inference) {
          response = {name:'test_inference',args:{passage:{selector:'conclusion'}, premise:'total', outcome:'largest',
            assumptions:'Two positive values form a pair.',
            program:'def reproduce(ids):\n    pair = [2, 4] if ids["case"] == "A" else [1, 5]\n    return {"premise": sum(pair), "outcome": max(pair)}'}}
        } else if (history.includes('Separate the result from its explanation')) {
          assert.match(history, /different exposure still fits/)
          response = JSON.stringify({amend:{verdict:'accept',checks:{manuscript:{claims:[{kind:'result'},{kind:'explanation'}]}}}})
        } else if (history.includes('independently-read-primary-passage')) {
          const citations = {status:'assessed', evidence:[{artifact:'sources/paper.txt',quote:'independently-read-primary-passage'}]}
          response = JSON.stringify(google ? {revision:'rev1', verdict:'unassessed', alternative:'different exposure still fits', checks:{citations,manuscript:{conclusion_support:'measured'}}} : {amend:{checks:{citations}}})
        } else if (history.includes('Cite an observed primary source')) {
          response = {name:'read_evidence',args:{artifact:'sources/paper.txt'}}
        } else if (history.includes('measured-observation')) {
          assert.match(history, google ? /"inlineData"/ : /"type":"image"/, 'The provider must receive pixels for a visual receipt')
          response = JSON.stringify({ revision:'rev1', verdict:'accept' })
        } else response = { name:'read_evidence',args:{artifact:'paper/main.pdf',page:1,visual:true} }
      } else {
        assert.ok(tools.includes('request_review'), JSON.stringify(tools))
        assert.ok(!tools.includes('run_method_control') && !tools.includes('reproduce_measurement') && !tools.includes('test_inference'))
        if (reviewerError) {
          response = 'The reviewer stopped.'
        } else if (parked) {
          response = {name:'request_review',args:{draft:true}}
        } else if (resumed) {
          assert.match(history, /parent-scientific-private/)
          assert.match(history, /What happened/)
          response = 'The recorded review accepted revision one.'
        } else if (phase === 'review') {
          phase = 'deliver'
          response = {name:'request_review',args:{}}
        } else if (phase === 'deliver') {
          if (commitment === 'program') {
            assert.match(history, /reported_by_worker/)
            assert.match(history, /independently_recomputed_by_reviewer/)
          }
          phase = 'done'
          response = {name:'deliver',args:{}}
        } else if (scenario === 'unavailable' && delivered) {
          res.writeHead(402, {'Content-Type':'application/json'})
          res.end(JSON.stringify({error:{code:'insufficient_balance',message:'Quota after delivery'}}))
          return
        } else response = 'The project delivered.'
      }
      res.writeHead(200, {'Content-Type':'text/event-stream'})
      if (google) {
        const parts = typeof response === 'string' ? [{text:response}]
          : [{functionCall:{name:response.name,args:response.args},thoughtSignature:'c2ln'}]
        const chunk = {candidates:[{content:{role:'model',parts},finishReason:'STOP'}],
          usageMetadata:{promptTokenCount:10,candidatesTokenCount:5,totalTokenCount:15}}
        res.end('data: ' + JSON.stringify(chunk) + '\n\n')
      } else res.end(frames(body.model, response))
    } catch (error) {
      failure ??= error
      res.writeHead(400)
      res.end(JSON.stringify({error:{message:error.message}}))
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const gateway = 'http://127.0.0.1:' + server.address().port
  const project = join(home,'research')
  if (google) {
    const env = {PATH:process.env.PATH, HOME:home, LITELLM_LOCAL_MODEL_COST_MAP:'True',
      ARK_DSH_CONTEXT_WINDOW:'65536',
      ARK_DSH_MAX_TOKENS:'2048', GEMINI_API_KEY:'fixture-only'}
    if (scenario === 'no-retry') env.ARK_DSH_RETRIES = '0'
    execFileSync('python3', ['-c', [
      'from pathlib import Path', 'from types import SimpleNamespace', 'import sys',
      'from ark.orchestrator.dsh_workflow import _native_profile',
      '_native_profile(SimpleNamespace(config={}, model="gemini/gemini-4-hydrogen"), Path(sys.argv[1]), Path(sys.argv[2]))',
    ].join('\n'), project, profile], {env, cwd:join(bundle,'../..')})
    const config = join(profile, 'cordis.patch.yml')
    const rows = JSON.parse(await readFile(config,'utf8'))
    const googleRoute = rows.find(row => row.id === 'llm-pi-ai').config.providers.google
    assert.match(googleRoute.baseURL, /^https:\/\/generativelanguage\.googleapis\.com\//)
    googleRoute.baseURL = gateway + '/v1beta'
    googleRoute.models[0].input = ['text', 'image']
    await writeFile(config, JSON.stringify(rows))
  } else await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
    {id:'agent-default-model',config:{provider:'loopback',model:'vision-fixture'}},
    {id:'llm-pi-ai',config:{providers:{loopback:{api:'anthropic-messages',baseURL:gateway,apiKeyEnv:'FIXTURE_KEY',
      models:[{id:'vision-fixture',contextWindow:65536,maxTokens:2048,input:['text','image']}]}}}},
  ]))
  const domain = ({op,args}) => {
    calls.push(op)
    switch (op) {
      case 'stop': stopped = true; return {state:'stopped'}
      case 'project_status': return {state:reviewerError?'waiting':stopped?'stopped':delivered?'delivered':'running', delivered, commands:[]}
      case 'project_event':
        events.push(args)
        if (terminal && args.role === 'reviewer' && args.kind === 'turn/end' && args.reason?.kind === 'error') {
          reviewerError = args.reason.error
        }
        return {}
      case 'review_snapshot':
        assert.equal(args.preflight, !parked)
        return {revision:'rev1',method_control:true,measurement_reproduction:true,rendered_pages:2,rendered_artifact:'paper/main.pdf',
          opening_evidence:['scripts/plot.py','results/data.json',{artifact:'paper/main.pdf',page:2}], brief:'manuscript-private; prior-finding-private',
          ...((scenario === 'program' || parked) && {scope_brief:'schema-only-scope-brief', inference_test:scenario === 'program', claim_instructions:'archive-each-claim-once'}),
          ...(!parked && {reproduction_brief:'original protocol only: derive metric for seed'})}
      case 'commit_evidence_scope':
        assert.ok(committed || parked)
        assert.equal(args.revision, 'rev1')
        assert.equal(args.discriminator, 'measured exposure')
        assert.equal(scopeCommitments++, 0)
        return {comparison:args.comparison, alternative:args.alternative, discriminator:args.discriminator}
      case 'review_evidence':
        assert.ok(committed || parked, 'read submission before commitment')
        if (scenario === 'program' || parked) assert.equal(scopeCommitments, 1)
        if (args.artifact === 'results/data.json') return {text:'reported-row-private',receipt:{artifact:args.artifact,revision:'rev1'}}
        if (args.artifact === 'scripts/plot.py') return {text:'frozen-source-only-for-independent-reader',receipt:{artifact:args.artifact,revision:'rev1'}}
        if (args.artifact === 'sources/paper.txt') return {text:'independently-read-primary-passage',receipt:{artifact:args.artifact,revision:'rev1'}}
        if (args.artifact === 'paper/main.pdf' && args.page === 2 && !args.visual)
          return {text:'conclusion-passage-private',receipt:{artifact:args.artifact,page:2,revision:'rev1'}}
        return {text:'measured-observation',receipt:{artifact:'paper/main.pdf',visual:true},
          mime_type:'image/png',image_base64:'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAIAAABvFaqvAAAAIklEQVR4nGNkYGhgoAZgooopowaNGjRq0KhBowaNGkQRAAAV1ACwA5vbQAAAAABJRU5ErkJggg=='}
      case 'reproduce_measurement':
        assert.equal(args.revision, 'rev1')
        assert.equal(committed, false)
        assert.deepEqual(Object.keys(args).sort(), [commitment, 'revision'].sort())
        committed = true
        if (commitment === 'unavailable') return {unavailable:args.unavailable}
        return {receipt:{control:2, artifact:'results/data.json', rows_pointer:'', identifiers:{seed:2},
          columns:['metric'], expected:{metric:4}, observed:{metric:4}, differences:[], matches:true,
          marker:'host-only-row-provenance', program:args.program}}
      case 'run_method_control':
        assert.ok(committed)
        assert.equal(args.revision, 'rev1')
        return {receipt:{control:1, artifact:'scripts/plot.py', marker:'known-answer-receipt', matches:true}}
      case 'test_inference':
        assert.equal(args.revision, 'rev1')
        assert.equal(args.premise, 'total')
        return {receipt:{...args,control:3,passage:{artifact:'paper/main.pdf',page:2,quote:'conclusion-passage-private'},
          cases:{A:{premise:6,outcome:4},B:{premise:6,outcome:5}},same_premise:true,distinguishable_outcome:true}}
      case 'check_review':
        if (google && args.verdict.checks?.citations?.evidence?.length && !args.verdict.checks?.manuscript?.claims)
          return {valid:false, diagnostic:'Separate the result from its explanation'}
        return args.observed.some(r => r.artifact === 'sources/paper.txt') && args.verdict.checks?.citations.evidence[0].artifact === 'sources/paper.txt'
          ? {valid:true} : {valid:false, diagnostic:'Cite an observed primary source'}
      case 'record_review':
        assert.equal(args.verdict.verdict, 'accept')
        assert.equal(args.verdict.checks.citations.evidence[0].artifact, 'sources/paper.txt')
        assert.ok(args.observed.some(r => r.marker === 'known-answer-receipt'))
        assert.equal(args.observed.some(r => r.marker === 'host-only-row-provenance'), commitment === 'program')
        assert.equal(args.observed.some(r => r.control === 3 && r.cases.B.outcome === 5), scenario === 'program')
        assert.ok(args.observed.every(Boolean))
        assert.ok(args.observed.some(receipt => receipt.artifact === 'scripts/plot.py'))
        assert.ok(args.observed.filter(receipt => receipt.artifact === 'paper/main.pdf' && receipt.visual).length >= 2)
        recorded = args
        return { accepted:true, checks:{experiment:{controls:args.observed.filter(r => r.control)}} }
      case 'deliver': assert.ok(recorded); delivered=true; return {delivered}
      default: throw new Error('unexpected op ' + op)
    }
  }
  async function launch(...args) {
    const child = spawn(process.env.IDEA2PAPER_TEST_DSH, ['--profile','idea2paper',...args], {
      env: { HOME:home,PATH:process.env.PATH,DSH_HOME:home,
        PKG_NATIVE_CACHE_PATH:process.env.PKG_NATIVE_CACHE_PATH,DEEPSEEK_API_KEY:'fixture-only',FIXTURE_KEY:'localhost',ARK_DSH_API_KEY:'fixture-only',
        DEEPSEEK_BASE_URL:'http://127.0.0.1:' + server.address().port,
        DSH_TELEMETRY_DISABLED:'1',IDEA2PAPER_DOMAIN_FD:'3',...(google && {IDEA2PAPER_HOSTED:'1'}) },
      stdio:['ignore','pipe','pipe','pipe'],
    })
    let log = ''
    child.stdout.on('data',chunk => {log += chunk})
    child.stderr.on('data',chunk => {log += chunk})
    const watchdog = setTimeout(() => child.kill('SIGKILL'), 30000)
    const lines = createInterface({input:child.stdio[3]})
    lines.on('line', line => {
      const request = JSON.parse(line)
      try { child.stdio[3].write(JSON.stringify({id:request.id,ok:true,result:domain(request)}) + '\n') }
      catch(error) { failure ??= error; child.stdio[3].write(JSON.stringify({id:request.id,ok:false,error:error.message}) + '\n') }
    })
    const code = await new Promise(resolve => child.once('close',resolve))
    clearTimeout(watchdog)
    if (failure) throw failure
    assert.equal(code,0,home + '\n' + log)
    assert.doesNotMatch(log, /\[review_repair_requested\] undefined/)
  }
  await launch('run',project,'parent-scientific-private')
  if (google) {
    assert.ok(failedRequest && (terminal ? !followupRequest : followupRequest))
    assert.equal(calls.filter(op => op === 'run_method_control').length,1)
    assert.equal(calls.filter(op => op === 'reproduce_measurement').length,1)
    assert.equal(new Set(events.filter(e => e.role === 'reviewer').map(e => e.session_id)).size, 1)
    if (terminal) {
      assert.equal(rejectedRequests, scenario === 'server-exhausted' ? 2 : 1)
      assert.equal(reviewerError?.code, {'flattened-503':'INVALID_REQUEST',quota:'QUOTA','no-retry':'SERVER','server-exhausted':'SERVER'}[scenario])
      assert.ok(!recorded && !delivered)
      if (scenario === 'quota') {
        const event = events.find(e => e.role === 'reviewer' && e.kind === 'turn/end' && e.reason?.error === reviewerError)
        execFileSync('python3', ['-c', [
          'import json, sys', 'from ark.orchestrator.native_domain import NativeProject',
          'p = NativeProject(sys.argv[1])', 'p.state_dir.mkdir(parents=True)', 'p.state = {"state":"running"}',
          'p.project_event(json.load(sys.stdin))', 'assert NativeProject(p.root).state["pause_reason"] == "quota"',
        ].join('\n'), join(home,'quota-checkpoint')], {cwd:join(bundle,'../..'),input:JSON.stringify(event)})
      }
      return
    }
  }
  const first = JSON.parse(await readFile(join(project,'workflow.json'),'utf8'))
  const version = execFileSync(process.env.IDEA2PAPER_TEST_DSH, ['--version'], {
    encoding:'utf8', env:{PATH:process.env.PATH, HOME:home, DSH_HOME:home, PKG_NATIVE_CACHE_PATH:process.env.PKG_NATIVE_CACHE_PATH},
  }).trim()
  assert.equal(first.runtime, version)
  assert.ok(delivered)
  assert.equal(calls.filter(op => op === 'review_snapshot').length,1)
  assert.equal(calls.filter(op => op === 'reproduce_measurement').length,1)
  assert.equal(calls.filter(op => op === 'check_review').length,scenario === 'program' ? 3 : 2)
  assert.notEqual(recorded.session_id,first.owner_session_id)
  const repairs = events.filter(e => e.kind === 'review_repair_requested')
  assert.ok(repairs.length > 0, 'reviewer accepted a correction request')
  assert.ok(repairs.every(e => e.role === 'reviewer' && e.session_id === recorded.session_id && Number.isInteger(e.seq)))
  assert.ok(repairs.every(e => !Object.hasOwn(e, 'detail') && !Object.hasOwn(e, 'title') && !Object.hasOwn(e, 'content')))
  assert.ok(events.some(e => e.role === 'reviewer' && e.kind === 'tool/call'))
  assert.deepEqual([...new Set(events.filter(e => e.role === 'reviewer').map(e => e.session_id))], [recorded.session_id])
  if (google) return
  resumed = true
  await launch('resume',project,'What happened?', '--command-id', 'history-question')
  await launch('resume',project,'What happened?', '--command-id', 'history-question')
  assert.equal(JSON.parse(await readFile(join(project,'workflow.json'),'utf8')).owner_session_id,first.owner_session_id)
  assert.equal(calls.filter(op => op === 'record_review').length,1)
  parked = true
  const active = launch('resume', project, 'Explain again', '--command-id', 'interrupted')
  await blocked.promise
  await launch('resume', project, 'Narrow the claim', '--command-id', 'future-scope')
  await launch('stop', project)
  await active
  assert.equal(calls.filter(op => op === 'stop').length,1)
  assert.equal(calls.filter(op => op === 'record_review').length,1)
  assert.ok(stopped)
})

test('line edits use displayed one-based positions and a digest, preserving other bytes', async t => {
  const root = await mkdtemp(join(tmpdir(), 'paper-line-edits-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const source = 'préambule\r\nfirst 🧪\r\nsame\r\nsame\r\nlast'
  const {sha256} = await projectFile(root, path, source)
  const view = await readProjectFile(root, {path, start_line:2})
  assert.equal(view.sha256, sha256)
  assert.deepEqual(view.lines.map(l => [l.line,l.text]), [[2,'first 🧪'],[3,'same'],[4,'same'],[5,'last']])
  assert.equal((await readProjectFile(root, {path, find:'same'})).match_line, 3)
  assert.equal(view.next_line, null)
  await assert.rejects(projectFile(root, path, 'edited', undefined, {start_line:2,end_line:2}), /base_sha256/)
  const edit = {start_line:3,end_line:3,base_sha256:sha256}
  const after = await projectFile(root, path, 'edited', undefined, edit)
  assert.equal((await projectFile(root,path)).content, source.replace('same','edited'))
  await assert.rejects(projectFile(root, path, 'stale', undefined, edit), /changed/)
  const end = await projectFile(root, path, '', undefined, {start_line:4,end_line:5,base_sha256:after.sha256})
  assert.equal(end.previous_sha256, after.sha256)
  assert.equal((await projectFile(root,path)).content, 'préambule\r\nfirst 🧪\r\nedited\r\n')
})

test('native manuscript preflight precedes publication and detects concurrent external changes', async t => {
  const root = await mkdtemp(join(tmpdir(), 'paper-edit-trial-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const previous = manuscript('old paragraph')
  const {sha256} = await projectFile(root, path, previous)
  const candidate = previous.replace('old paragraph','bad formula')
  const rejectEdit = async data => {
    assert.deepEqual(data, {content:candidate,base_sha256:sha256})
    return {ok:false,diagnostic:'extra closing brace on line 3'}
  }
  await assert.rejects(projectFile(root,path,'bad formula','old paragraph',{},rejectEdit), /not saved.*closing brace/)
  assert.equal((await projectFile(root,path)).sha256, sha256)
  assert.deepEqual((await readProjectFile(root,{path,list_revisions:true})).revisions, [])
  const external = manuscript('concurrent draft')
  const race = async () => {
    await writeFile(join(root,path),external)
    return {ok:true}
  }
  await assert.rejects(projectFile(root,path,'good formula','old paragraph',{},race), /changed during/)
  assert.equal((await projectFile(root,path)).content, external)
})

test('implicit line revisions bind only the lines seen by the same owner', async t => {
  const root = await mkdtemp(join(tmpdir(), 'paper-owner-view-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path = 'paper/main.tex'
  const source = Array.from({length:60}, (_,i) => 'Line ' + i).join('\n')
  const {sha256} = await projectFile(root,path,source)
  const first=projectEditor(root), second=projectEditor(root)
  const args={path,start_line:4,end_line:4,content:'changed'}
  await first.read({path,start_line:1})
  await assert.rejects(second.write(args), /Read these numbered lines/)
  await assert.rejects(first.write({...args,end_line:41}), /Read these numbered lines/)
  await projectFile(root,path,'concurrent','Line 57')
  await assert.rejects(first.write(args), /changed since/)
  await first.read({path,start_line:3})
  await first.write(args)
  await assert.rejects(first.write(args), /Read these numbered lines/)
  await second.read({path,start_line:2,revision_sha256:sha256})
  await assert.rejects(second.write(args), /Read these numbered lines/)
  assert.equal((await projectFile(root,path)).content,source.replace('Line 3','changed').replace('Line 57','concurrent'))
})

test('owner can continue the research report and inspect only allowlisted read-only source records', async t => {
  const root = await mkdtemp(join(tmpdir(), 'paper-research-references-'))
  t.after(() => rm(root, {recursive:true, force:true}))
  const path='auto_research/state/deep_research.md', source='sources/0123456789abcdef01234567.txt'
  await mkdir(join(root,'auto_research/state'),{recursive:true})
  await mkdir(join(root,'sources'))
  await writeFile(join(root,path),'🌱'.repeat(28000)+'Prior work\nPrimary source')
  await writeFile(join(root,source),'Journal paper\nMeasured effect')
  assert.match((await readProjectFile(root,{path,offset:28000})).content,/Prior work/)
  assert.match((await readProjectFile(root,{path:source})).content,/Measured effect/)
  for (const key of ['auto_research/state/idea.md','auto_research/state/experiment_plan.yaml']) {
    await writeFile(join(root,key),'Use the prespecified measurement')
    assert.match((await readProjectFile(root,{path:key})).content,/prespecified measurement/)
    await assert.rejects(projectFile(root,key,'changed'), /Use paper/)
  }
  await assert.rejects(projectFile(root,path,'changed'), /Use paper/)
  await assert.rejects(projectFile(root,source,'changed'), /Use paper/)
  await assert.rejects(readProjectFile(root,{path:'auto_research/state/native_project.json'}), /Use paper/)
  await assert.rejects(readProjectFile(root,{path:'sources/credentials.txt'}), /Use paper/)
})

test('review amendments preserve unchanged checks and cannot cross revisions or set host fields', () => {
  const previous = {revision:'snapshot', verdict:'revise', checks:{visual:{status:'unassessed'}}, issues:[{id:'R1'}]}
  const check = {status:'assessed', evidence:[]}
  const edit = {amend:{checks:{experiment:check}}}
  assert.deepEqual(reviewSubmission(edit, previous, 'snapshot'),
    {...previous, checks:{visual:previous.checks.visual, experiment:check}})
  assert.deepEqual(Object.keys(previous.checks), ['visual'])
  assert.throws(() => reviewSubmission(edit, undefined, 'snapshot'))
  assert.throws(() => reviewSubmission(edit, previous, 'another-snapshot'))
  for (const bad of [{amend:{revision:'other'}}, {amend:{},revision:'snapshot'},
                     {amend:{checks:{unknown:check}}}, JSON.parse('{"amend":{"__proto__":{}}}')]) {
    assert.throws(() => reviewSubmission(bad, previous, 'snapshot'))
  }
})
