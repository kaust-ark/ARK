import { spawn } from 'node:child_process'
import { Socket } from 'node:net'
import { createHash, randomUUID } from 'node:crypto'
import { constants } from 'node:fs'
import { link, lstat, mkdir, open, readFile, readdir, realpath, rename, rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'

const packageDir = dirname(fileURLToPath(import.meta.url))
const writable = /^(?:paper\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.(?:tex|bib)|scripts\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.py)$/
const readable = /^(?:results\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.(?:json|csv|tsv|txt)|\.claude\/skills\/(?:[a-zA-Z0-9_-]+\/)*[a-zA-Z0-9_-]+\.md)$/

const mainSource = 'paper/main.tex'
const revisionParts = ['.dsh', 'native', 'main-tex-revisions']
const digest = data => createHash('sha256').update(data).digest('hex')
const revisionId = /^[a-f0-9]{64}$/

const writes = new Map()
export async function projectFile(directory, path, content, replace, edit = {}) {
  if (content === undefined && replace === undefined && edit.restore_sha256 === undefined) {
    if (Object.keys(edit).length) throw new Error('Supply content for an edit, or restore_sha256 for a manuscript restore.')
    return accessFile(directory, path)
  }
  const key = JSON.stringify([directory, path])
  const previous = writes.get(key) || Promise.resolve()
  const next = previous.catch(() => {}).then(() => accessFile(directory, path, content, replace, edit))
  writes.set(key, next)
  try { return await next } finally { if (writes.get(key) === next) writes.delete(key) }
}

// Keep full files inside the transport. Give the model a bounded view and the hash needed for a safe edit.
export async function readProjectFile(directory, { path, offset = 0, find, revision_sha256, list_revisions }) {
  if ((revision_sha256 !== undefined || list_revisions) && path !== mainSource) throw new Error('Only paper/main.tex has saved revisions.')
  if (list_revisions) {
    if (!Number.isSafeInteger(offset) || offset < 0) throw new Error('Offset must be a nonnegative revision index.')
    let entries
    try { entries = await readdir(await safeDirectory(directory, revisionParts, false), {withFileTypes:true}) }
    catch (error) { if (error.code !== 'ENOENT') throw error; entries = [] }
    const revisions = entries.filter(e => e.isFile() && revisionId.test(e.name)).map(e => e.name).sort()
    return {path, revisions:revisions.slice(offset, offset + 50), next_offset:offset + 50 < revisions.length ? offset + 50 : null}
  }
  const {content, sha256} = revision_sha256 === undefined
    ? await projectFile(directory, path) : await readRevision(directory, revision_sha256)
  const chars = Array.from(content)
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > chars.length) throw new Error('Offset is outside this file.')
  const result = {path, sha256, total_chars: chars.length}
  if (find !== undefined) {
    if (typeof find !== 'string' || !find || find.length > 500) throw new Error('Search for a passage of 1 to 500 characters.')
    const utf16Offset = chars.slice(0, offset).join('').length
    const hit = content.indexOf(find, utf16Offset)
    result.found = hit >= 0
    if (hit < 0) return {...result, content: ''}
    result.match_offset = Array.from(content.slice(0, hit)).length
    offset = Math.max(offset, result.match_offset - 300)
  }
  const end = Math.min(offset + 6000, chars.length)
  return {...result, offset, content: chars.slice(offset, end).join(''), next_offset: end < chars.length ? end : null}
}

async function safeDirectory(directory, parts, create) {
  let parent = directory
  for (const part of parts) {
    parent = join(parent, part)
    if (create) await mkdir(parent, { mode: 0o700 }).catch(e => { if (e.code !== 'EEXIST') throw e })
    if (!(await lstat(parent)).isDirectory() || await realpath(parent) !== parent) throw new Error('Unsafe project directory')
  }
  return parent
}

