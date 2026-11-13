import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import { parseCmdline } from '@deepseek-ai/dsh-cmdline'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { Command } from 'commander'
import { runWorkflow } from './workflow.js'

export const name = 'idea2paper'
export const inject = ['cmdlineArgs', 'agentDefaultModel', 'agents', 'sessions', 'tools']

export function apply(ctx) {
  const cli = new Command().name('dsh --profile idea2paper')
  const launch = (directory, idea) => {
    void (async () => {
      await ctx.get('loader')?.await()
      const abort = new AbortController()
      const stop = () => abort.abort(new Error('Workflow interrupted'))
      const disposeStop = ctx.effect(() => stop)
      try {
        const project = resolve(directory)
        const selection = ctx.agentDefaultModel.currentSelection()
        await runWorkflow({
          directory: project,
          idea,
          selection: { provider: selection.provider, model: selection.model },
          signal: abort.signal,
          runRole: (role, prompt) => runRole(ctx, selection, project, role, prompt, abort.signal),
        })
        ctx.get('appExit')(0)
      } finally {
        disposeStop()
      }
    })().catch(error => {
      console.error(error.message)
      ctx.get('appExit')?.(1)
    })
  }
  cli.command('run <project> <idea>').description('Start a short research project').action(launch)
  cli.command('resume <project>').description('Continue an incomplete project').action(project => launch(project))
  parseCmdline(ctx, cli)
}

async function runRole(ctx, selection, project, role, prompt, signal) {
  signal = AbortSignal.any([signal, AbortSignal.timeout(120_000)])
  signal.throwIfAborted()
  const sessionId = `idea2paper-${role}-${randomUUID()}`
  const handle = await ctx.agents.create({
    sessionId,
    meta: { cwd: project },
    agentOptions: { provider: selection.provider, model: selection.model, maxTokens: 8192 },
    signal,
    setup(scope) {
      installModelSelection(scope, { current: selection, assembled: undefined })
      scope.tools.restrict({ allow: [] })
    },
  })
  const { agent } = handle
  let text = ''
  let reason
  const unsubscribe = ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return
    if (event.type === 'assistant/message') {
      text = event.data.message.content.filter(block => block.type === 'text').map(block => block.text).join('')
    }
    if (event.type === 'turn/end') reason = event.data.reason
  })
  const cancel = () => agent.cancel({ kind: 'user' })
  signal.addEventListener('abort', cancel, { once: true })
  try {
    signal.throwIfAborted()
    agent.followup(createUserMessage({
      source: { kind: 'user' },
      content: [{ type: 'text', text: `Idea2Paper role: ${role}\n${prompt}\nReturn only the requested JSON.` }],
    }))
    await agent.whenIdle()
    await ctx.sessions.flush(agent.session)
    signal.throwIfAborted()
    if (reason?.kind !== 'completed' && reason?.kind !== 'max-tokens') {
      throw new Error(`${role} session ${sessionId} failed: ${reason?.error?.message ?? reason?.kind ?? 'missing turn result'}`)
    }
    const output = text.trim().replace(/^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$/gi, '')
    return { sessionId, output, truncated: reason.kind === 'max-tokens' }
  } finally {
    signal.removeEventListener('abort', cancel)
    unsubscribe()
    await handle.dispose()
  }
}
