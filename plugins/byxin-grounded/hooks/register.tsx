import type { Hook, Register } from 'claude-code'

import { LESSONS, SECTION } from './lessons'

// The layers of ByxIn, each a real module of the brain driven through brain/bridge.py:
//   retriever   byxin_rerank + byxin_bm25   what the record holds for this question (lexical lanes; dense lane when an engine answers)
//   lessons     byxin_lessons               corrections that bind next time, injected before the facts
//   self-report byxin_history + byxin_affect the brain's state, measured, never a mood
//   comparator  handler + byxin_nli         citations, numbers, claims checked against what was shown
//   history     byxin_history               every answered question is an episode with declared provenance (answering_layer)
//   shared brain bridge.py hub             every session in one project reads what the others did, and who is editing what
// Claude Code's own model is the content model: the bridge splits the answering layer around it.
//
// WHICH BRAIN. A project that is itself a ByxIn tree is served by its own brain (its record, its lessons, its loop
// lock); any other project by the brain vendored here, its record kept per project outside this folder. One brain
// per project: a second one beside a live loop would share its engine and spoil its measurements.

type Api = Parameters<Hook<'session.start'>>[0]
type Reply = { ok: boolean; error?: string; [k: string]: unknown }
type Prepared = {
  ok: boolean; error?: string; answerable: boolean; system: string; sources: string[]; lessons: string[]
  chunks: number; retrieval_note: string | null; corpus: string; anchored: boolean; state: unknown
}
type Checked = { ok: boolean; error?: string; attribution: { verified: boolean; unsupported?: string[]; unsupported_numbers?: string[]; contradicted?: string[] }; note: string }
type Live = { session: string; host: string; branch?: string; at: string; files: string[]; where: string }
type Where = { tree: string; tree_why: string; hub: string; record: string; vendored: boolean; runtime: string; shared: unknown; host: string; branch: string }

const QUESTION = /\?\s*$|^\s*(what|why|how|where|when|who|which|is|are|does|do|did|can|could|explain|describe|tell me)\b/i
// NO ONE ASKED. A prompt a background task, a schedule, another session or a plugin sent is not a person asking
// (lesson a-status-line-is-not-someone-asking): recorded as asks, a long-running job's notifications would fill the
// block every other session reads. The engine says where a prompt came from; these origins are not a person, and a
// prompt in one of these envelopes is not either. brain/bridge.py keeps the same envelopes for older records.
const NOT_A_PERSON = new Set(['task-notification', 'scheduled-trigger', 'peer', 'peer-send-message', 'projects-relay',
  'coordinator', 'observer', 'observer-activity', 'plugin'])
const ENVELOPES = ['task-notification', 'system-reminder', 'ci-monitor-event', 'local-command-caveat', 'local-command-stdout', 'command-name', 'bash-input', 'bash-stdout', 'bash-stderr', 'agent-message']
const ENVELOPE = new RegExp(`^\\s*<(${ENVELOPES.join('|')})[\\s>]`)
const EDIT_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit'])
// python3 first (Linux, macOS, a cloud box); python and the launcher where Windows has no python3
const PYTHONS: string[][] = [['python3'], ['python'], ['py', '-3']]
const BEAT_MS = 60000

const TOOLS: Record<string, { script: string; why: string }> = {
  world: { script: 'byxin_world.py', why: 'the loaded world pack' },
  history: { script: 'byxin_history.py', why: 'are we writing history (episodes with declared provenance)' },
  lessons: { script: 'byxin_lessons.py', why: 'the school: --list, --render, --add-file F' },
  bm25: { script: 'byxin_bm25.py', why: 'lexical recall: "<question>" | --stats' },
  surprise: { script: 'byxin_surprise.py', why: 'the surprise gate: --replay' },
}