async function readSource(target, expectedDigest) {
  const file = await open(target, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const stat = await file.stat()
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > 512 * 1024) throw new Error('Expected a small regular project file')
    const bytes = await file.readFile()
    const sha256 = digest(bytes)
    if (expectedDigest !== undefined && ((stat.mode & 0o222) || sha256 !== expectedDigest)) {
      throw new Error('Saved manuscript revision failed its read-only or SHA-256 check.')
    }
    return {content:new TextDecoder('utf-8', {fatal:true, ignoreBOM:true}).decode(bytes), sha256}
  } finally { await file.close() }
}

async function readRevision(directory, id) {
  if (typeof id !== 'string' || !revisionId.test(id)) throw new Error('Expected a saved manuscript SHA-256.')
  const parent = await safeDirectory(directory, revisionParts, false)
  return readSource(join(parent, id), id)
}

async function saveRevision(directory, previous) {
  const parent = await safeDirectory(directory, revisionParts, true)
  const target = join(parent, previous.sha256)
  try { await readSource(target, previous.sha256); return }
  catch (error) { if (error.code !== 'ENOENT') throw error }
  const temp = join(parent, '.' + randomUUID())
  const file = await open(temp, 'wx', 0o600)
  try {
    try {
      await file.writeFile(previous.content)
      await file.chmod(0o400)
      await file.sync()
    } finally { await file.close() }
    try { await link(temp, target) } catch (error) { if (error.code !== 'EEXIST') throw error }
  } finally { await rm(temp, {force:true}) }
  await readSource(target, previous.sha256)
  const folder = await open(parent, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW)
  try { await folder.sync() } finally { await folder.close() }
}

function completeDocument(source) {
  // Skip comments, escaped characters and literal TeX examples when looking for the document boundaries.
  const tokens = /%[^\r\n]*|\\verb\*?(?<delimiter>[^a-zA-Z\s])[^\r\n]*?\k<delimiter>|\\begin\{(?<literal>verbatim\*?|Verbatim|lstlisting|minted)\}[\s\S]*?\\end\{\k<literal>\}|\\(?<boundary>begin|end)\s*\{\s*document\s*\}|\\[\s\S]/g
  let began = false
  for (const match of source.matchAll(tokens)) {
    if (match.groups.boundary === 'begin') began = true
    if (began && match.groups.boundary === 'end') return true
  }
  return false
}

