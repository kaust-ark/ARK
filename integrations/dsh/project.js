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

const researchSource = /^(?:auto_research\/state\/(?:idea\.md|experiment_plan\.yaml|deep_research\.md|reviews\/[a-f0-9]{64}\/review\.json)|sources\/[a-f0-9]{24}\.txt)$/

const mainSource = 'paper/main.tex'
const revisionParts = ['.dsh', 'native', 'main-tex-revisions']
const digest = data => createHash('sha256').update(data).digest('hex')
const revisionId = /^[a-f0-9]{64}$/

const writes = new Map()
export async function projectFile(directory, path, content, replace, edit = {}, checkManuscript) {
  if (content === undefined && replace === undefined && edit.restore_sha256 === undefined) {
    if (Object.keys(edit).length) throw new Error('Supply content for an edit, or restore_sha256 for a manuscript restore.')
    return accessFile(directory, path)
  }
  const key = JSON.stringify([directory, path])
  const previous = writes.get(key) || Promise.resolve()
  const next = previous.catch(() => {}).then(() => accessFile(directory, path, content, replace, edit, checkManuscript))
  writes.set(key, next)
  try { return await next } finally { if (writes.get(key) === next) writes.delete(key) }
}

// Keep full files inside the transport. Give the model a bounded view and the hash needed for a safe edit.
export async function readProjectFile(directory, { path, offset = 0, find, start_line, revision_sha256, list_revisions }) {
  if ((revision_sha256 !== undefined || list_revisions) && path !== mainSource) throw new Error('Only paper/main.tex has saved revisions.')
  if (start_line !== undefined && (offset !== 0 || find !== undefined || list_revisions || !Number.isSafeInteger(start_line) || start_line < 1))
    throw new Error('Use a one-based start_line on its own to view numbered source lines.')
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
  if (start_line !== undefined) {
    const lines = sourceLines(content)
    if (start_line > lines.length) throw new Error('Requested line is outside the file.')
    const numbered = []
    let size = 0
    for (let i = start_line - 1; i < lines.length && numbered.length < 40; i++) {
      const line = Array.from(lines[i].replace(/\r?\n$/, ''))
      if (line.length > 6000 && !numbered.length) {
        throw new Error('This line exceeds 6000 characters. Read it with offset and use an exact passage edit.')
      }
      if (size + line.length + 1 > 6001) break
      numbered.push({line:i + 1, text:line.join('')})
      size += line.length + 1
    }
    const next = start_line + numbered.length
    return {...result, lines:numbered, next_line:next <= lines.length ? next : null}
  }
  if (find !== undefined) {
    if (typeof find !== 'string' || !find || find.length > 500) throw new Error('Search for a passage of 1 to 500 characters.')
    const utf16Offset = chars.slice(0, offset).join('').length
    const hit = content.indexOf(find, utf16Offset)
    result.found = hit >= 0
    if (hit < 0) return {...result, content: ''}
    result.match_offset = Array.from(content.slice(0, hit)).length
    result.match_line = content.slice(0, hit).split('\n').length
    offset = Math.max(offset, result.match_offset - 300)
  }
  const end = Math.min(offset + 6000, chars.length)
  return {...result, offset, content: chars.slice(offset, end).join(''), next_offset: end < chars.length ? end : null}
}

