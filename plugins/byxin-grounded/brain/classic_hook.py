#!/usr/bin/env python3
"""classic_hook.py -- the shared brain for a session that cannot load the mod: a Claude Code on the web session.



  SessionStart      start event, sync, the shared-brain block as additional context
  UserPromptSubmit  remembers the ask (none for a prompt in an envelope: no one asked), beats, and passes on what
                    other sessions did since the last prompt
  PostToolUse       an edited file joins this turn's files (matcher Edit|Write|MultiEdit|NotebookEdit)
  Stop              the turn: what was asked and what was edited, then sync; a turn no one asked only if it edited
  SessionEnd        end event, sync
SPDX-License-Identifier: BSD-2-Clause
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _state_path(B):
    return os.path.join(B._dir("presence"), "%s.classic.json" % (B.CTX.get("session") or "nosession"))


def _load(B):
    try:
        with open(_state_path(B), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"ask": "", "trigger": "", "turn_files": [], "files": [], "heard": ""}


def _save(B, st):
    with open(_state_path(B) + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(_state_path(B) + ".tmp", _state_path(B))


#: the mod's own question test (hooks/register.tsx QUESTION), kept equal by test_the_web_hooks_ask_like_the_mod
QUESTION = re.compile(r"\?\s*$|^\s*(what|why|how|where|when|who|which|is|are|does|do|did|can|could|explain|describe|tell me)\b",
                      re.I)


def _context(event, text, shown=""):
    """The context the model reads, and the line the person sees: the )|( sigil when ByxIn acted on the turn."""
    out = {}
    if text:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": text}
    if shown:
        out["systemMessage"] = ")|( ByxIn: " + shown
    if out:
        sys.stdout.write(json.dumps(out))


def _last_answer(path):
    """The last assistant text of the turn, from the session transcript (one JSON object per line)."""
    texts = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("type") == "user" and not isinstance((m.get("message") or {}).get("content"), list):
                    texts = []                     # a person's new prompt starts a new turn
                elif m.get("type") == "assistant":
                    for c in (m.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            texts.append(c.get("text") or "")
    except OSError:
        return ""
    return "\n".join(t for t in texts if t).strip()


def main():
    if os.environ.get("CLAUDE_CODE_REMOTE", "").lower() != "true" and os.environ.get("BYXIN_CLASSIC_HOOKS") != "1":
        return 0
    try:
        h = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    event = h.get("hook_event_name") or ""
    sys.path.insert(0, HERE)
    import bridge as B
    cwd = h.get("cwd") or os.getcwd()
    a = {"root": cwd, "project": cwd, "session": h.get("session_id") or ""}
    H = B.setup(a)
    st = _load(B)
    if event == "SessionStart":
        out = B.start(a, H)
        st["heard"] = out.get("at") or ""
        _save(B, st)
        _context("SessionStart", out.get("text") or "")
    elif event == "UserPromptSubmit":
        # a classic hook is told no origin: a prompt in an envelope (a task's notification, a reminder) is no one asking
        prompt = h.get("prompt") or ""
        st["trigger"] = B.envelope_of(prompt) or ""
        st["ask"], st["turn_files"] = ("" if st["trigger"] else prompt[:600]), []
        out = B.beat(dict(a, files=st["files"], since=st.get("heard")), H)
        st["heard"] = out.get("at") or st.get("heard")
        st["prep"] = None
        parts, shown = [out.get("news") or ""], "shared brain: news from the other sessions" if out.get("news") else ""
        if st["ask"] and QUESTION.search(prompt) and not prompt.startswith("/"):
            prep = B.prepare(dict(a, question=prompt), H)
            if prep.get("answerable"):
                parts.append(prep.get("system") or "")
                shown = "grounding · %d passages · %d lessons" % (prep.get("chunks") or 0, len(prep.get("lessons") or []))
                st["prep"] = {"state": prep.get("state"), "chunks": prep.get("chunks"), "lessons": prep.get("lessons")}
            elif prep.get("anchored"):
                parts.append("ByxIn retrieved nothing from the record for this question, which names something that "
                             "should be there. Answer exactly that you cannot see it in the record and stop; do not fill "
                             "the gap from general knowledge, and do not invent a file, number or citation.")
                shown = "grounding · the record lacks what this names"
        _save(B, st)
        _context("UserPromptSubmit", "\n\n".join(x for x in parts if x), shown)
    elif event == "PostToolUse":
        ti = h.get("tool_input") or {}
        p = ti.get("file_path") or ti.get("notebook_path")
        try:
            p = (os.path.relpath(p, cwd) if os.path.isabs(p) else p).replace("\\", "/") if p else None
        except ValueError:
            p = None                                   # another drive: not in the project
        if p and B._in_project(p):                     # a scratchpad or another repository is not this project's
            st["turn_files"] = sorted(set(st.get("turn_files") or []) | {p})
            st["files"] = sorted(set(st.get("files") or []) | {p})
            _save(B, st)
    elif event == "Stop":
        # a turn no one asked is recorded only for what it edited, and says what started it
        outcome, answer, shown = None, _last_answer(h.get("transcript_path") or ""), ""
        if st.get("prep") and answer:
            chk = B.check({"answer": answer, "question": st.get("ask") or "", "state": st["prep"]["state"]}, H)
            verified = bool((chk.get("attribution") or {}).get("verified"))
            outcome = "verified" if verified else "unverified"
            shown = "%s · %d passages" % ("verified" if verified else "UNVERIFIED", st["prep"].get("chunks") or 0)
        if st.get("ask") or (st.get("trigger") and st.get("turn_files")):
            B.turn(dict(a, ask=st.get("ask") or "", files=st.get("turn_files") or [], answer=answer[:600],
                        outcome=outcome, trigger=st.get("trigger") or None), H)
        st["ask"], st["turn_files"], st["trigger"], st["prep"] = "", [], "", None
        _save(B, st)
        _context("Stop", "", shown)
    elif event == "SessionEnd":
        B.end(dict(a, files=st.get("files") or []), H)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)        # a hook that fails must never stop the session it serves
