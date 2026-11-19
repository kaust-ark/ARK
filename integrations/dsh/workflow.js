import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { copyFile, lstat, mkdir, mkdtemp, open, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { checkExperimentRunner, removeExperiment, runExperiment } from './experiment.js'

const steps = ['plan', 'code', 'experiment', 'writer', 'reviewer', 'revision', 'final-review', 'delivery']
const formatVersion = 2
const python = process.env.IDEA2PAPER_PYTHON || 'python3'

export async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
}

export async function writeJson(path, value) {
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
  return {
    question: text(data?.question, 'research question', 8, 350),
    hypothesis: text(data?.hypothesis, 'hypothesis', 8, 500),
    method: text(data?.method, 'experimental method', 40, 1800),
    metric: plainText(data?.metric, 'metric description', 3, 100),
  }
}

function validateResults(data) {
  const rows = data?.rows
  if (!Array.isArray(rows) || !rows.length || rows.length > 12 ||
      rows.some(row => !Number.isFinite(row?.baseline) || !Number.isFinite(row?.proposed))) {
    throw new Error('Experiment must return 1–12 rows with finite baseline and proposed numbers')
  }
  return { rows: rows.map(row => ({
    label: plainText(row.label, 'trial label', 1, 70),
    baseline: row.baseline,
    proposed: row.proposed,
  })) }
}

function plainText(input, label, min, max) {
  const value = text(typeof input === 'string' ? input
    .replace(/[‘’]/gu, "'")
    .replace(/[“”]/gu, '"')
    .replace(/[‐‑–—−]/gu, '-')
    .replace(/≤/gu, '<=').replace(/≥/gu, '>=').replace(/≈/gu, '~').replace(/×/gu, '*')
    .replace(/\u00a0/gu, ' ') : input, label, min, max)
  if (/[^\x20-\x7e\n\t]/u.test(value)) throw new Error(`Use plain ASCII for ${label}`)
  return value
}

function validateDraft(data) {
  const fields = { title: [8, 160], abstract: [140, 1800], method: [240, 2800],
    interpretation: [240, 2800], limitations: [180, 2200] }
  return Object.fromEntries(Object.entries(fields).map(([field, [min, max]]) => [
    field, plainText(data?.[field], field, min, max),
  ]))
}

function validateReview(data) {
  if (typeof data?.accepted !== 'boolean' || !Array.isArray(data.unsupported_claims) ||
      data.unsupported_claims.length > 8) {
    throw new Error('Review must include a boolean acceptance and an array of unsupported claims')
  }
  const unsupported = data.unsupported_claims.map(claim => text(claim, 'unsupported claim', 6, 300))
  const feedback = text(data.feedback, 'review feedback', 12, 2000)
  return { accepted: data.accepted && unsupported.length === 0, feedback, unsupported }
}

const escapes = {
  '\\': '\\textbackslash{}', '{': '\\{', '}': '\\}', '$': '\\$', '&': '\\&',
  '#': '\\#', '%': '\\%', '_': '\\_', '~': '\\textasciitilde{}', '^': '\\textasciicircum{}',
}