// A numbered read binds subsequent line edits to the bytes actually shown in this owner session.
export function projectEditor(directory) {
  let view
  return {
    async read(args) {
      const result = await readProjectFile(directory, args)
      if (args.path === mainSource && args.revision_sha256 === undefined && result.lines?.length) {
        view = {sha256:result.sha256, first:result.lines[0].line, last:result.lines.at(-1).line}
      }
      return result
    },
    async write(args, checkManuscript) {
      let edit = args
      if (args.path === mainSource && args.start_line !== undefined && args.base_sha256 === undefined) {
        if (!view || args.start_line < view.first || args.end_line > view.last)
          throw new Error('Read these numbered lines with start_line before editing, or supply their current base_sha256.')
        edit = {...args, base_sha256:view.sha256}
      }
      const result = await projectFile(directory, args.path, args.content, args.replace, edit, checkManuscript)
      if (args.path === mainSource) view = undefined
      return result
    },
  }
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

function sourceLines(content) {
  return content.match(/[^\n]*\n|[^\n]+$/g) || []
}

async function accessFile(directory, path, content, replace, edit = {}, checkManuscript) {
  const restoring = edit.restore_sha256 !== undefined
  const writing = restoring || content !== undefined || replace !== undefined
  if (typeof path !== 'string' || path.length > 240 || !(writable.test(path) || (!writing && (readable.test(path) || researchSource.test(path))))) {
    throw new Error('Use paper/*.tex, paper/*.bib or scripts/*.py for authoring, or read the original brief, approved plan, named result, project skill, Deep Research report or verified source passage.')
  }
  if (restoring && (path !== mainSource || !edit.base_sha256 || content !== undefined || replace !== undefined ||
      edit.insert_before !== undefined || edit.insert_after !== undefined || edit.expected_matches !== undefined || edit.allow_shrink !== undefined)) {
    throw new Error('To restore paper/main.tex, supply only restore_sha256 and the current base_sha256.')
  }
  const range = edit.start_line !== undefined || edit.end_line !== undefined
  if (range && (path !== mainSource || !revisionId.test(edit.base_sha256 || '') || restoring ||
      replace !== undefined || edit.insert_before !== undefined || edit.insert_after !== undefined ||
      edit.expected_matches !== undefined || !Number.isSafeInteger(edit.start_line) || edit.start_line < 1 ||
      !Number.isSafeInteger(edit.end_line) || edit.end_line < edit.start_line)) {
    throw new Error('For a line edit of paper/main.tex supply base_sha256, inclusive start_line and end_line, and replacement content.')
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
  if (range) {
    if (typeof content !== 'string') throw new Error('A line edit requires replacement text.')
    const lines = sourceLines(previous.content)
    if (edit.end_line > lines.length) throw new Error('The line range is outside the current manuscript.')
    const terminator = lines[edit.end_line - 1].match(/\r?\n$/)?.[0]
    if (content && terminator && !content.endsWith('\n')) content += terminator
    content = lines.slice(0, edit.start_line - 1).join('') + content + lines.slice(edit.end_line).join('')
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
      if (found === 0 && path === mainSource) {
        const headings = sourceLines(previous.content).flatMap((line, index) =>
          /^\s*\\(?:sub){0,2}section\*?(?:\[[^\]]*\])?\{/.test(line)
            ? [`${index + 1} ${line.trim().slice(0, 140)}`] : []).slice(0, 32)
        const outline = headings.length ? '\nCurrent section lines:\n' + headings.join('\n') : ''
        throw new Error(`The requested passage must occur ${expected === 1 ? 'exactly once' : expected + ' times'}, found 0. Read the intended section ` +
          'with start_line, then edit its observed lines using start_line, end_line and base_sha256.' + outline)
      }
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
    if (!restoring && previous.content.length > 3500 && content.length < previous.content.length * 0.80 &&
        (!edit.base_sha256 || edit.allow_shrink !== true)) {
      throw new Error(`This edit would shrink paper/main.tex from ${previous.content.length} to ${content.length} characters. Narrow the edit to the intended passage. For a deliberate cut of over one fifth, pass the current base_sha256 and allow_shrink:true.`)
    }
    if (!restoring && checkManuscript && completeDocument(previous.content)) {
      const check = await checkManuscript({content, base_sha256:previous.sha256})
      if (check?.ok !== true) throw new Error('The manuscript edit was not saved. ' + (check?.diagnostic || 'TeX validation was unavailable.'))
      if (check.first_pass_preview) result.first_pass_preview = check.first_pass_preview
    }
    await saveRevision(directory, previous)
    result.previous_sha256 = previous.sha256
  }
  if (path === mainSource) result.sha256 = digest(content)
  const temp = join(parent, '.idea2paper-' + randomUUID())
  try {
    const file = await open(temp, 'wx', 0o600)
    try { await file.writeFile(content); await file.sync() } finally { await file.close() }
    if (path === mainSource && previous && (await readSource(target)).sha256 !== previous.sha256) {
      throw new Error('Manuscript changed during the edit. Read the current revision and try again.')
    }
    await rename(temp, target)
  } finally { await rm(temp, { force: true }) }
  return result
}

// Requests are correlated by id so a status check cannot consume a tool response.
export function openDomain(directory, signal, { detached = false } = {}) {
  signal.throwIfAborted()
  const interpreter = process.env.IDEA2PAPER_PYTHON || 'python3'
  const args = process.env.IDEA2PAPER_PYTHON
    ? ['-I', '-u', '-m', 'ark.orchestrator.native_domain']
    : ['-I', '-u', join(packageDir, 'domain_bootstrap.py')]
  const fd = process.env.IDEA2PAPER_DOMAIN_FD
  if (fd !== undefined && (!/^\d+$/.test(fd) || Number(fd) < 3)) throw new Error('Invalid inherited domain socket')
  const socket = fd === undefined ? undefined : new Socket({ fd: Number(fd) })
  const child = socket ? undefined : spawn(interpreter, args, { cwd: directory, stdio: ['pipe', 'pipe', 'inherit'], signal, detached })
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
const plotField = {type:'object', properties:{path:str, label:str}, required:['path']}

export const operations = {
  project_status: ['Read current research state, outstanding prerequisites, budgets, skills and artifact paths.', {}],
  lookup_papers: ['Find primary records, abstracts and canonical BibTeX. Set add_to_bibliography with an exact title or DOI to save that work and receive its cite key for the manuscript.', { query: str, limit: num, add_to_bibliography: bool }, ['query']],
  research: ['Retrieve or reconcile the requested research report and primary sources.', { query: str }],
  plan_experiment: ['Register the comparison and its two file paths. When the study aims to explain why an outcome changes, include mechanism so the owner can approve the distinguishing measurement or a narrower conclusion. A descriptive study needs only its comparison. For a correction, reread auto_research/state/idea.md, check the disputed calculation on a discriminating input, and choose a fresh result_path. The script prints its JSON measurements to stdout.', {
    question: str,
    baseline: {type:'string', description:'The reference method, configuration or condition.'},
    comparison: {type:'string', description:'What changes in the comparison condition relative to the baseline.'},
    metric: {type:'string', description:'What is measured and in what units.'},
    controls: {type:'array', items:{type:'string'}, description:'Data, splits, settings or other conditions held fixed across the comparison.'},
    mechanism: {type:'object', additionalProperties:false, properties:{
      explanation:str, alternative:str,
      distinguishing_observation:{type:'string', description:'Which observation would differ under these explanations? Specify the measure and their predictions.'},
      claim_without_it:{type:'string', description:'The strongest conclusion the primary comparison alone can support.'},
      measurement:{type:'object', additionalProperties:false, properties:{
        rows_pointer:{type:'string',description:'JSON pointer to the planned table, or an empty string for a root array.'},
        columns:{type:'array',items:str,minItems:1,maxItems:16,description:'Numeric fields that will distinguish the explanations. Dotted paths address nested values.'},
      },required:['rows_pointer','columns']},
    }, required:['explanation','alternative','distinguishing_observation','claim_without_it']},
    script: {type:'string', description:'One project-relative Python filename, for example scripts/experiment.py.'},
    result_path: {type:'string', description:'One project-relative JSON output filename, for example results/measurements.json.'},
  }, ['question', 'baseline', 'comparison', 'metric', 'controls', 'script', 'result_path']],
  run_experiment: ['Run or reconcile the registered experiment. The isolated script writes measured JSON to the registered relative results path or prints it to stdout.', {}],
  render_results_table: ['Inspect the sealed worker rows. With summarize=<numeric field> get mean, minimum, maximum and sample standard deviation across the rows. Add reference=<second field> for paired differences (value minus reference) and percentage change in means. To check each condition separately, also supply condition_columns naming the fields that identify each row. Each row gives the signed difference and (abs(reference) - abs(value)) / abs(reference), null at a zero reference. Interpret this fraction as error reduction only for errors measured from zero. With columns, typeset the rows and include the returned file in a LaTeX table. With no arguments discover fields.', {
    name: str, rows_pointer: str, summarize: str, reference: str, condition_columns: strings, columns: { type: 'array', items: { type: 'object', properties: {
      path: str, label: str, digits: num, percent: bool }, required: ['path'] } },
  }],
  render_plot: ['Plot sealed worker measurements. Call with no arguments to list columns. For a line, point or grouped bar plot provide filename, kind, x and series, with optional rows_pointer and y_label. Each path names a measured field. For a custom plot provide script and filename; read /study/results.json and save /output/<filename>.', {
    filename: str, rows_pointer: str, kind: {type:'string', enum:['line','points','bar']},
    x: plotField, series: {type:'array', items:plotField}, y_label: str, script: str,
  }],
  generate_concept_figure: ['Illustrate the scientific question. For paired conditions, side-by-side comparisons or a method overview, use compare_conditions (the default). The approved baseline and comparison are typeset automatically. Use the returned caption. For explain_mechanism, name the process in description. The tool draws the approved protocol, compared operations and readout beside wordless subject artwork. To specify a sequence, choose two or three mechanism_steps in order using approved field names such as controls[0], comparison or metric.', {
    name: str, caption: str, description: str,
    purpose: {type:'string', enum:['compare_conditions','explain_mechanism']}, mechanism_steps: strings,
    representation: {type:'string', enum:['study_panel','schematic'], description:'Legacy format for older callers. New calls use purpose.'},
  }, ['name', 'caption', 'description']],
  verify_citations: ['Check the manuscript bibliography against primary bibliographic records.', {}],
  compile_paper: ['Compile the current manuscript and inspect its figures, citations and layout. manuscript_revision identifies main.tex after typesetting adjustments.', {}],
  request_review: ['Request an independent, blind submission review after artifact checks. Use draft only for early scientific feedback on an incomplete manuscript. For a delivery request, follow acceptance with deliver and report its result. For a review-only request, report the findings.', { draft: bool }],
  deliver: ['Verify the current reviewed revision and publish the checked project deliverables.', {}],
  read_project_file: ['Read up to 6000 characters of a manuscript, result, skill, original brief (auto_research/state/idea.md), experiment_plan.yaml or deep_research.md in that directory, sources/<id>.txt from verify_citations, or the signed_record returned by review. Supply next_offset as offset to continue, or find a passage to get its match_line. Use start_line to read up to 40 numbered lines. sha256 covers the entire file. For paper/main.tex, list_revisions lists saved digests and revision_sha256 reads a saved version.', { path: str, offset: num, find: str, start_line: num, revision_sha256: str, list_revisions: bool }, ['path']],
  write_project_file: ['Write a manuscript or script. To edit lines of paper/main.tex, read the numbered lines and send inclusive start_line/end_line with just the new text. The last numbered view supplies its revision; alternatively provide the exact current base_sha256. A TeX error introduced into a compiling draft rejects the edit. For a section edit use replace, insert_before or insert_after with an exact unique passage or heading. Use expected_matches only to correct a known count of identical passages. Any edit that cuts over one fifth of a manuscript longer than 3500 characters needs its current base_sha256 and allow_shrink:true. Each changed manuscript saves previous_sha256. To restore it, supply only path, current base_sha256 and restore_sha256.', { path: str, content: str, start_line: num, end_line: num, replace: str, expected_matches: num, insert_before: str, insert_after: str, base_sha256: str, allow_shrink: bool, restore_sha256: str }, ['path']],
}
// The reviewer may revise its own draft during this activation.
// The original DSH repair message is durable. Map its generated opening instruction to a public topic.
const repairInstructions = {
  citations: ["Quote the authors' scientific statement from sources/", 'Unobserved or unlocatable quote in sources/',
    'Cite an observed primary source'],
  findings: ['Finding IDs', 'Expected a review finding', 'Recheck every open finding',
    'Invalid status, severity or domain', 'The proposed verdict disagrees with the open findings'],
  experiment: ['Trace the measured quantity', 'Use reproduce_measurement',
    'The verdict must retain all executed numerical controls', 'The reproduced row differs'],
  visual: ['Trace the plotted values', 'Rendered layout was not observed', 'For each figure in figure_methods'],
  manuscript: ['Separate the result from its explanation', 'Expected a nonempty bridge from the derivation',
    'Choose a separate observed PDF reasoning block', 'Use artifact, page and the printed one-based PDF text block',
    "Check the submission's derivation", 'Unobserved or unlocatable quote in paper/main.pdf'],
}

export function reviewRepairTopic(message) {
  if (typeof message !== 'string') return
  const instruction = message.match(/^Review evidence check: ([^\n:]+)/)?.[1]
  if (!instruction) return
  if (/^Finding R[1-9][0-9]*, PDF page [1-9][0-9]*, remaining sentence /.test(instruction)) return 'findings'
  const domain = instruction.match(/\bchecks\.(manuscript|experiment|citations|visual)\b/)?.[1]
  if (domain) return domain
  for (const [topic, openings] of Object.entries(repairInstructions)) {
    if (openings.some(opening => instruction.startsWith(opening))) return topic
  }
}

// A rewritten verdict or a repeated read of the same window supplies no new observation.
export function nextReviewRepair(previous, output, diagnostic, observed) {
  const observations = new Set(observed.map(receipt => JSON.stringify(receipt))).size
  const sameEvidence = previous?.observations === observations
  if (sameEvidence && previous.output === output) {
    throw new Error('Repeated unsupported review without further evidence: ' + diagnostic)
  }
  const repeats = sameEvidence && previous.diagnostic === diagnostic ? previous.repeats + 1 : 1
  if (repeats >= 4) {
    throw new Error('Independent review remains unsigned. Three correction requests returned to the same check without new evidence. ' + diagnostic)
  }
  return {output, diagnostic, observations, repeats}
}

export function reviewSubmission(message, previous, revision) {
  if (!message || !Object.hasOwn(message, 'amend')) return message
  const patch = message.amend
  if (!previous || previous.revision !== revision || Object.keys(message).length !== 1 ||
      !patch || typeof patch !== 'object' || Array.isArray(patch) || !Object.keys(patch).length) {
    throw new Error('An amendment requires an earlier verdict for this revision and one amend object.')
  }
  const allowed = ['summary', 'alternative', 'strengths', 'checks', 'issues', 'verdict', 'score']
  if (Object.keys(patch).some(field => !allowed.includes(field))) {
    throw new Error('Amend only verdict fields. The submission revision is fixed.')
  }
  const updated = {...previous, ...patch}
  if (Object.hasOwn(patch, 'checks')) {
    const checks = patch.checks
    const domains = ['manuscript', 'experiment', 'citations', 'visual']
    if (!checks || typeof checks !== 'object' || Array.isArray(checks) || !Object.keys(checks).length ||
        Object.keys(checks).some(domain => !domains.includes(domain))) {
      throw new Error('In amend.checks, supply complete replacement checks under their domain names.')
    }
    updated.checks = {...previous.checks, ...checks}
  }
  return updated
}

export const evidenceTool = ['Inspect a frozen artifact. For a directional or threshold claim about paired JSON measurements, use statistic=paired with column, reference, condition_columns and optional rows_pointer. Each JSON line gives one condition, the signed difference (value minus reference) and (abs(reference) - abs(value)) / abs(reference), null at a zero reference. Quote full lines and follow next_start for additional conditions. For a disputed explanation, find a distinctive phrase throughout the rendered PDF. The first matching page returns quotable text. Open other matching_pages by page and start to compare the claims; next_search_page continues the search. Use visual for the actual page image.', {
  artifact: str, page: num, start: num, visual: bool, statistic: str, column: str, reference: str, condition_columns: strings, rows_pointer: str, find: str,
}, ['artifact']]

export const methodControlTool = ['Test a calculation from the frozen worker. Use the target and input names shown in result_column_targets for an emitted result column. You can also select a top-level worker function or quote an exact return or assignment calculation. The target must exist in the frozen worker. Bind its operands in inputs_json and list array_inputs needing NumPy conversion. For a calculated scalar result use prediction_program: define reproduce(inputs) returning {"expected": number, "alternative": number}. Derive both from those exact inputs according to the intended definition and a competing definition. Your program runs first in a separate CPU sandbox without the worker source or result. For simple exact predictions or arrays, give expected_json and alternative_json instead. Explain why the predictions differ in rationale.', {
  target: str, inputs_json: str, array_inputs: strings, prediction_program: str, expected_json: str, alternative_json: str, rationale: str,
}, ['target', 'inputs_json', 'rationale']]

export const reproductionTool = ['Use the original research request to independently recompute the designated condition. Supply exactly one of program or unavailable. Python calls reproduce(identifiers) once with a dictionary for one condition. Each named identifier is a scalar. Return one flat dict with exactly the requested outcome keys and one finite Python number per key. The CPU sandbox provides the science image and identifiers, with no project files or network. If reproduction is infeasible, give the specific reason in unavailable. The submission opens after commitment, and the program cannot be changed after disclosure.', {program: str, unavailable: str}]

export const inferenceTool = ['Test whether the cited quantity determines the claimed outcome. Design two cases satisfying the stated assumptions using the same calculation. Define reproduce(ids) returning {"premise": number, "outcome": number} from the constructed data. The sandbox calls it independently with ids["case"] equal to "A" and "B". Name the quantities in premise and outcome. Locate the observed PDF sentence in passage (a selector or an artifact, page and exact quote). A test of two hypothetical cases is evidence about an implication. Interpret its relevance to the actual study separately.', {
  passage:object, premise:str, outcome:str, assumptions:str, program:str,
}, ['passage','premise','outcome','assumptions','program']]

export function reproductionComparison(receipt) {
  const {artifact, identifiers, rows_pointer, columns, differences, observed, expected} = receipt
  return {artifact, rows_pointer, identifiers, metrics: columns.map(metric => ({
    metric, reported_by_worker: observed[metric], independently_recomputed_by_reviewer: expected[metric],
    agrees: !differences.includes(metric),
  }))}
}

export function reviewForAuthor(review) {
  // DSH compacts long tool results. Keep the signed decision and the work it
  // requires together, with a stable read-only path to the complete record.
  if (Array.isArray(review.issues) && /^[a-f0-9]{64}$/.test(review.revision)) {
    const issues = review.issues.filter(issue => issue.status !== 'resolved').map(issue => ({
      id:issue.id, status:issue.status, severity:issue.severity, domain:issue.domain,
      pages:[...new Set((issue.evidence || []).filter(ref => ref.artifact?.endsWith('/main.pdf') &&
        Number.isSafeInteger(ref.page)).map(ref => ref.page))],
      claim:issue.claim, fix:issue.fix, reason:issue.reason,
    }))
    const result = {verdict:review.verdict, revision:review.revision, deliverable:review.deliverable,
      signed_record:'auto_research/state/reviews/' + review.revision + '/review.json', issues}
    if (review.reviewed_pdf_matches_last_build !== undefined) result.reviewed_pdf_matches_last_build = review.reviewed_pdf_matches_last_build
    if (review.verdict === 'accept' && review.deliverable && review.reviewed_pdf_matches_last_build === undefined) {
      result.delivery_instruction = 'Call deliver on the current revision and use its receipt to report completion.'
    } else if (issues.some(issue => issue.severity === 'blocker')) {
      result.revision_instruction = 'Recheck the complete source paragraph of each open finding, including text on the next PDF page. Trace any repetition in the abstract, results, discussion and conclusion to the same observation. Compile the revision and inspect the surviving passages before requesting independent review.'
    }
    if (review.checks) result.unassessed = Object.entries(review.checks).filter(([,check]) => check.status === 'unassessed')
      .map(([domain,check]) => ({domain,reason:check.reason}))
    const reproductions = review.checks?.experiment?.controls?.filter(receipt => receipt.control === 2)
    if (reproductions?.length) result.reproduction = reproductions.map(reproductionComparison)
    return result
  }
  const experiment = review.checks?.experiment
  if (!experiment?.controls?.some(r => r.control === 2)) return review
  const controls = experiment.controls.map(r => r.control === 2 ? reproductionComparison(r) : r)
  return {...review, checks: {...review.checks, experiment: {...experiment, controls}}}
}

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