async function accessFile(directory, path, content, replace, edit = {}) {
  const restoring = edit.restore_sha256 !== undefined
  const writing = restoring || content !== undefined || replace !== undefined
  if (typeof path !== 'string' || path.length > 240 || !(writable.test(path) || (!writing && readable.test(path)))) {
    throw new Error('Use paper/*.tex, paper/*.bib or scripts/*.py for authoring, or read a named result or project skill.')
  }
  if (restoring && (path !== mainSource || !edit.base_sha256 || content !== undefined || replace !== undefined ||
      edit.insert_before !== undefined || edit.insert_after !== undefined || edit.expected_matches !== undefined || edit.allow_shrink !== undefined)) {
    throw new Error('To restore paper/main.tex, supply only restore_sha256 and the current base_sha256.')
  }
  const parts = path.split('/')
  const name = parts.pop()
  const parent = await safeDirectory(directory, parts, writing)
  const target = join(parent, name)
  if (!writing) return {path, ...await readSource(target)}
  if (edit.insert_before && edit.insert_after) throw new Error('Choose a single insertion anchor.')
  const anchor = edit.insert_before ?? edit.insert_after
  if (edit.expected_matches !== undefined && (replace === undefined || anchor !== undefined ||
      !Number.isSafeInteger(edit.expected_matches) || edit.expected_matches < 1 || edit.expected_matches > 50)) {
    throw new Error('expected_matches requires an exact replacement and a count between 1 and 50.')
  }
  if (anchor !== undefined) {
    if (replace !== undefined || typeof anchor !== 'string' || !anchor || typeof content !== 'string') throw new Error('An insertion needs one exact anchor and new content.')
    replace = anchor
    content = edit.insert_before ? content + anchor : anchor + content
  }
  let previous
  if (path === mainSource || replace !== undefined) {
    try { previous = await readSource(target) }
    catch (error) { if (error.code !== 'ENOENT' || replace !== undefined) throw error }
  }
  if (path === mainSource && edit.base_sha256 !== undefined && previous?.sha256 !== edit.base_sha256) {
    throw new Error('Manuscript changed since it was read. Reopen the current revision.')
  }
  if (restoring) content = (await readRevision(directory, edit.restore_sha256)).content
  if (replace !== undefined) {
    if (typeof content !== 'string' || typeof replace !== 'string' || !replace || Buffer.byteLength(replace) > 512 * 1024) {
      throw new Error('A section edit needs its exact existing passage and the new content.')
    }
    const passages = previous.content.split(replace)
    const found = passages.length - 1
    const expected = edit.expected_matches ?? 1
    if (found !== expected) {
      throw new Error(`The requested passage must occur ${expected === 1 ? 'exactly once' : expected + ' times'}, found ${found}. Read the current file. Use a longer unique passage, or set expected_matches to replace a known number of identical passages.`)
    }
    content = passages.join(content)
  }
  if (typeof content !== 'string' || Buffer.byteLength(content) > 512 * 1024) throw new Error('Author files must be UTF-8 text under 512 KiB.')
  const result = {path, bytes:Buffer.byteLength(content)}
  if (path === mainSource && previous && previous.content !== content) {
    if (!restoring && completeDocument(previous.content) && !completeDocument(content)) {
      throw new Error('Edit would replace a complete paper/main.tex with an incomplete document. Use an anchored section edit or supply the complete LaTeX document.')
    }
    const fullRewrite = replace === undefined || previous.content.trim() === replace.trim()
    if (!restoring && fullRewrite && previous.content.length > 3500 && content.length < previous.content.length * 0.80 &&
        (!edit.base_sha256 || edit.allow_shrink !== true)) {
      throw new Error('This full rewrite removes over one fifth of the manuscript. Use a section edit, or deliberately pass both the current base_sha256 and allow_shrink:true.')
    }
    await saveRevision(directory, previous)
    result.previous_sha256 = previous.sha256
  }
  if (path === mainSource) result.sha256 = digest(content)
  const temp = join(parent, '.idea2paper-' + randomUUID())
  try {
    const file = await open(temp, 'wx', 0o600)
    try { await file.writeFile(content); await file.sync() } finally { await file.close() }
    await rename(temp, target)
  } finally { await rm(temp, { force: true }) }
  return result
}

