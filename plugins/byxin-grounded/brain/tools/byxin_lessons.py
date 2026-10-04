#!/usr/bin/env python3
"""byxin_lessons.py -- the school half of the loop: a lesson is a correction that binds NEXT time.


WHAT WAS ALREADY THERE, AND WHY IT IS NOT THIS. _agent_hook/main-pc/lessons/ holds 145 files, and every one is a
REPORT CARD: {ts, curriculum, lesson_id, prompts_run, passes, pass_rate, leak_count, ...}. They record how a
drill went. None of them contains anything a brain could learn FROM. The school has been giving exams and no
lessons.

WHAT A LESSON IS HERE, and each part is required because a lesson missing any of them cannot do its job:

  MISTAKE     what the brain actually said or did, quoted. Not a category -- the words.
  MEASUREMENT how we know it was wrong. A probe, a file, a number. Without this a lesson is an opinion, and
              this project has a rule about that.
  LESSON      the GENERALISABLE rule, which must be wider than the mistake. "Component A is not verified"
              is an answer and teaches nothing; "read which noun a qualifier attaches to before repeating it"
              is a lesson and applies to sentences nobody has written yet.
  HOW WE KNOW it took: the question or test whose verdict must change. A lesson nobody can check is a slogan.
  TRIGGER     a regular expression over the QUESTION. When it matches, the lesson is injected. See below for why
              this is required rather than optional.




A lesson may also carry quote_file and quote_pattern, and then the authoritative sentence is lifted VERBATIM
from that file at injection time, so it keeps one definition instead of being copied here and drifting.

Lessons still render to docs/lessons/, which the Cerebellum ingests, so they are readable by a person and
retrievable as background -- just not RELIED ON to arrive that way.

    py -3.11 tools/byxin_lessons.py --add-file lesson.json
    py -3.11 tools/byxin_lessons.py --render          # write docs/lessons/*.md
    py -3.11 tools/byxin_lessons.py --list
SPDX-License-Identifier: BSD-2-Clause
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def repo_root():
    try:
        out = subprocess.run(["git", "-C", HERE, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if out:
            return os.path.dirname(out) if os.path.basename(out) == ".git" else out
    except Exception:
        pass
    return os.path.dirname(HERE)


ROOT = repo_root()
LEDGER = os.path.join(ROOT, "school", "byxin_lessons.jsonl")
PAGES = os.path.join(ROOT, "school", "lessons")
REQUIRED = ("slug", "mistake", "measurement", "lesson", "how_we_know", "trigger")
#: A lesson must generalise past its own example, or it is just the answer to one question written down.
_MIN_LESSON = 40
_WORDS = re.compile(r"[a-z0-9]+")


def _tokens(t):
    return {w for w in _WORDS.findall((t or "").lower()) if len(w) > 3}


def load():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with io.open(LEDGER, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def scope_of(row):
    """
    """
    s = str((row or {}).get("scope") or "").strip().lower()
    if s in ("brain", "world"):
        return s
    return "world" if (row or {}).get("quote_file") else "brain"


def add(row, ledger=None):
    """Append one lesson. Raises unless it carries every part, and unless the rule is wider than the mistake."""
    for k in REQUIRED:
        if not str(row.get(k) or "").strip():
            raise ValueError("a lesson needs %s -- missing or empty" % k)
    if not re.match(r"^[a-z0-9][a-z0-9-]{4,60}$", row["slug"]):
        raise ValueError("slug must be lower-case kebab-case: %r" % row["slug"])
    try:
        re.compile(row["trigger"])
    except re.error as e:
        raise ValueError("the trigger is not a valid pattern (%s) -- it would take every other lesson down "
                         "with it" % e)
    if len(row["lesson"].strip()) < _MIN_LESSON:
        raise ValueError("the lesson is too short to be a rule (%d chars); state what it means in general"
                         % len(row["lesson"].strip()))
    # The rule must not simply restate the mistake. A lesson whose words are a subset of the mistake's words is
    # the answer to one question, and it will not fire on a question nobody has asked yet.
    m, l = _tokens(row["mistake"]), _tokens(row["lesson"])
    if l and m and len(l - m) < 3:
        raise ValueError("the lesson only restates the mistake -- say the general rule, not the one answer")
    existing = load() if ledger is None else ledger
    if any(e["slug"] == row["slug"] for e in existing):
        raise ValueError("a lesson with slug %r already exists" % row["slug"])
    row = dict(row)
    row.setdefault("at", _now())      # one clock: UTC with a Z, like every other ledger (helper audit #9)
    row.setdefault("taught_by", "the school")
    row.setdefault("scope", scope_of(row))
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with io.open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    # A lesson taught is an episode of the school. Raises if it cannot be recorded: a lesson the record does
    # not know about was, as far as the brain's own history goes, never taught.
    sys.path.insert(0, HERE)
    import byxin_history
    byxin_history.record("lesson taught: %s" % row["slug"], source="lesson", skill="school", outcome="taught",
                         narration=row["lesson"], operator=row.get("taught_by"),
                         reason="trigger=/%s/; mistake: %s" % (row["trigger"], str(row["mistake"])[:300]),
                         # a lesson is TOLD: its operator is the one who said so
                         # how the brain came to hold the rule: a page told it (a drafted lesson names the page and
                         # who checked it) or a person said so (helper audit #10)
                         origin="told", origin_ref=("drafted_from=%s; checked_by=%s" % (row["drafted_from"], row.get("checked_by", "?"))
                                                    if row.get("drafted_from") else "taught_by=%s" % row.get("taught_by")))
    return row


def _now():
    """UTC with a Z -- the clock every other ledger and the record use (helper audit #9: this ledger stamped local
    time with no zone, the backfill converted with mktime, and the model was told at[:10] as the teaching date)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rewrite(rows):
    """The ledger rewritten whole, atomically: to a sibling file, re-read and parsed line by line, then renamed
    over the old one. Review 2026-10-01: revise/withdraw truncated and rewrote in place, so a reader (the
    answering layer, in its own process, at any moment) could see a half-written school, and a crash left
    nothing. A trainer and a session can both write this file; the rename is the only step a reader sees."""
    tmp = LEDGER + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with io.open(tmp, encoding="utf-8") as fh:
        back = [json.loads(line) for line in fh if line.strip()]
    if len(back) != len(rows):
        raise RuntimeError("the rewritten ledger does not read back whole (%d of %d rows)" % (len(back), len(rows)))
    os.replace(tmp, LEDGER)


