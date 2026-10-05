# byxin-grounded

ByxIn's brain inside Claude Code. Version 0.9.0.

Claude Code answers from what it can see. byxin-grounded puts a small, honest brain beside it, for anyone, in any
repository:

- **Grounded answers.** For a question about your project it retrieves what the repository holds (identifier recall
  and BM25, held to a coverage floor so a loosely related passage is not passed off as an answer), applies the
  lessons that bind on it, and after the answer checks every citation, number and claim against what was actually
  shown or read. A miss is annotated, never rewritten. When the record does not hold the question, ByxIn stays out.
- **An honest record.** Every answered question is an episode with declared provenance: perceived, told, recalled,
  imagined or inferred. A notification, a schedule or another session's message is never recorded as someone asking.
- **One shared brain per project.** Every Claude Code session working in the same repository, local or on the web,
  hears what the others did: what was asked, which files were edited, what was left unverified, notes people left,
  and who is editing what right now. Editing a file another live session is editing raises a warning.
- **Mail between sessions.** A session can mail another one, or all of them; an idle recipient is woken with it.
- **You can see it work.** The status line shows `)|(` whenever ByxIn acted on a turn, and plain text when it stayed
  out.

## Where ByxIn comes from

ByxIn was built as the brain of **Bombyx OS**, an AI-native operating system on the seL4 microkernel. In Bombyx the
thinking layer can only *ask* for something to move in the physical world: a small, separate gate decides, and only
a person's physical consent opens it. A brain in that seat must not bluff, so ByxIn was built to answer from what it
was shown, to say how it knows, and to say plainly when it does not. This plugin brings that brain to any repository.

## Install

```
/plugin marketplace add hendrikusjohannes92-cell/byxin-grounded
/plugin install byxin-grounded@byxin
```

Or load it for one session: `claude --plugin-dir plugins/byxin-grounded`. It needs Python 3 on the machine
(`python3`, `python` or `py -3`) and git. Nothing else: no server, no account, no model download.

## Use

Ask questions as usual; ByxIn steps in for questions about the project and stays out of conversation. The first
session in a new project says in one line what ByxIn does and how to turn it off.

| command | what it does |
|---|---|
| `/byxin help` | every command in plain words |
| `/byxin on` · `off` · `always` | ground questions (the default) · do nothing · ground every prompt |
| `/byxin brain` | what the other sessions of this project did and left |
| `/byxin sessions` | who is working here right now, and on what |
| `/byxin note <text>` · `notes` | leave a note every session here will read · list them |
| `/byxin send <session or all> <text>` | mail another session (its id, 8 or more of its characters, or its name); an idle one is woken with it |
| `/byxin mail` · `/byxin name <name>` | the mail for this session · give it a name others can mail it by |
| `/byxin events` · `/byxin retract <id> <why>` | the record of sessions; mark one wrong, kept and marked |
| `/byxin share on` | share the brain through the repository, so sessions on other machines and the web join |
| `/byxin where` · `/byxin ask <question>` | which brain serves this project · what ByxIn would show the model for a question |

The model gets one tool, `send`, to mail another session itself (to hand over work or warn about a file you are both
changing). Claude Code's own session messaging reaches live sessions on one machine; ByxIn mail also reaches sessions
on other machines and on the web, and stays in the project's record.