// Requests are correlated by id so a status check cannot consume a tool response.
export function openDomain(directory, signal) {
  signal.throwIfAborted()
  const interpreter = process.env.IDEA2PAPER_PYTHON || 'python3'
  const args = process.env.IDEA2PAPER_PYTHON
    ? ['-I', '-u', '-m', 'ark.orchestrator.native_domain']
    : ['-I', '-u', join(packageDir, 'domain_bootstrap.py')]
  const fd = process.env.IDEA2PAPER_DOMAIN_FD
  if (fd !== undefined && (!/^\d+$/.test(fd) || Number(fd) < 3)) throw new Error('Invalid inherited domain socket')
  const socket = fd === undefined ? undefined : new Socket({ fd: Number(fd) })
  const child = socket ? undefined : spawn(interpreter, args, { cwd: directory, stdio: ['pipe', 'pipe', 'inherit'], signal })
  const input = socket || child.stdout
  const output = socket || child.stdin
  const endpoint = socket || child
  const pending = new Map()
  const lines = createInterface({ input, crlfDelay: Infinity })
  let failed
  const fail = reason => {
    failed ??= reason
    for (const request of pending.values()) request.reject(failed)
    pending.clear()
  }
  const closed = new Promise(resolveClosed => endpoint.once('close', (code, sig) => {
    fail(new Error('Idea2Paper domain helper closed (' + (code ?? sig) + ')'))
    resolveClosed()
  }))
  const stop = () => socket?.destroy(new Error('Project interrupted'))
  signal.addEventListener('abort', stop, { once: true })
  const terminate = () => socket ? socket.destroy() : child.kill()
  endpoint.on('error', fail)
  output.on('error', fail)
  lines.on('line', line => {
    let reply
    try { reply = JSON.parse(line) } catch {
      fail(new Error('Domain helper returned invalid JSON')); terminate(); return
    }
    const request = pending.get(reply.id)
    if (!request) { fail(new Error('Unexpected domain reply id')); terminate(); return }
    pending.delete(reply.id)
    if (reply.ok === true) request.resolve(reply.result)
    else request.reject(new Error(typeof reply.error === 'string' ? reply.error : JSON.stringify(reply.error)))
  })
  function call(op, args = {}) {
    signal.throwIfAborted()
    if (failed) return Promise.reject(failed)
    const id = randomUUID()
    return new Promise((resolveRequest, reject) => {
      pending.set(id, { resolve: resolveRequest, reject })
      output.write(JSON.stringify({ id, op, args }) + '\n')
    })
  }
  return { call, async close() {
    signal.removeEventListener('abort', stop)
    if (socket) socket.destroy()
    else output.end()
    await closed
    lines.close()
  } }
}

const str = { type: 'string' }
const num = { type: 'integer' }
const bool = { type: 'boolean' }
const strings = { type: 'array', items: str }
const object = { type: 'object', additionalProperties: true }

export const operations = {
  project_status: ['Read current research state, outstanding prerequisites, budgets, skills and artifact paths.', {}],
  lookup_papers: ['Find primary bibliographic records, abstracts, and canonical BibTeX.', { query: str, limit: num }, ['query']],
  research: ['Retrieve or reconcile the requested research report and primary sources.', { query: str }],
  plan_experiment: ['Register the controls and script before running the comparison. Declare a relative results/*.json output.', {
    question: str, baseline: str, comparison: str, metric: str, controls: strings, script: str, expected: str,
  }, ['question', 'baseline', 'comparison', 'metric', 'controls', 'script', 'expected']],
  run_experiment: ['Run or reconcile the registered experiment. The isolated script prints its measured JSON result to stdout.', {}],
  render_results_table: ['Typeset measured results from the worker JSON. Call with no columns to discover available rows and fields. Include the returned file in a LaTeX table.', {
    name: str, rows_pointer: str, columns: { type: 'array', items: { type: 'object', properties: {
      path: str, label: str, digits: num, percent: bool }, required: ['path'] } },
  }],
  render_plot: ['Execute a project plotting script in the evidence backend to render a named measured plot.', { script: str, filename: str }, ['script', 'filename']],
  generate_concept_figure: ['Generate a labelled method illustration from its description, caption and optional grouping.', { name: str, caption: str, description: str, groups: strings }, ['name', 'caption', 'description']],
  verify_citations: ['Check the manuscript bibliography against primary bibliographic records.', {}],
  compile_paper: ['Compile and inspect the current manuscript and return page measurements and artifacts.', {}],
  request_review: ['Request an independent, blind submission review after artifact checks. Use draft only for early scientific feedback on an incomplete manuscript.', { draft: bool }],
  deliver: ['Verify the current reviewed revision and publish the checked project deliverables.', {}],
  read_project_file: ['Read up to 6000 characters of a project source, measured result, or installed skill. Supply next_offset as offset to continue, or find an exact passage. sha256 covers the entire file. For paper/main.tex, list_revisions lists saved digests and revision_sha256 reads a saved version.', { path: str, offset: num, find: str, revision_sha256: str, list_revisions: bool }, ['path']],
  write_project_file: ['Write a manuscript or script. For a section edit use replace, insert_before or insert_after with an exact unique passage or heading. Use expected_matches only to correct a known count of identical passages. A substantial full shrink of paper/main.tex needs its current base_sha256 and allow_shrink:true. Each changed manuscript saves previous_sha256. To restore it, supply only path, current base_sha256 and restore_sha256.', { path: str, content: str, replace: str, expected_matches: num, insert_before: str, insert_after: str, base_sha256: str, allow_shrink: bool, restore_sha256: str }, ['path']],
}
export const evidenceTool = ['Inspect one named artifact from the blind review packet. Request an exact PDF page or image when checking visual presentation.', {
  artifact: str, page: num, start: num, visual: bool, statistic: str, column: str, rows_pointer: str,
}, ['artifact']]