const words = (s: string): string[] => s.trim().split(/\s+/).filter(Boolean)
// THE SIGIL: the person sees when ByxIn is active. It marks a turn a layer acted on -- facts or a refusal injected, the comparator's verdict, news from the other sessions -- and never
// a prompt passed through untouched, so the sigil claims no more than happened.
const SIGIL = ')|('
const SEND_DESC = 'Send a message to another Claude Code session working in this project, or to all of them, through ' +
  "ByxIn's shared brain. An idle recipient receives it at once as a new turn; a busy one when it finishes. Address a " +
  'session by the 8-character id the shared-brain block shows, or "all". Use it to hand over work, warn about a file ' +
  'you are both changing, or answer a message you received.'
const HELP_HEAD = 'ByxIn grounded: answers grounded in this project\'s own files and checked against what was shown, and one shared brain for every session here.'
// FIRST SESSION. A project where no session has worked yet hears once what ByxIn does and how to turn it off.
const WELCOME = `${SIGIL} ByxIn grounded is on in this project: it grounds answers in the project's files and shares what each session does with the others. /byxin help · /byxin off`
const sigil = (active: boolean, text: string): string => `${active ? SIGIL + ' ' : ''}ByxIn: ${text}`
const tail = (s: string, n: number): string => (s.length > n ? '…' + s.slice(s.length - n) : s)

let cwd = ''
let root = ''
let project = ''
let sid = ''
let PY: string[] | null = null
let where: Where | null = null
let mode: 'questions' | 'always' | 'off' = 'questions'
let pending: { question: string; state: unknown; lessons: string[]; chunks: number } | null = null
let reads: string[] = []
let seen: string[] = []
let asked = 0
let unverified = 0
// the shared brain
let turnAsk = ''
let turnTrigger = ''   // where this turn's prompt came from (the engine's origin, or the envelope it came in)
let turnByPerson = true
let turnEdits: string[] = []
const sessionEdits = new Set<string>()
let live: Live[] = []
let resumeText = ''
let resumeShown = false
let news = ''
// heardAt: how far this session has been SHOWN the others' events; newsAt: how far the waiting news reaches. A beat
// asks for everything since heardAt, so news waits whole until a prompt shows it; nothing is lost between beats.
let heardAt = ''
let newsAt = ''
// MAIL. mailAt: how far this session's mail has been delivered; sendTool: the model's send tool, once registered.
let mailAt = ''
let sendTool = ''
let sendToolError = ''
let lastBeat = 0
const alerts: string[] = []
const warned = new Set<string>()
let ready: Promise<void> | null = null

// A path inside the project, relative to the session's tree or the project's main tree; null for anything outside
// (a scratchpad, a memory file, another repository): those are none of this project's business, and naming them would
// tell every other session about files it cannot see.
function rel(p: string): string | null {
  const n = p.replace(/\\/g, '/')
  for (const base of [root, project]) {
    const b = base.replace(/\\/g, '/').replace(/\/$/, '')
    if (b !== '' && n.toLowerCase().startsWith(b.toLowerCase() + '/')) return n.slice(b.length + 1)
  }
  return /^([A-Za-z]:|\/)/.test(n) || n === '..' || n.startsWith('../') ? null : n
}

async function run($: Api, op: string, args: Record<string, unknown> = {}, timeoutMs = 60000): Promise<Reply> {
  if (PY === null) return { ok: false, error: 'no Python 3 on this machine (tried python3, python, py -3)' }
  try {
    const ran = await $.process.run([...PY, 'bridge.py'], {
      cwd: `${$.plugin.root}/brain`,
      stdin: JSON.stringify({ ...args, op, root: cwd, project, session: sid }),
      timeoutMs,
      env: { PYTHONIOENCODING: 'utf-8' },
    })
    try {
      return JSON.parse(ran.stdout) as Reply
    } catch {
      // loud, not empty: a bridge that failed is not a record that held nothing
      return { ok: false, error: `bridge exit ${ran.exitCode}: ${tail(ran.stderr, 400)}` }
    }
  } catch (err) {
    return { ok: false, error: String(err).slice(0, 300) }
  }
}