Mail cannot keep sessions busy with no person in the loop: a mail wakes its recipient, the answer wakes the sender, and
a reply to that answer is shown but starts no turn. Mail to `all` is shown to every session and wakes none, and no
sender starts more than three turns an hour in one session. Mail already waiting when a session starts is in the
shared-brain block it reads first, not a wake-up. Who wrote a mail (a person or a session's model) is the sender's
claim, and the woken turn says so.

## Sharing across machines and web sessions

`/byxin share on` writes `.byxin/share.json`; commit it, and every clone unions the brain's write-once events with an
orphan branch (`byxin-brain` by default) through git plumbing alone. Nothing is staged in your index and nothing is
checked out. `BYXIN_SHARE=0` keeps one run on its machine.

A Claude Code on the web session does not load plugins from a repository, but it runs the hooks the repository's
`.claude/settings.json` declares. Point SessionStart, UserPromptSubmit, PostToolUse (Edit|Write|MultiEdit|NotebookEdit),
Stop and SessionEnd at the plugin's classic hook script, from a copy of `plugins/byxin-grounded` in the repository or
one the session fetches:

```
python3 "<path to>/byxin-grounded/brain/classic_hook.py" 2>/dev/null || true
```

A web session then gets the same grounding, the same shared brain and its mail at each prompt, and sees `)|(` in
the hook's message line. The script acts only when `CLAUDE_CODE_REMOTE` is `true`, so a local session with the plugin
never records twice.

## What each hook does

| hook | what changes |
|---|---|
| `session.start` | registers `/byxin` and the `send` tool; reads the shared brain; starts a once-a-minute heartbeat |
| `prompt.compose` | adds one short system-prompt section: six reading rules (how to treat retrieved facts) |
| `prompt.submit` | for a question a person asked about the project: adds the passages that cover it, or a note that the record lacks what it names; adds the shared brain's news since the last prompt. A prompt passed through is not changed |
| `tool.call` | observes which files were edited (for the shared brain) and which were read (as evidence); answers the `send` tool. It never blocks or changes a tool call |
| `turn.complete` | checks the answer's citations, numbers and claims; appends a short note only when something could not be verified; records the turn |
| `session.end` | records that the session ended |

## Data and privacy

- **Loopback only.** The plugin talks to nothing but `127.0.0.1`, and only when you run a local ByxIn engine;
  without one it works entirely offline.
- **Sharing is opt-in.** Nothing leaves your machine unless you commit `.byxin/share.json`. Then the shared events
  go to the git remote that file names, and nowhere else.
- **No credentials.** It never reads tokens, keys or passwords. When sharing is on, `git` pushes with your own git
  setup.
- **No service.** There is no server behind the plugin. Nothing is sent to its author or to anyone else.

The details, so you can decide before installing it:

**What it adds to your prompts.** The reading-rules section, and, for a question about the project, the passages it
retrieved and the shared brain's news. Mail from another session arrives as a new turn of your session (marked as the
plugin's message from another session, never as your words or your instructions); `/byxin off` keeps mail from
starting turns. The plugin does not call a model itself.

**What it runs on your machine.**
- Python 3 (`brain/bridge.py`) when you ask a question about the project, after each turn, and once a minute while
  a session is open (its heartbeat).
- `git` in your repository: reading (`grep`, `ls-files`, `rev-parse`, `log`) always; writing objects and pushing an
  orphan branch only when sharing is on. It never stages anything in your index and never checks out a branch.
- The commands you run through `/byxin <tool>`.

**What it connects to.** Only `127.0.0.1`: a ByxIn engine on port 8080 and its optional local helpers, if you run
them. Without them it works lexically and says so. Nothing goes to the internet unless you turn sharing on.

**What it sends when you turn sharing on.** With a committed `.byxin/share.json`, it pushes the shared brain's
events to the git remote that file names, as an orphan branch, using your own git credentials, and fetches the
other sessions' events from it. An event holds what was asked, the first 600 characters of the answer, the paths of
edited files inside the project, the comparator's verdict, notes, corrections, mail and session names, the session
id, the machine's host name and the branch. A remote listed under `never` is refused; `BYXIN_SHARE=0` keeps one run
on its machine.

**What it stores.** In your repository's `.git/byxin-brain/` (or `~/.byxin/projects/<name>-<hash>/` outside git):
the shared events, one presence file per live session, the brain's record of answered questions and notes (a
SQLite file), and a lexical index. In a project that is itself a ByxIn tree, answers go into that tree's own record
under `data/`, with a small mark there (`data/answering_layer_ask.json`: when, which session) that tells the tree's
own measurements a person is asking.

**What it never does.** It does not change Claude Code's settings or permissions, does not block or rewrite your
tool calls, does not read credentials from your environment, and writes in your working tree only when you run
`/byxin share on` (`.byxin/share.json`) or a `/byxin` tool that you start.

## Tests

`python -m pytest test_shared_brain.py test_grounding.py` exercises the shared brain, mail and grounding against real
git repositories, including a remote, concurrent pushes and the web path. They pass on Windows and Linux.

## License

BSD-2-Clause. See LICENSE. How it works inside: docs/ARCHITECTURE.md.