function render(_args, value) {
  const { attachment, ...details } = value
  const blocks = [{ type: 'text', text: JSON.stringify(details) }]
  if (attachment) blocks.push({ type: 'image', attachment })
  return blocks
}
export function registerTool(scope, name, [description, properties, required = []], execute, timeoutMs) {
  scope.tools.register({
    name, description, parameters: { type: 'object', properties, required, additionalProperties: false },
    output: { schema: object, render }, execute, timeoutMs,
  })
}
export async function attachEvidence(scope, agent, args, signal, read) {
  if (args.visual) {
    const route = agent.session.requestHeader()?.config ?? agent.options
    const model = await scope.llm.resolveModelInfo(route.provider, route.model, signal)
    if (!model.inputModalities?.includes('image') || !scope.get('attachments')) throw new Error('Visual inspection unavailable for this model or attachment store. Record the check as unassessed.')
  }
  const { image_base64, mime_type, ...value } = await read()
  if (!image_base64) {
    if (args.visual) throw new Error('Requested pixels unavailable. Record the visual check as unassessed.')
    return value
  }
  const attachment = await scope.attachments.saveImage({ data: Buffer.from(image_base64, 'base64'), mediaType: mime_type, name: args.artifact.split('/').pop() })
  return { ...value, attachment }
}

const commandsFolder = directory => join(directory, '.dsh', 'native', 'commands')

export async function queueCommand(directory, text, id = randomUUID()) {
  if (!/^[a-z0-9][a-z0-9_-]{0,95}$/i.test(id) || typeof text !== 'string' || !text.trim() || text.length > 32_000) throw new Error('Invalid command id or message')
  const folder = commandsFolder(directory)
  await mkdir(folder, { recursive: true, mode: 0o700 })
  const path = join(folder, id + '.json')
  // Publish the complete message with a no-clobber hard link. Reusing an id
  // verifies the original content, even after its DSH admission was acknowledged.
  const temp = join(folder, '.' + randomUUID() + '.tmp')
  const file = await open(temp, 'wx', 0o600)
  try {
    try { await file.writeFile(JSON.stringify({ id, text })); await file.sync() } finally { await file.close() }
    try { await link(temp, path) } catch (error) {
      if (error.code !== 'EEXIST') throw error
      if (JSON.parse(await readFile(path, 'utf8')).text !== text) throw new Error('Command id already belongs to another message')
    }
  } finally { await rm(temp, { force: true }) }
  return id
}

export async function pendingCommands(directory) {
  const folder = commandsFolder(directory)
  let names
  try { names = new Set(await readdir(folder)) } catch (error) { if (error.code === 'ENOENT') return []; throw error }
  const commands = []
  for (const name of names) {
    if (!name.endsWith('.json') || names.has(name + '.ack')) continue
    const row = JSON.parse(await readFile(join(folder, name), 'utf8'))
    commands.push({ id: 'local-' + row.id, text: row.text })
  }
  return commands
}

export async function acknowledgeLocal(directory, id) {
  const file = await open(join(commandsFolder(directory), id.slice(6) + '.json.ack'), 'a', 0o600)
  await file.close()
}