async function findPython($: Api): Promise<string[] | null> {
  for (const cand of PYTHONS) {
    try {
      // a fixed argument, no inline code: Python 3 prints its version on stdout, Python 2 on stderr
      const r = await $.process.run([...cand, '--version'], { timeoutMs: 20000 })
      if (r.exitCode === 0 && /^Python 3\./.test((r.stdout + r.stderr).trim())) return cand
    } catch {
      // not on this machine: try the next name
    }
  }
  return null
}

async function heartbeat($: Api): Promise<void> {
  lastBeat = Date.now()
  const r = await run($, 'beat', { files: [...sessionEdits], since: heardAt, mail_since: mailAt }, 90000)
  if (!r.ok) return
  live = (r.live as Live[] | undefined) ?? []
  if (typeof r.news === 'string') news = r.news
  if (typeof r.at === 'string') newsAt = r.at
  // THE ACTIVE TRIGGER: each new mail becomes a turn of this session -- at once when it is idle, after the turn it is
  // in otherwise (a plugin's prompt waits for idle). It arrives as the plugin's message, never as the person's words.
  const mail = (r.mail as { from: string; host: string; text: string; told: boolean }[] | undefined) ?? []
  if (mail.length && typeof r.at === 'string') mailAt = r.at
  for (const m of mail) {
    const from = String(m.from ?? '?').slice(0, 8)
    $.ui.toast(`${SIGIL} ByxIn: mail from session ${from}`)
    // /byxin off: the mail is kept and shown, but it starts no turn in this session
    if (mode === 'off') continue
    void $.prompt.submit({ text: `Mail through ByxIn from session ${from} on ${m.host} (${m.told ? 'a person wrote it' : 'its model wrote it'}):\n\n${m.text}\n\nTo answer, use the ByxIn send tool with to: "${from}".` })
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    cwd = e.cwd
    await $.command.register({ name: 'byxin', description: 'ByxIn: ask | on | off | always | ledger | brain | note | notes | events | retract | sessions | share | where | send | mail | name | ' + Object.keys(TOOLS).join(' | ') })
    $.ui.status('ByxIn: starting')
    // the model's send tool is registered before the first turn: a tool registered later is listed only from the next
    try {
      sendTool = (await $.tool.register({
        name: 'send', description: SEND_DESC,
        inputSchema: { type: 'object', required: ['to', 'text'], properties: {
          to: { type: 'string', description: 'the recipient session id (8 characters) or "all"' },
          text: { type: 'string', description: 'the message' } } },
      })).tool
    } catch (err) { sendTool = ''; sendToolError = String(err) }
    // The start runs beside the session; the first prompt waits for it, so the shared brain is in front of the model
    // before it answers (a headless run submits its prompt at once).
    ready = (async () => {
      sid = await $.session.id()
      root = await $.session.root()
      project = (await $.session.repo())?.root ?? root
      PY = await findPython($)
      if (PY === null) {
        $.ui.status('ByxIn: no Python 3 found (tried python3, python, py -3)')
        return
      }
      const w = await run($, 'where')
      if (!w.ok) {
        $.ui.status('ByxIn: ' + String(w.error).slice(0, 80))
        return
      }
      where = w as unknown as Where
      const st = await run($, 'start', {}, 120000)
      if (st.ok) {
        resumeText = String(st.text ?? '')
        live = (st.live as Live[] | undefined) ?? []
        heardAt = String(st.at ?? '')
        mailAt = heardAt
        if (st.first === true) $.ui.toast(WELCOME)
      }
      $.ui.status(`ByxIn: ready (${mode}) · ${where.vendored ? 'vendored brain' : "the project's own brain"}${live.length ? ` · ${live.length} other session(s) working here` : ''}`)
      $.clock.every(BEAT_MS, () => { void heartbeat($) })
    })()
    return next(e)
  })

  // The identity and the reading lessons ride in the system prompt, as the school's lessons ride before the facts.
  on('prompt.compose', async ($, e, next) => {
    const r = await next(e)
    return { sections: [...r.sections, { id: `${$.plugin.name}:grounding`, text: SECTION, scope: 'session' as const }] }
  })

  // THE SHARED BRAIN, then RETRIEVE + LESSONS + SELF-REPORT, before the model speaks: beside the prompt, unseen by the person.
  on('prompt.submit', async ($, e, next) => {
    reads = []
    seen = []
    pending = null
    const envelope = ENVELOPE.exec(e.text)
    const origin = e.origin?.kind ?? 'unclassified'
    turnTrigger = envelope !== null ? envelope[1] : origin
    turnByPerson = envelope === null && !NOT_A_PERSON.has(origin)
    turnAsk = !turnByPerson || e.text.startsWith('/') ? '' : e.text
    turnEdits = []
    if (mode === 'off') return next(e)
    if (ready !== null) await ready

    const extra: string[] = []
    if (!resumeShown && resumeText !== '') extra.push(resumeText)
    else if (news !== '') {
      extra.push(news)
      if (newsAt !== '') heardAt = newsAt
    }
    resumeShown = true
    news = ''
    extra.push(...alerts.splice(0))
    const ev = extra.length ? { ...e, context: [...(e.context ?? []), ...extra] } : e

    // a notification is not a question to ground: no retrieval, no engine call, no comparator for it
    const isOn = turnByPerson && (mode === 'always' || (QUESTION.test(e.text) && !e.text.startsWith('/')))
    if (!isOn) {
      if (extra.length) $.ui.status(sigil(true, 'shared brain: news from the other sessions'))
      return next(ev)
    }
    if (PY === null) {
      return next({ ...ev, context: [...(ev.context ?? []), 'ByxIn could not consult the record: no Python 3 on this machine. Say so if the answer depends on the record.'] })
    }
    const r = await run($, 'prepare', { question: e.text }, 120000)
    if (!r.ok) {
      $.ui.status('ByxIn: ' + String(r.error ?? 'failed').slice(0, 60))
      return next({ ...ev, context: [...(ev.context ?? []), 'ByxIn could not consult the record (' + String(r.error).slice(0, 300) + '). Say you could not consult the record; do not answer as if you had.'] })
    }
    const prep = r as unknown as Prepared
    asked += 1
    pending = { question: e.text, state: prep.state, lessons: prep.lessons, chunks: prep.chunks }
    // Conversation is not the record's business: with nothing retrieved and nothing in the question that can be looked
    // up by its letters (an identifier, a path), the prompt goes through untouched and nothing is checked afterward.
    if (!prep.answerable && !prep.anchored) {
      pending = null
      $.ui.status(sigil(extra.length > 0, extra.length ? 'shared brain: news; nothing in the record for this question'
        : 'nothing in the record for this; passed through'))
      return next(ev)
    }
    const lexical = String(prep.retrieval_note ?? '').startsWith('dense lane unavailable') ? ' (lexical lanes)' : ''
    $.ui.status(sigil(true, prep.answerable ? `grounding · ${prep.chunks} passages · ${prep.lessons.length} lessons${lexical}`
      : 'grounding · the record lacks what this names'))
    const block = prep.answerable
      ? prep.system
      : 'ByxIn retrieved nothing from the record for this question, which names something that should be there. Answer exactly that you cannot see it in the record and stop; do not fill the gap from general knowledge, and do not invent a file, number or citation. Reading files yourself is still allowed: what you read becomes evidence.'
    return next({ ...ev, context: [...(ev.context ?? []), block] })
  })

  // THE EVIDENCE and THE EDITS: what the session reads is shown to the comparator; what it edits is told to the others.
  on('tool.call', async ($, e, next) => {
    if (sendTool !== '' && e.tool === sendTool) {
      const m = e as unknown as { to?: string; text?: string }
      const r = await run($, 'send', { to: m.to ?? '', text: m.text ?? '', by: 'session' }, 60000)
      return { result: r.ok ? `sent to ${String(r.to)} through ByxIn's shared brain` : `not sent: ${String(r.error)}` }
    }
    const ran = await next(e)
    const denied = 'deny' in ran && ran.deny !== undefined
    // An edit counts only when it happened: a write held for permission comes back as an errored result, not a deny,
    // and must not be recorded as a file written.
    const failed = denied || (ran as { isError?: boolean }).isError === true
    if (!failed && EDIT_TOOLS.has(e.tool)) {
      const input = e as { file_path?: string; notebook_path?: string }
      const p = input.file_path ?? input.notebook_path
      const r = p ? rel(p) : null
      if (r !== null) {
        turnEdits.push(r)
        sessionEdits.add(r)
        const other = live.find(s => (s.files ?? []).some(f => f.toLowerCase() === r.toLowerCase()))
        if (other !== undefined && !warned.has(r)) {
          warned.add(r)
          const msg = `ByxIn: session ${other.session.slice(0, 8)} on ${other.host} (${other.where}) is also editing ${r}.`
          $.ui.toast(msg)
          alerts.push(msg + ' Coordinate before changing it further: read the file again first.')
        }
        if (Date.now() - lastBeat > 15000) void heartbeat($)
      }
    }
    if (pending !== null && !denied) {
      if (e.tool === 'Read') reads.push((e as { file_path: string }).file_path)
      const text = (ran as { text?: unknown }).text
      if (typeof text === 'string' && seen.join('').length < 400000) seen.push(text)
    }
    return ran
  })

  // THE COMPARATOR, before speech, and THE TURN, into the shared brain and the record.
  on('turn.complete', async ($, e, next) => {
    const out = await next(e)
    if (e.reason !== 'answer' || e.agentId !== undefined) return out
    let outcome: string | null = null
    let note = ''
    let participation: unknown = null
    if (pending !== null && e.answer !== '') {
      const turn = pending
      const r = await run($, 'check', { answer: e.answer, question: turn.question, state: turn.state, reads, seen })
      if (!r.ok) {
        $.ui.status('ByxIn: comparator ' + String(r.error ?? 'failed').slice(0, 50))
      } else {
        const chk = r as unknown as Checked
        const a = chk.attribution
        if (!a.verified) unverified += 1
        outcome = a.verified ? 'verified' : 'unverified'
        participation = { chunks: turn.chunks, lessons: turn.lessons, comparator: a }
        note = a.verified ? '' : chk.note
        $.ui.status(sigil(true, `${a.verified ? 'verified' : 'UNVERIFIED'} · ${turn.chunks} passages · ${turn.lessons.length} lessons · ${unverified}/${asked} flagged`))
      }
    }
    pending = null
    // a turn no one asked enters the record only for what it edited, and says what started it
    const keep = turnByPerson ? turnAsk !== '' : turnEdits.length > 0
    if (keep && PY !== null && where !== null) {
      // local first and awaited, so a session that exits right after its turn still leaves the turn behind; the
      // network sync runs beside it, and any later sync of any session carries the event if this one is cut short
      await run($, 'turn', { ask: turnAsk, answer: e.answer.slice(0, 600), files: [...turnEdits], outcome, participation, trigger: turnTrigger, sync: false }, 60000)
      if (where.shared) void run($, 'share', { act: 'sync' }, 120000)
    }
    turnAsk = ''
    turnTrigger = ''
    turnByPerson = true
    turnEdits = []
    return note === '' ? out : { ...out, text: out.text + '\n\n' + note }
  })

  on('session.end', async ($, e, next) => {
    if (PY !== null && where !== null) await run($, 'end', { files: [...sessionEdits], asked }, 8000)
    return next(e)
  })

  on('command.run', { command: 'byxin' }, async ($, e) => {
    const [sub = 'help', ...rest] = words(e.args)

    if (sub === 'on' || sub === 'off' || sub === 'always') {
      mode = sub === 'on' ? 'questions' : sub === 'off' ? 'off' : 'always'
      $.ui.status('ByxIn: ' + mode)
      return { text: `ByxIn layers: ${mode === 'questions' ? 'on for questions' : mode === 'always' ? 'on for every prompt' : 'off'}.` }
    }
    if (sub === 'where') {
      if (rest[0] === 'tool') return { text: `send tool: ${sendTool || '(none)'} ${sendToolError}` }
      return { text: where === null ? 'ByxIn has not started (no Python 3, or the bridge failed).' : [
        `brain   ${where.tree}  (${where.tree_why})`, `record  ${where.record}`, `shared  ${where.hub}`,
        `beyond this machine: ${where.shared ? JSON.stringify(where.shared) : 'no (/byxin share on to share through the repository)'}`,
        `python  ${PY?.join(' ')}`, `session ${sid} on ${where.host}, branch ${where.branch}`].join('\n') }
    }
    if (sub === 'brain' || sub === 'resume') {
      const r = await run($, 'resume', {}, 120000)
      if (r.ok) live = (r.live as Live[] | undefined) ?? live
      return { text: r.ok ? String(r.text) : 'ByxIn: ' + r.error }
    }
    if (sub === 'note') {
      const r = await run($, 'note', { text: rest.join(' ') }, 120000)
      return { text: r.ok ? `Noted for every session in this project (told). Stored in ${String(r.stored)}.` : 'ByxIn: ' + r.error }
    }
    if (sub === 'notes') {
      const r = await run($, 'notes')
      const list = (r.notes as { text: string; at: string; session: string; host: string }[] | undefined) ?? []
      return { text: r.ok ? (list.length ? list.map(n => `${n.at}  ${n.text}  (session ${n.session.slice(0, 8)} on ${n.host})`).join('\n') : 'No notes yet. /byxin note <text> leaves one for every session.') : 'ByxIn: ' + r.error }
    }
    if (sub === 'sessions') {
      await heartbeat($)
      return { text: live.length ? live.map(s => `${s.session.slice(0, 8)} on ${s.host} (${s.where}), branch ${s.branch ?? '?'}, seen ${s.at}${s.files.length ? ', editing ' + s.files.slice(0, 10).join(', ') : ''}`).join('\n') : 'No other session is working in this project right now.' }
    }
    if (sub === 'share') {
      const [act = 'status', remote, branch] = rest
      const r = await run($, 'share', { act, remote, branch }, 120000)
      if (r.ok && act === 'on') where = where === null ? null : { ...where, shared: { remote: remote ?? 'origin', branch: branch ?? 'byxin-brain' } }
      return { text: r.ok ? String(r.text) : 'ByxIn: ' + r.error }
    }
    if (sub === 'retract') {
      const [event = '', ...why] = rest
      const r = await run($, 'retract', { event, why: why.join(' ') }, 120000)
      return { text: r.ok ? `Retracted ${String(r.retracts)}: the record stays, marked wrong, with your reason, for every session.` : 'ByxIn: ' + r.error }
    }
    if (sub === 'events') {
      const r = await run($, 'events', { n: Number(rest[0] ?? 30) || 30 })
      const list = (r.events as { id: string; kind: string; files?: string[]; outcome?: string }[] | undefined) ?? []
      return { text: r.ok ? list.map(v => `${v.id}  ${v.kind}${v.outcome ? ' ' + v.outcome : ''}${v.files?.length ? ' edited ' + v.files.join(', ') : ''}`).join('\n') || 'The shared brain is empty.' : 'ByxIn: ' + r.error }
    }
    if (sub === 'ledger') {
      return { text: `ByxIn this session: ${asked} questions through the layers, ${unverified} flagged by the comparator, ${sessionEdits.size} files edited.\nLessons in force (system prompt): ${LESSONS.map(l => l.slug).join(', ')}` }
    }
    if (sub === 'ask') {
      const r = await run($, 'prepare', { question: rest.join(' ') }, 120000)
      if (!r.ok) return { text: 'ByxIn: ' + r.error }
      const p = r as unknown as Prepared
      return { text: p.answerable ? `ByxIn would show the model ${p.chunks} chunks from ${p.sources.join(', ') || '(none)'}; lessons fired: ${p.lessons.join(', ') || 'none'}.${p.retrieval_note ? '\n(' + p.retrieval_note + ')' : ''}` : `ByxIn: the record holds nothing for that. ${p.retrieval_note ?? ''}` }
    }
    if (sub === 'send') {
      const [to = '', ...words_] = rest
      const r = await run($, 'send', { to, text: words_.join(' '), by: 'person' }, 60000)
      return { text: r.ok ? `${SIGIL} ByxIn: sent to ${to}.` : 'ByxIn: ' + String(r.error) }
    }
    if (sub === 'name') {
      const r = await run($, 'name', { name: rest.join(' ') }, 60000)
      return { text: r.ok ? `${SIGIL} ByxIn: this session is now "${String(r.name)}"; mail sent to that name reaches it.` : 'ByxIn: ' + String(r.error) }
    }
    if (sub === 'mail') {
      const r = await run($, 'mail', {}, 60000)
      if (!r.ok) return { text: 'ByxIn: ' + String(r.error) }
      const list = (r.mail as { from: string; host: string; text: string; at: string }[]) ?? []
      return { text: list.length ? list.map(m => `${m.at}  from ${String(m.from).slice(0, 8)} on ${m.host}: ${m.text}`).join('\n')
        : 'No mail for this session.' }
    }
    const tool = TOOLS[sub]
    if (tool === undefined) {
      return { text: [HELP_HEAD,
        '  /byxin ask <question>     what ByxIn would give the model for this question, without asking it',
        '  /byxin on | off | always  ground questions (the default) | do nothing | ground every prompt',
        '  /byxin brain              what the other sessions of this project did and left',
        '  /byxin sessions           who is working here right now, and what they are editing',
        '  /byxin note <text>        leave a note every session here will read; /byxin notes lists them',
        '  /byxin send <session|all> <text>   mail another session here; it wakes the session if it is idle',
        '  /byxin mail               the mail sent to this session',
        '  /byxin name <name>        give this session a name other sessions can mail it by',
        '  /byxin events [n]         the record of sessions; /byxin retract <event-id> <why> marks one wrong',
        '  /byxin share on|off|status|sync   share the brain with other machines through the repository',
        '  /byxin where              which brain, record and hub serve this project',
        '  /byxin ledger             this session: questions grounded, answers flagged, files edited',
        ...Object.entries(TOOLS).map(([k, t]) => `  /byxin ${k.padEnd(18)}${t.why}`)].join('\n') }
    }
    if (PY === null) return { text: 'ByxIn: no Python 3 on this machine (tried python3, python, py -3).' }
    let args = rest
    if (sub === 'consolidate') {
      // consolidation writes into the knowledge index: it runs dry unless the person says --commit
      args = rest.includes('--commit') ? rest.filter(a => a !== '--commit') : ['--dry-run', ...rest]
    }
    const dir = where?.tree ?? `${$.plugin.root}/brain`
    try {
      const ran = await $.process.run([...PY, `tools/${tool.script}`, ...args], { cwd: dir, env: { PYTHONIOENCODING: 'utf-8', BYXIN_CORPUS_ROOT: cwd }, timeoutMs: 600000 })
      const body = (ran.stdout + (ran.stderr ? '\n[stderr]\n' + ran.stderr : '')).trim()
      return { text: `byxin ${sub} ${args.join(' ')} -> exit ${ran.exitCode}\n${tail(body, 6000) || '(no output)'}` }
    } catch (err) {
      return { text: `byxin ${sub}: ${String(err).slice(0, 300)}` }
    }
  })
}
