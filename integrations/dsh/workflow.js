import { spawn } from 'node:child_process'
import { lstat, mkdir, mkdtemp, open, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const steps = ['plan', 'experiment', 'writer', 'reviewer', 'delivery']
const question = 'Compare the number of key comparisons in linear and binary search over sorted arrays.'
const python = process.env.IDEA2PAPER_PYTHON || 'python3'

async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
}

async function writeJson(path, value) {
  const temp = path + '.tmp'
  const handle = await open(temp, 'w', 0o600)
  try {
    await handle.writeFile(JSON.stringify(value, null, 2) + '\n')
    await handle.sync()
  } finally {
    await handle.close()
  }
  await rename(temp, path)
}

function execute(program, args, cwd, signal) {
  return new Promise((done, fail) => {
    const child = spawn(program, args, { cwd, signal, stdio: ['ignore', 'ignore', 'pipe'] })
    let errors = ''
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-2000) })
    child.on('error', fail)
    child.on('close', (code, sig) => {
      if (code === 0) done()
      else fail(new Error(`${program} failed (${code ?? sig}): ${errors.trim()}`))
    })
  })
}

function text(value, label, min, max) {
  if (typeof value !== 'string' || value.trim().length < min || value.length > max) {
    throw new Error(`Invalid ${label}`)
  }
  return value.trim()
}

function validatePlan(data) {
  const sizes = data?.sizes
  if (!Array.isArray(sizes) || sizes.length < 2 || sizes.length > 4 ||
      sizes.some(n => !Number.isSafeInteger(n) || n < 16 || n > 8192) ||
      new Set(sizes).size !== sizes.length) {
    throw new Error('Plan must give two to four distinct input sizes between 16 and 8192')
  }
  return { hypothesis: text(data.hypothesis, 'hypothesis', 8, 500), sizes }
}

function measure(sizes) {
  return sizes.map(size => {
    const values = Array.from({ length: size }, (_, i) => i * 2)
    const targets = [values[0], values[Math.floor(size / 2)], values.at(-1), -1, values.at(-1) + 1]
    const trials = targets.map(target => {
      let linear = 0
      for (const value of values) {
        linear++
        if (value === target) break
      }
      let binary = 0
      let lo = 0
      let hi = values.length - 1
      while (lo <= hi) {
        const mid = Math.floor((lo + hi) / 2)
        binary++
        if (values[mid] === target) break
        if (values[mid] < target) lo = mid + 1
        else hi = mid - 1
      }
      return { target, linear, binary }
    })
    return { size, trials }
  })
}

function validateDraft(data) {
  const fields = { title: [8, 130], abstract: [140, 650], method: [420, 1600],
    interpretation: [420, 1600], limitations: [360, 1600] }
  return Object.fromEntries(Object.entries(fields).map(([field, [min, max]]) => {
    const value = text(data?.[field], field, min, max)
    if (/[^\x20-\x7e\n\t]/u.test(value)) throw new Error(`Use plain ASCII for ${field}`)
    return [field, value]
  }))
}

function validateReview(data) {
  if (typeof data?.accepted !== 'boolean') throw new Error('Review must explicitly accept or reject the draft')
  return { accepted: data.accepted, feedback: text(data.feedback, 'review feedback', 12, 2000) }
}

const escapes = {
  '\\': '\\textbackslash{}', '{': '\\{', '}': '\\}', '$': '\\$', '&': '\\&',
  '#': '\\#', '%': '\\%', '_': '\\_', '~': '\\textasciitilde{}', '^': '\\textasciicircum{}',
}

