import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { chmod, cp, lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { projectFile, projectEditor, readProjectFile } from './project.js'

const bundle = dirname(fileURLToPath(import.meta.url))
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
  const data = [
    {type:'message_start',message:{id, model,usage:{input_tokens:10,output_tokens:0}}},
    {type:'content_block_start',index:0,content_block:isTool ? {type:'tool_use',id,name:content.name,input:{}} : {type:'text',text:''}},
    {type:'content_block_delta',index:0,delta:isTool ? {type:'input_json_delta',partial_json:JSON.stringify(content.args)} : {type:'text_delta',text:content}},
    {type:'content_block_stop',index:0},
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

// The test copies the profile and overlays only the candidate bundle's source.
test('pinned native owner, evidence-only reviewer and persisted resume', {
  skip: !process.env.IDEA2PAPER_TEST_PROFILE || !process.env.IDEA2PAPER_TEST_DSH,
  timeout: 90000,
}, async t => {
  const home = await mkdtemp(join(tmpdir(), 'idea2paper-native-test-'))
  if (!process.env.IDEA2PAPER_TEST_KEEP) t.after(() => rm(home, { recursive: true, force: true }))
  const profile = join(home, 'profiles', 'idea2paper')
  await mkdir(dirname(profile), { recursive: true })
  await cp(process.env.IDEA2PAPER_TEST_PROFILE, profile, { recursive: true })
  const pkg = join(profile, 'node_modules', 'idea2paper-dsh')
  for (const file of ['index.js','workflow.js','project.js','experiment.js','cordis.patch.yml','package.json']) await cp(join(bundle,file), join(pkg,file))
  let phase = 'review'
  let recorded, delivered = false, resumed = false, stopped = false, parked
  const blocked = Promise.withResolvers()
  const events = []
  const calls = []
  let failure
  const server = createServer(async (req,res) => {
    try {
      let raw = ''
      for await (const chunk of req) raw += chunk
      const body = JSON.parse(raw)
      const tools = body.tools?.map(tool => tool.name) || []
      const history = JSON.stringify(body.messages)
      if (parked && tools.includes('read_evidence')) {
        blocked.resolve()
        return // Hold the request open until DSH cancels the in-flight turn.
      }
      let response
      if (tools.includes('read_evidence')) {
        assert.deepEqual(tools, ['read_evidence'])
        assert.ok(history.includes('frozen-source-only-for-independent-reader'))
        assert.ok(!history.includes('parent-scientific-private'))
        assert.match(history, /Rendered manuscript page 2 of 2/)
        assert.match(history, /"type":"image"/)
        if (history.includes('independently-read-primary-passage')) {
          response = JSON.stringify({revision:'rev1', verdict:'accept', source:'sources/paper.txt'})
        } else if (history.includes('Cite an observed primary source')) {
          response = {name:'read_evidence',args:{artifact:'sources/paper.txt'}}
        } else if (history.includes('measured-observation')) {
          assert.match(history, /"type":"image"/, 'The provider must receive pixels for a visual receipt')
          response = JSON.stringify({ revision:'rev1', verdict:'accept' })
        } else response = { name:'read_evidence',args:{artifact:'paper/main.pdf',page:1,visual:true} }
      } else {
        assert.ok(tools.includes('request_review'), JSON.stringify(tools))
        if (parked) {
          response = {name:'request_review',args:{draft:true}}
        } else if (resumed) {
          assert.match(history, /parent-scientific-private/)
          assert.match(history, /What happened/)
          response = 'The recorded review accepted revision one.'
        } else if (phase === 'review') {
          phase = 'deliver'
          response = {name:'request_review',args:{}}
        } else if (phase === 'deliver') {
          phase = 'done'
          response = {name:'deliver',args:{}}
        } else response = 'The project delivered.'
      }
      res.writeHead(200, {'Content-Type':'text/event-stream'})
      res.end(frames(body.model, response))
    } catch (error) {
      failure = error
      res.writeHead(400)
      res.end(JSON.stringify({error:{message:error.message}}))
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const gateway = 'http://127.0.0.1:' + server.address().port
  await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
    {id:'agent-default-model',config:{provider:'loopback',model:'vision-fixture'}},
    {id:'llm-pi-ai',config:{providers:{loopback:{api:'anthropic-messages',baseURL:gateway,apiKeyEnv:'FIXTURE_KEY',
      models:[{id:'vision-fixture',contextWindow:65536,maxTokens:2048,input:['text','image']}]}}}},
  ]))
  const domain = ({op,args}) => {
    calls.push(op)
    switch (op) {
      case 'stop': stopped = true; return {state:'stopped'}
      case 'project_status': return { state:stopped?'stopped':delivered?'delivered':'running', delivered, commands:[] }
      case 'project_event': events.push(args); return {}
      case 'review_snapshot':
        assert.equal(args.preflight, !parked)
        return {revision:'rev1',rendered_pages:2,rendered_artifact:'paper/main.pdf',opening_evidence:['scripts/plot.py'],brief:'Blind reviewer. Inspect results/data.json using read_evidence.'}
      case 'review_evidence':
        if (args.artifact === 'scripts/plot.py') return {text:'frozen-source-only-for-independent-reader',receipt:{artifact:args.artifact,revision:'rev1'}}
        if (args.artifact === 'sources/paper.txt') return {text:'independently-read-primary-passage',receipt:{artifact:args.artifact,revision:'rev1'}}
        return {text:'measured-observation',receipt:{artifact:'paper/main.pdf',visual:true},
          mime_type:'image/png',image_base64:'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAIAAABvFaqvAAAAIklEQVR4nGNkYGhgoAZgooopowaNGjRq0KhBowaNGkQRAAAV1ACwA5vbQAAAAABJRU5ErkJggg=='}
      case 'check_review':
        return args.observed.some(r => r.artifact === 'sources/paper.txt') && args.verdict.source === 'sources/paper.txt'
          ? {valid:true} : {valid:false, diagnostic:'Cite an observed primary source'}
      case 'record_review':
        assert.equal(args.verdict.verdict, 'accept')
        assert.equal(args.verdict.source, 'sources/paper.txt')
        assert.ok(args.observed.some(receipt => receipt.artifact === 'scripts/plot.py'))
        assert.ok(args.observed.filter(receipt => receipt.artifact === 'paper/main.pdf' && receipt.visual).length >= 2)
        recorded = args
        return { accepted:true }
      case 'deliver': assert.ok(recorded); delivered=true; return {delivered}
      default: throw new Error('unexpected op ' + op)
    }
  }
  const project = join(home,'research')
  async function launch(...args) {
    const child = spawn(process.env.IDEA2PAPER_TEST_DSH, ['--profile','idea2paper',...args], {
      env: { HOME:home,PATH:process.env.PATH,DSH_HOME:home,
        PKG_NATIVE_CACHE_PATH:process.env.PKG_NATIVE_CACHE_PATH,DEEPSEEK_API_KEY:'fixture-only',FIXTURE_KEY:'localhost',
        DEEPSEEK_BASE_URL:'http://127.0.0.1:' + server.address().port,
        DSH_TELEMETRY_DISABLED:'1',IDEA2PAPER_DOMAIN_FD:'3' },
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
  }
  await launch('run',project,'parent-scientific-private')
  const first = JSON.parse(await readFile(join(project,'workflow.json'),'utf8'))
  assert.ok(delivered)
  assert.equal(calls.filter(op => op === 'review_snapshot').length,1)
  assert.equal(calls.filter(op => op === 'check_review').length,2)
  assert.notEqual(recorded.session_id,first.owner_session_id)
  assert.ok(events.some(e => e.role === 'reviewer' && e.kind === 'tool/call'))
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
