# byxin-grounded

ByxIn's brain inside Claude Code. Version 0.5.3.

Claude Code answers from what it can see. byxin-grounded puts a small, honest brain beside it:

- **Grounded answers.** For a question about your project it retrieves what the repository holds (identifier recall
  and BM25 with a coverage floor), applies the lessons that bind on it, and after the answer checks every citation,
  number and claim against what was actually shown or read. A miss is annotated, never rewritten.
- **An honest record.** Every answered question is an episode with declared provenance: perceived, told, recalled,
  imagined or inferred. Lessons are corrections that bind next time.
- **One shared brain per project.** Every Claude Code session working in the same repository, local or remote, reads
  what the others did: what was asked, which files were edited, what was left unverified, notes people left, and who
  is editing what right now. Editing a file another live session is editing raises a warning.

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
(`python3`, `python` or `py -3`) and git.

## Use

Ask questions as usual; ByxIn steps in for questions about the project and stays out of conversation.

| command | what it does |
|---|---|
| `/byxin brain` | what the other sessions of this project did and left |
| `/byxin note <text>` | leave a note every session in this project will read |
| `/byxin sessions` | who is working in this project right now, and on what |
| `/byxin events` · `/byxin retract <id> <why>` | the record of sessions; mark one wrong, kept and marked |
| `/byxin share on` | share the brain through the repository, so sessions on other machines join |
| `/byxin where` | which brain serves this project, and where its record lives |
| `/byxin ask <question>` | what ByxIn would show the model for a question |
| `/byxin help` | every command, including the brain's maintenance loop |

## Sharing across machines and cloud sessions

`/byxin share on` writes `.byxin/share.json`; commit it, and every clone unions the brain's write-once events with an
orphan branch (`byxin-brain` by default) through git plumbing alone. Nothing is staged in your index and nothing is
checked out. `BYXIN_SHARE=0` keeps one run on its machine.

A Claude Code on the web session does not load plugins from a repository. To give cloud sessions the shared brain,
keep a copy of `plugins/byxin-grounded` in the repository and run its classic hook script from the project's
`.claude/settings.json` on SessionStart, UserPromptSubmit, PostToolUse (Edit|Write|MultiEdit|NotebookEdit), Stop and
SessionEnd:

```
python3 "$CLAUDE_PROJECT_DIR/<path to>/byxin-grounded/brain/classic_hook.py" 2>/dev/null || true
```

The script acts only when `CLAUDE_CODE_REMOTE` is `true`, so a local session with the plugin never records twice.

## Data and privacy

- **Loopback only.** The plugin talks to nothing but `127.0.0.1`, and only when you run a local ByxIn engine;
  without one it works entirely offline.
- **Sharing is opt-in.** Nothing leaves your machine unless you commit `.byxin/share.json`. Then the shared events
  go to the git remote that file names, and nowhere else.
- **No credentials.** It never reads tokens, keys or passwords. When sharing is on, `git` pushes with your own git
  setup.
- **No service.** There is no server behind the plugin. Nothing is sent to its author or to anyone else.

The details, so you can decide before installing it:

**What it adds to your prompts.** A short system-prompt section with six reading rules, and, for a question about
the project, the passages it retrieved and the shared brain's summary of other sessions. It does not call a model
itself, with two exceptions you start yourself: `/byxin audit`, which runs the Claude Code CLI on your machine to
judge contradictions in the record, and `/byxin engine` (the engine is not included in this release).

**What it runs on your machine.**
- Python 3 (`brain/bridge.py`) when you ask a question about the project, after each turn, and once a minute while
  a session is open (its heartbeat).
- `git` in your repository: reading (`grep`, `ls-files`, `rev-parse`, `log`) always; writing objects and pushing an
  orphan branch only when sharing is on (below). It never stages anything in your index and never checks out a
  branch.
- The commands you run through `/byxin <tool>`, such as the bench or the trainer, and `curl` against `127.0.0.1`
  for `/byxin engine status`.

**What it connects to.** Only `127.0.0.1`: a ByxIn engine on port 8080 and its optional local helpers, if you run
them. Without them it works lexically and says so. Nothing goes to the internet unless you turn sharing on.

**What it sends when you turn sharing on.** With a committed `.byxin/share.json`, it pushes the shared brain's
events to the git remote that file names, as an orphan branch, using your own git credentials, and fetches the
other sessions' events from it. An event holds what was asked, the first 600 characters of the answer, the
paths of edited files, the comparator's verdict, notes and corrections, the session id, the machine's host name,
the branch, and the session's working folder. A remote listed under `never` is refused; `BYXIN_SHARE=0` keeps one run on its machine.

**What it stores.** In your repository's `.git/byxin-brain/` (or `~/.byxin/projects/<name>-<hash>/` outside git):
the shared events, one presence file per live session, the brain's record of answered questions and notes (a
SQLite file), and a lexical index. The engine queue lives in the system temp directory. In a project that is
itself a ByxIn tree, answers go into that tree's own record under `data/`.

**What it never does.** It does not change Claude Code's settings or permissions, does not read credentials from
your environment, and writes in your working tree only when you run `/byxin share on` (`.byxin/share.json`) or a
`/byxin` tool that you start. The one exception is a project that is itself a ByxIn tree: there the brain's own
`data/` folder holds its record and its index, as it does without the plugin.

## Tests

`python -m pytest test_shared_brain.py` exercises the shared brain against real git repositories, including a
remote, concurrent pushes and the cloud path.

## License

BSD-2-Clause. See LICENSE. How it works inside: docs/ARCHITECTURE.md.
