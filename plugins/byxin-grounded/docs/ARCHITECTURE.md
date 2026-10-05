# byxin-grounded — ByxIn as a Claude Code mod
`brain/` is ByxIn's brain (tools/, config/, school/); `brain/bridge.py` splits
config/skills/answer_from_knowledge/handler.py around the content model, so the session's model is the content model.
Per question: retrieve (identifier recall + BM25, dense lane if the C++ engine is on :8080) -> lessons -> self-report
-> facts ride beside the prompt -> after the answer the comparator checks citations/numbers/claims against what was
shown and what the session read; a miss is annotated, never rewritten; the answer is recorded as an episode.
`/byxin help` lists the commands. Needs Python 3: the mod tries `python3`, then `python`, then `py -3`.
Engine-only layers (dense retrieval) report "unreachable" rather than faking.

## Which brain serves a project
One brain per project. A project that is itself a ByxIn tree (`tools/byxin_rerank.py`, `tools/byxin_history.py` and the
answering skill) is served by its own brain: its code, its record, its lessons, its loop lock. Any other project is served by `brain/`, its
record kept per project in the shared hub (below), never in this folder: the host watches a loaded plugin's folder and
reloads the module on every save. The dense lane is used only when the engine on the port is this project's own (its
record's writer runs from this project's tree); on a machine whose :8080 is another project's brain, retrieval stays lexical
and says so. `BYXIN_ROOT` names a brain tree explicitly. `/byxin where` shows the choice.

## The shared brain: every session in one project, local or remote
Each session writes what it did as write-once events (start, each turn with what was asked and the files edited and the
comparator's verdict, a note a person left, end) into one hub: `<the repository's shared .git>/byxin-brain/`, the same
from every worktree, or `~/.byxin/projects/<name>-<hash>/` outside git. A session reads the others' events before its first
answer and as they arrive: what was asked, what was edited, what was left unverified, and who is editing what right now
(editing a file another live session is editing raises a toast and tells the model to read it again first). What a
session did is perceived by the mod; a note is told by a person; the block says it is a record of sessions, not a fact
about the code. Answers and notes are also episodes in the brain's own record (source `answering_layer`).
`/byxin brain` · `/byxin note <text>` · `/byxin notes` · `/byxin sessions` · `/byxin share on|off|status|sync`.

**Across machines.** A committed `.byxin/share.json` (`{"remote": "origin", "branch": "claude/byxin-brain"}`) unions the
hub with an orphan branch on that remote by git plumbing alone (a scratch index, hash-object, write-tree, commit-tree,
push; nothing staged in anyone's index, nothing checked out). Events are write-once
with unique names, so the union has no conflict case; two sessions pushing at once both land (retry on a rejected push).
A remote listed under `never` is refused. `BYXIN_SHARE=0` keeps one run's brain on its machine. **A cloud session** (Claude Code on the web) does not load a repository's
plugins, so the repository's `.claude/settings.json` runs `brain/classic_hook.py` as classic hooks; it acts only when
`CLAUDE_CODE_REMOTE` is `true` and exits at once elsewhere, so a local session with the mod never records a turn twice.
Tested by `test_shared_brain.py`: two worktrees, a separate clone as the remote machine, a bare repository as the remote,
concurrent pushes, the classic-hook path, and the red halves (a session alone hears nothing, no `share.json` sends
nothing, a `never` remote is refused, classic hooks stay silent in a local session).

## Retrieval judgment (tested by `test_retrieval_battery.py`)
Without a dense lane the lexical lanes have no similarity floor, so `bridge.py` supplies one: a hit must carry the question's
rare terms (a term in more than 2% of chunks is not vocabulary of the record; its file name counts as text), cover 0.6 of their IDF mass (0.5 when the
question names an identifier or path), and the plugin's own folder is excluded so no source appears twice. A question that retrieves nothing and names no identifier
passes through untouched. Known cost: a short question made only of common words ("what is a lesson in byxin") retrieves nothing.