function latex(value) {
  return value.replace(/[\\{}$&#%_~^]/g, char => escapes[char])
}

function render(draft, plan, results) {
  const rowEnd = ' \\\\' + '\n'
  const rows = results.rows.map(({ label, baseline, proposed }) =>
    [latex(label), baseline, proposed].join(' & ') + rowEnd).join('')
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
\\section{Measured results}
Metric: ${latex(plan.metric)}.

\\begin{tabular}{lrr}
${'Trial & Baseline & Proposed' + rowEnd}${rows}\\end{tabular}
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
  const build = await mkdtemp(join(directory, '.paper-build-'))
  try {
    const stagedPaper = join(build, 'paper')
    await mkdir(stagedPaper)
    await mkdir(join(build, 'results'))
    await copyFile(join(directory, 'results', 'experiment.json'), join(build, 'results', 'experiment.json'))
    await writeFile(join(stagedPaper, 'main.tex'), render(state.draft, state.plan, state.results), { mode: 0o600 })
    await execute('pdflatex', ['-no-shell-escape', '-halt-on-error', '-interaction=batchmode', 'main.tex'],
      stagedPaper, AbortSignal.any([signal, AbortSignal.timeout(60_000)]))
    await execute(python, ['-c', `
import sys
from pathlib import Path
from ark.delivery_contract import evaluate, write_report
report = evaluate(Path(sys.argv[1]))
if write_report(Path(sys.argv[2]), report) is None:
    raise SystemExit("Could not write the delivery report")
if report.hard_violations:
    raise SystemExit(", ".join(f.check for f in report.hard_violations))
`, build, directory], directory, AbortSignal.any([signal, AbortSignal.timeout(60_000)]))
    signal.throwIfAborted()
    for (const artifact of ['main.tex', 'main.pdf']) {
      await rename(join(stagedPaper, artifact), join(paper, artifact))
    }
  } finally {
    await rm(build, { recursive: true, force: true })
  }
}

export async function runLegacyWorkflow({ directory, idea, selection, signal, runRole }) {
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
      state = { version: formatVersion, id: randomUUID(), idea, selection, completed: [], sessions: {} }
      await writeJson(path, state)
    }
    if (state.version === 1) {
      throw new Error('This project was created by 0.1.0-alpha.0. Resume it with that bundle version.')
    }
    if (state.version !== formatVersion || !/^[a-f0-9-]{36}$/.test(state.id) ||
        !Array.isArray(state.completed) ||
        state.completed.some((step, i) => steps[i] !== step) ||
        state.selection?.provider !== selection.provider || state.selection?.model !== selection.model) {
      throw new Error('Incompatible checkpoint or model selection')
    }
    if (state.completed.length < steps.length) {
      const deadline = AbortSignal.any([signal, AbortSignal.timeout(30_000)])
      await execute('pdflatex', ['--version'], directory, deadline)
      await execute(python, ['-c', 'import fitz, yaml, ark.delivery_contract'], directory, deadline)
      if (!state.completed.includes('experiment')) await checkExperimentRunner(deadline)
    }

    async function ask(role, prompt, validate) {
      const parse = response => {
        if (response.truncated) throw new Error('Response reached the output limit')
        let data
        try {
          data = JSON.parse(response.output)
        } catch {
          throw new Error('Expected a complete JSON response')
        }
        return validate(data)
      }
      let response = await runRole(role, prompt)
      let validated
      try {
        validated = parse(response)
      } catch (error) {
        response = await runRole(role, `${prompt}\nThe last response failed validation: ${error.message}. Return only a concise, corrected JSON object.`)
        validated = parse(response)
      }
      state.sessions[role] = response.sessionId
      return validated
    }

    async function writeDraft(role, review) {
      return ask(role, `Write an English mini paper about the measured experiment below.
Research plan: ${JSON.stringify(state.plan)}
Executed Python code: ${JSON.stringify(state.code)}
Measured data: ${JSON.stringify(state.results)}
${review ? `Prior draft: ${JSON.stringify(state.draft)}\nReview to address: ${JSON.stringify(review)}` : ''}
The renderer supplies the exact result table. Return a JSON object with title (one short line),
abstract (2–4 sentences), method, interpretation, and limitations (one short paragraph each).
Describe the executed code where it differs from the plan. Check every trial for exceptions before
generalizing. Only claim measured quantities and trends supported by the table. If wall time, memory
usage, or similar metrics were not collected, report them as unmeasured. Use plain ASCII prose.`,
      validateDraft)
    }

    async function review(role) {
      return ask(role, `Independently review whether the draft answers the question and whether
its claims are supported by the measured experiment. Inspect all sections. List each unsupported
claim (at most eight short strings), including plausible but unmeasured quantities and overstated
trends. Reject if that list is non-empty or the baseline is weak.
Plan: ${JSON.stringify(state.plan)}
Experiment code: ${state.code}
Measured data: ${JSON.stringify(state.results)}
Draft: ${JSON.stringify(state.draft)}
Return a JSON object with accepted (boolean), unsupported_claims (array, empty only if none),
and feedback (string).`, validateReview)
    }

    const stages = {
      async plan() {
        state.plan = await ask('plan', `User idea: ${JSON.stringify(state.idea)}
Design a small reproducible CPU experiment on this idea. Use a synthetic input or data generated
by the experiment itself, with a meaningful baseline and proposed comparison. The experiment
has Python standard library only, no network or writable filesystem, 128 MiB of memory, and
60 seconds of runtime. Use at most twelve trial rows. Choose a numerical tolerance that the
implementation can reach at the chosen scales. Return a JSON object with question (8–350 chars),
hypothesis (8–500), method (40–1800), and metric (3–100, short ASCII table label only).
If the idea is too broad, propose a measurable subquestion and make the limitation explicit.`, validatePlan)
      },
      async code() {
        state.code = await ask('code', `Implement this experiment in standard-library Python:
${JSON.stringify(state.plan)}
Return a JSON object with a code string, at most 12000 characters. The program must compute its
results using deterministic inputs and print exactly one JSON object, with a rows array of 1–12
objects. Each row has a short label, a finite baseline number, and a finite proposed number.
Use stdout for the result, with no files, installations, or external datasets.`,
        data => text(data?.code, 'experiment code', 12, 12_000))
      },
      async experiment() {
        const existing = await readJson(resultPath)
        state.results = existing ? validateResults(existing) :
          validateResults(await runExperiment({ id: state.id, code: state.code, signal }))
        if (!existing) {
          await mkdir(join(directory, 'results'), { recursive: true })
          await writeJson(resultPath, state.results)
        }
      },
      async writer() {
        state.draft = await writeDraft('writer')
      },
      async reviewer() {
        state.review = await review('reviewer')
      },
      async revision() {
        if (!state.review.accepted) state.draft = await writeDraft('revision', state.review)
      },
      async 'final-review'() {
        if (!state.review.accepted) state.review = await review('final-review')
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
      if (step === 'experiment') {
        try {
          await removeExperiment(state.id, state.code, signal)
        } catch (error) {
          console.error(`Saved results. Container cleanup failed: ${error.message}`)
        }
      }
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
export async function acquireLock(directory, signal, { detached = false } = {}) {
  signal.throwIfAborted()
  const child = spawn('flock', ['-n', '-E', '73', join(directory, '.workflow.lock'),
    'sh', '-c', 'printf locked; cat >/dev/null'], { stdio: ['pipe', 'pipe', 'ignore'], detached })
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
