import { Socket } from 'node:net'
import { createInterface } from 'node:readline'

export const name = 'idea2paper-hosted'
export const inject = ['appReady', 'appExit']

export function apply(ctx) {
  ctx.effect(() => ctx.appReady.onReady(() => {
    const channel = new Socket({ fd: Number(process.env.ARK_DSH_WORKFLOW_FD) })
    const lines = createInterface({ input: channel, crlfDelay: Infinity })
    const responses = lines[Symbol.asyncIterator]()
    const send = step => new Promise((resolve, reject) => {
      channel.write(`${step}\n`, error => error ? reject(error) : resolve())
    })
    const call = async step => {
      await send(step)
      const response = await responses.next()
      if (response.done) throw new Error(`Worker disconnected during ${step}`)
      return JSON.parse(response.value)
    }
    void (async () => {
      let exit = 1
      try {
        await send('ready-v2')
        await runWorkflow(call)
        await send('done')
        exit = 0
      } catch (error) {
        console.error(`Idea2Paper workflow: ${error.message}`)
      } finally {
        lines.close()
        channel.destroy()
        ctx.appExit(exit)
      }
    })()
  }))
}

async function runExperiments(call) {
  await call('dev-start')
  for (;;) {
    const steps = await call('dev-iteration')
    if (steps === null) break
    for (const step of steps) {
      if (!await call(`dev-${step}`)) return
    }
    if ((await call('dev-evaluate')) !== false) break
  }
}

async function runWorkflow(call) {
  if (!await call('setup')) return
  if (await call('should-research')) await call('research')
  if (await call('prepare')) {
    await runExperiments(call)
    if (!await call('dev-check')) return
    for (const step of ['writing', 'figures', 'draft', 'deliver']) await call(`dev-${step}`)
    if (!await call('dev-check')) return
  }
  while (await call('paper-start')) {
    for (const step of ['compile', 'review', 'plan', 'execute', 'validate', 'finalize']) {
      const next = await call(`paper-${step}`)
      if (next === 'stop') return
      if (next === 'repeat') break
      if (next !== 'next') throw new Error(`Invalid transition after ${step}`)
    }
  }
}