def revise(slug, changes, reason, by="the school"):
    """Change fields of one lesson in place, number the revision, and record it as its own event.

    Measured 2026-09-30: the second revision of one lesson in one hour was STRENGTHENED by the surprise gate
    rather than written -- same intent, same outcome as the first -- so its reason survived only in the ledger
    row. A revision is a new event; its number goes into the intent, so each one is a row of its own.
    """
    rows = load()
    row = next((r for r in rows if r.get("slug") == slug), None)
    if row is None:
        raise ValueError("no lesson with slug %r" % slug)
    if "slug" in changes:
        raise ValueError("the slug is the lesson's identity; teach a new lesson instead of renaming one")
    for k, v in changes.items():
        row[k] = v
    if "trigger" in changes:
        try:
            re.compile(row["trigger"])
        except re.error as e:
            raise ValueError("the new trigger is not a valid pattern (%s)" % e)
    sys.path.insert(0, HERE)
    import byxin_history
    if set(changes) <= METADATA:
        _rewrite(rows)
        byxin_history.record("lesson %s: %s" % (", ".join(sorted(changes)), slug), source="lesson", origin="told",
                             origin_ref="taught_by=%s" % by, skill="school", outcome="scoped", operator=by,
                             narration=json.dumps(changes, ensure_ascii=False)[:4000], reason=reason)
        return row
    n = int(row.get("revision") or 0) + 1
    row["revision"] = n
    row["revised"] = "%s (#%d): %s" % (_now(), n, reason)
    _rewrite(rows)
    byxin_history.record("lesson revised #%d: %s" % (n, slug), source="lesson", origin="told",
                         origin_ref="taught_by=%s" % by, skill="school", outcome="revised", operator=by,
                         narration=json.dumps(changes, ensure_ascii=False)[:4000], reason=reason)
    return row


#: fields that say what a lesson is FOR, not what it says: changing them is never a revision the prompt shows
METADATA = {"scope"}


def withdraw(slug, reason, by="the school"):
    """
    """
    rows = load()
    row = next((r for r in rows if r.get("slug") == slug), None)
    if row is None:
        raise ValueError("no lesson with slug %r" % slug)
    if row.get("status") == "withdrawn":
        raise ValueError("lesson %r is already withdrawn" % slug)
    row["status"] = "withdrawn"
    row["withdrawn"] = "%s by %s: %s" % (_now(), by, reason)
    _rewrite(rows)
    sys.path.insert(0, HERE)
    import byxin_history
    byxin_history.record("lesson withdrawn: %s" % slug, source="lesson", origin="told",
                         origin_ref="withdrawn_by=%s" % by, skill="school", outcome="withdrawn", operator=by,
                         narration=row.get("lesson", "")[:4000], reason=reason)
    return row


def render():
    """One markdown page per lesson under docs/lessons/, where the Cerebellum will ingest them.

    Written as prose a retrieval hit can stand on its own: the rule first, because a chunk may be read without
    its neighbours, then what went wrong, then how anyone can check whether it took.
    """
    rows = load()
    os.makedirs(PAGES, exist_ok=True)
    written = []
    for r in rows:
        path = os.path.join(PAGES, "%s.md" % r["slug"])
        body = [
            "# Lesson: %s" % r.get("title", r["slug"].replace("-", " ")),
            "",
            "**The rule.** %s" % r["lesson"].strip(),
            "",
            "**What went wrong.** %s" % r["mistake"].strip(),
            "",
            "**How we know it was wrong.** %s" % r["measurement"].strip(),
            "",
            "**How we know the lesson took.** %s" % r["how_we_know"].strip(),
            "",
        ]
        if r.get("applies_to"):
            body += ["**Where it applies.** %s" % r["applies_to"].strip(), ""]
        body += ["*Taught %s by %s. This page is ingested by the Cerebellum from docs/, so the rule is "
                 "retrievable by the ordinary path -- nothing is hardcoded to make it bind.*"
                 % (r.get("at", "?"), r.get("taught_by", "?")), ""]
        text = "\n".join(body)
        old = io.open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if old != text:
            io.open(path, "w", encoding="utf-8", newline="\n").write(text)
            written.append(os.path.relpath(path, ROOT))
    return rows, written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--add-file", help="a JSON file holding one lesson, or a list of them")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)

    if a.add_file:
        doc = json.load(io.open(a.add_file, encoding="utf-8"))
        for row in (doc if isinstance(doc, list) else [doc]):
            try:
                got = add(row)
                print("added: %s" % got["slug"])
            except ValueError as e:
                print("refused (%s): %s" % (row.get("slug", "?"), e))
    if a.render:
        rows, written = render()
        print("%d lesson(s); %d page(s) written or changed" % (len(rows), len(written)))
        for w in written:
            print("  %s" % w)
    if a.list:
        for r in load():
            print("%-40s %s" % (r["slug"], r["lesson"][:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
