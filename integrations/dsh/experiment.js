import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { promisify } from 'node:util'

const run = promisify(execFile)
const image = 'python@sha256:ae52c5bef62a6bdd42cd1e8dffef86b9cd284bde9427da79839de7a4b983e7ca'
const limit = 60

export async function checkExperimentRunner(signal) {
  await docker(['info', '--format', '{{.ServerVersion}}'], signal)
  await docker(['image', 'inspect', image, '--format', '{{.Id}}'], signal)
}

async function docker(args, signal) {
  try {
    return await run('docker', args, { signal, timeout: 75_000, maxBuffer: 131_072 })
  } catch (error) {
    signal.throwIfAborted()
    throw new Error(`Docker ${args[0]} failed: ${error.stderr?.trim().slice(-1200) || error.code || error.message}`)
  }
}

async function lookup(name, signal) {
  const { stdout } = await docker(['ps', '-a', '--filter', `name=^${name}$`, '--format', '{{.ID}}'], signal)
  if (!stdout.trim()) return undefined
  const { stdout: info } = await docker(['inspect', name, '--format', '{{json .}}'], signal)
  return JSON.parse(info)
}

export async function runExperiment({ id, code, signal }) {
  const name = `idea2paper-${id}`
  const digest = createHash('sha256').update(code).digest('hex')
  let container = await lookup(name, signal)
  if (!container) {
    await docker([
      'create', '--name', name, '--label', `idea2paper.script=${digest}`,
      '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
      '--security-opt', 'no-new-privileges', '--user', '65534:65534',
      '--pids-limit', '32', '--memory', '128m', '--memory-swap', '128m', '--cpus', '0.5',
      '--log-driver', 'local', '--log-opt', 'max-size=64k', '--log-opt', 'max-file=1',
      '--log-opt', 'compress=false',
      image, 'timeout', '-k', '3', String(limit), 'python', '-I', '-B', '-c', code,
    ], signal)
    container = await lookup(name, signal)
  }
  if (container?.Config.Labels?.['idea2paper.script'] !== digest) {
    throw new Error('Experiment container identity does not match this project')
  }
  if (container.State.Status === 'created') {
    await docker(['start', name], signal)
  } else {
    console.log(`Recovering experiment container ${name} (${container.State.Status})`)
  }
  const { stdout: status } = await docker(['wait', name], signal)
  const { stdout: output } = await docker(['logs', name], signal)
  if (status.trim() !== '0') {
    throw new Error(`Experiment exited with code ${status.trim()}. Inspect it with docker logs ${name}`)
  }
  return JSON.parse(output.trim())
}

export async function removeExperiment(id, code, signal) {
  const name = `idea2paper-${id}`
  const container = await lookup(name, signal)
  const digest = createHash('sha256').update(code).digest('hex')
  if (container?.State.Status === 'exited' && container.Config.Labels?.['idea2paper.script'] === digest) {
    await docker(['rm', name], signal)
  }
}