function latex(value) {
  return value.replace(/[\\{}$&#%_~^]/g, char => escapes[char])
}

function render(draft, results) {
  const rows = results.flatMap(({ size, trials }) =>
    trials.map(({ target, linear, binary }) => `${size} & ${target} & ${linear} & ${binary} \\\\\n`)).join('')
  return `\\documentclass{article}
\\usepackage[T1]{fontenc}
\\title{${latex(draft.title)}}
\\author{}
\\date{}
\\begin{document}
\\maketitle
\\begin{abstract}${latex(draft.abstract)}\\end{abstract}
\\section{Method}
${latex(draft.method)}
\\section{Measured comparisons}
\\begin{tabular}{rrrr}
Input size & Target & Linear & Binary \\\\
${rows}\\end{tabular}
\\section{Interpretation}
${latex(draft.interpretation)}
\\section{Limitations}
${latex(draft.limitations)}
\\section*{Disclosure}
This draft was generated with Idea2Paper (idea2paper.org).
\\end{document}
`
}

async function deliver(directory, state, signal) {
  const paper = join(directory, 'paper')
  await mkdir(paper, { recursive: true })
  const build = await mkdtemp(join(paper, '.build-'))
  try {
    await writeFile(join(build, 'main.tex'), render(state.draft, state.results), { mode: 0o600 })
    await execute('pdflatex', ['-no-shell-escape', '-halt-on-error', '-interaction=batchmode', 'main.tex'], build, signal)
    signal.throwIfAborted()
    for (const artifact of ['main.tex', 'main.pdf']) {
      await rename(join(build, artifact), join(paper, artifact))
    }
  } finally {
    await rm(build, { recursive: true, force: true })
  }
  await execute(python, ['-c', `
import sys
from pathlib import Path
from ark.delivery_contract import evaluate, write_report
root = Path(sys.argv[1])
report = evaluate(root)
if write_report(root, report) is None:
    raise SystemExit("Could not write the delivery report")
if report.hard_violations:
    raise SystemExit(", ".join(f.check for f in report.hard_violations))
`, directory], directory, signal)
}

export async function runWorkflow({ directory, idea, selection, signal, runRole }) {
  if (idea === undefined) {
    if (!(await lstat(directory)).isDirectory()) throw new Error('Resume requires an existing project directory')
  } else {
    idea = text(idea, 'idea', 4, 2000)
    try {
      await mkdir(directory, { mode: 0o700 })
    } catch (error) {
      if (error.code === 'EEXIST') throw new Error('Project directory already exists; use resume for an Idea2Paper project')
      throw error
    }
  }
  const path = join(directory, 'workflow.json')
  const resultPath = join(directory, 'results', 'experiment.json')
  const guard = await acquireLock(directory, signal)
  try {
    let state = await readJson(path)
    if (idea !== undefined && state) throw new Error('Project already exists; use resume')
    if (idea === undefined && !state) throw new Error('Project checkpoint does not exist')
    if (!state) {
      state = { version: 1, idea, selection, completed: [], sessions: {} }
      await writeJson(path, state)
    }
    if (state.version !== 1 ||
        !Array.isArray(state.completed) ||
        state.completed.some((step, i) => steps[i] !== step) ||
        state.selection?.provider !== selection.provider || state.selection?.model !== selection.model) {
      throw new Error('Incompatible checkpoint or model selection')
    }
    if (state.completed.length < steps.length) {
      await execute('pdflatex', ['--version'], directory, signal)
      await execute(python, ['-c', 'import fitz, yaml, ark.delivery_contract'], directory, signal)
    }
    const stages = {
      async plan() {
        const { data, sessionId } = await runRole('plan',
          `Study: ${question}\nUser framing: ${state.idea}\nGive a JSON object with a hypothesis string and sizes array. Use two to four distinct integer sizes between 16 and 8192.`)
        state.plan = validatePlan(data)
        state.sessions.plan = sessionId
      },
      async experiment() {
        const existing = await readJson(resultPath)
        if (existing && JSON.stringify(existing.sizes) !== JSON.stringify(state.plan.sizes)) {
          throw new Error('Existing experiment result has different inputs')
        }
        if (!existing) {
          await mkdir(join(directory, 'results'), { recursive: true })
          await writeJson(resultPath, { sizes: state.plan.sizes, results: measure(state.plan.sizes) })
        }
        const evidence = await readJson(resultPath)
        state.results = evidence.results
      },
      async writer() {
        const { data, sessionId } = await runRole('writer',
          `Write an English mini paper for this study: ${question}
User framing: ${state.idea}
Plan: ${JSON.stringify(state.plan)}
Measured data: ${JSON.stringify(state.results)}
Each trial records one count per inspected element. The renderer supplies the exact result table.
Return fields title (8–130 chars), abstract (140–650), method (420–1600), interpretation (420–1600), limitations (360–1600).
Use plain prose and ASCII. Do not invent citations, performance claims, measurements, or numbers.`)
        state.draft = validateDraft(data)
        state.sessions.writer = sessionId
      },
      async reviewer() {
        const { data, sessionId } = await runRole('reviewer',
          `Review whether this draft is supported by these measured comparison counts.
Study: ${question}
Results: ${JSON.stringify(state.results)}
Draft: ${JSON.stringify(state.draft)}
Return accepted (boolean) and feedback (string). Reject unsupported claims.`)
        state.review = validateReview(data)
        state.sessions.reviewer = sessionId
      },
      async delivery() {
        if (!state.review.accepted) throw new Error(`Independent review rejected the draft: ${state.review.feedback}`)
        await deliver(directory, state, signal)
      },
    }
    for (const step of steps.slice(state.completed.length)) {
      signal.throwIfAborted()
      await stages[step]()
      signal.throwIfAborted()
      state.completed.push(step)
      await writeJson(path, state)
      console.log(`Completed ${step}`)
    }
    const pdf = join(directory, 'paper', 'main.pdf')
    if (!(await lstat(pdf)).isFile()) throw new Error('Expected a PDF at the recorded delivery path')
    console.log(pdf)
  } finally {
    await guard()
  }
}

// The OS releases this advisory lock even after SIGKILL. The helper exits on
// stdin EOF when its owning Node process dies.
async function acquireLock(directory, signal) {
  signal.throwIfAborted()
  const child = spawn('flock', ['-n', '-E', '73', join(directory, '.workflow.lock'),
    'sh', '-c', 'printf locked; cat >/dev/null'], { stdio: ['pipe', 'pipe', 'ignore'] })
  child.stdin.on('error', () => {})
  const closed = new Promise(resolve => child.on('close', resolve))
  let output = ''
  try {
    await new Promise((done, fail) => {
      child.on('error', fail)
      child.once('close', code => fail(new Error(code === 73 ? 'Project is already running' : 'Could not acquire project lock')))
      child.stdout.on('data', chunk => {
        output += chunk
        if (output === 'locked') done()
      })
    })
  } catch (error) {
    child.stdin.end()
    throw error
  }
  return async () => {
    child.stdin.end()
    await closed
  }
}
