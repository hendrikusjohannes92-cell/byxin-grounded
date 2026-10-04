#!/usr/bin/env python3
"""classic_hook.py -- the shared brain for a session that cannot load the mod: a Claude Code on the web session.



  SessionStart      start event, sync, the shared-brain block as additional context
  UserPromptSubmit  remembers the ask, beats, and passes on what other sessions did since the last prompt
  PostToolUse       an edited file joins this turn's files (matcher Edit|Write|MultiEdit|NotebookEdit)
  Stop              the turn: what was asked and what was edited, then sync
  SessionEnd        end event, sync
SPDX-License-Identifier: BSD-2-Clause
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _state_path(B):
    return os.path.join(B._dir("presence"), "%s.classic.json" % (B.CTX.get("session") or "nosession"))


def _load(B):
    try:
        with open(_state_path(B), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"ask": "", "turn_files": [], "files": [], "heard": ""}


def _save(B, st):
    with open(_state_path(B) + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(_state_path(B) + ".tmp", _state_path(B))


def _context(event, text):
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


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
        st["ask"], st["turn_files"] = (h.get("prompt") or "")[:600], []
        out = B.beat(dict(a, files=st["files"], since=st.get("heard")), H)
        st["heard"] = out.get("at") or st.get("heard")
        _save(B, st)
        _context("UserPromptSubmit", out.get("news") or "")
    elif event == "PostToolUse":
        ti = h.get("tool_input") or {}
        p = ti.get("file_path") or ti.get("notebook_path")
        if p:
            p = os.path.relpath(p, cwd).replace("\\", "/") if os.path.isabs(p) else p
            st["turn_files"] = sorted(set(st.get("turn_files") or []) | {p})
            st["files"] = sorted(set(st.get("files") or []) | {p})
            _save(B, st)
    elif event == "Stop":
        if st.get("ask"):
            B.turn(dict(a, ask=st["ask"], files=st.get("turn_files") or [], answer=""), H)
            st["ask"], st["turn_files"] = "", []
            _save(B, st)
    elif event == "SessionEnd":
        B.end(dict(a, files=st.get("files") or []), H)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)        # a hook that fails must never stop the session it serves
