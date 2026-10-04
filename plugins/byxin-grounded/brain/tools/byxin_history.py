#!/usr/bin/env python3
"""byxin_history.py -- are we actually writing history? Measured, and made true.

JAN ASKED, 2026-09-30: "Are we actually writing history?" Measured before answering: the hippocampus received
93 episodes that day, and nearly every one was the hourly pulse -- "status voice node", "status camera node",
"status motor node", "execute imu node", four rows an hour, source unknown. Not one of the bench questions asked
that day, not one lesson taught, not one idea proposed or refused, was an episode. The old exchange log
(_agent_hook/main-pc/exchanges.jsonl) had been silent since Sep 12. The brain had a heartbeat and no diary.

WHAT HISTORY IS, HERE. An episode with DECLARED provenance: a source that names who or what produced it. The
pulse is source "unknown" and stays that way, because the channel that sends it has not said what it is. The
school -- the bench, the lessons, the ideas rounds -- declares itself:

    bench            every answer the brain gave to a bench question, with its verdict and the condition
    lesson           every lesson taught, with its trigger and the mistake it came from
    idea             every idea proposed, and every one refused -- a teenager's record keeps the refusals
    answering_layer  reserved for the layer itself, when something routes to it

The surprise gate applies to these like everything else: the same question with the same verdict strengthens
the row it already has; a changed verdict is a prediction error and is written. So the record grows with what
changed, which is what a record is for.

THE RULE FOR "YES". writing_history is true when, in the last `hours`, at least one HISTORY episode was
written -- from the school, or from one of the three channels consolidation already trusts (exchanges,
conversation, decision_traces) -- AND the live writer (Byxin's own hippocampus daemon, asked through
byxin.episodic.stats) can be reached and reports a gate it can decide. The pulse alone is NO. A probe
(session_probe, surprise_gate_proof) declares itself and is still NO: a measurement of the record is not a page
of it. A writer that cannot be asked is NO, because a record nobody can vouch for is not one -- the gate was
"on" for three hours today in a note and off in the process.

    py -3.11 tools/byxin_history.py                 # the status, ending in WRITING HISTORY: YES / NO
    py -3.11 tools/byxin_history.py --recent 20     # the last 20 declared episodes
    py -3.11 tools/byxin_history.py --json

Under pytest, record() writes to a scratch database unless BYXIN_HIPPOCAMPUS_DB says otherwise: a test never
writes the live store, and that is decided here, once.
SPDX-License-Identifier: BSD-2-Clause
"""
import argparse
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hippocampus  # noqa: E402

#: The sources the school declares. Anything else is refused by record(): provenance is stated, never guessed.
SCHOOL_SOURCES = ("bench", "lesson", "idea", "answering_layer", "selftest", "trainer", "mind", "maintenance", "exam", "consistency")
#: What an undeclared row looks like. "live" was the manufactured default until 2026-09-30; "unknown" is honest.
UNDECLARED = frozenset(["", "unknown", "live"])
#: The rows that count as history: the school, plus the three channels tools/sleep_consolidator.py trusts. A
#: probe declares itself too and does not count -- it measures the record rather than adding to it.
TRUSTED_CHANNELS = ("exchanges", "conversation", "decision_traces")
HISTORY_SOURCES = SCHOOL_SOURCES + TRUSTED_CHANNELS
DB_ENV = "BYXIN_HIPPOCAMPUS_DB"
RPC_DEFAULT = "http://127.0.0.1:8080"
EXCHANGES_LOG = os.path.join(os.path.dirname(HERE), "_agent_hook", "main-pc", "exchanges.jsonl")
#: The background self-test's own state: its latest run whole (tools/byxin_selftest.py writes it).
SELFTEST_LAST = os.path.join(os.path.dirname(HERE), "data", "byxin_selftest_last.json")


def resolve_db(db=None):
    """Which store. Explicit > BYXIN_HIPPOCAMPUS_DB > (under pytest) a scratch file > the live store."""
    if db:
        return Path(db)
    env = os.environ.get(DB_ENV)
    if env:
        return Path(env)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import tempfile
        return Path(tempfile.gettempdir()) / "byxin_history_under_test.db"
    return Path(hippocampus.DEFAULT_DB)


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record(intent, *, source, outcome, origin, origin_ref=None, skill=None, action=None, narration=None,
           reason=None, operator=None, latency_ms=None, confidence=None, ts_utc=None, db=None):
    """One episode of the school's own history. Returns the row id, or None when the gate strengthened instead.

    Raises on an undeclared source, a missing or unknown origin, an empty intent or an empty outcome: the point
    of this function is that nothing reaches the store without saying what it is AND how the writer knows it.
    The origin (hippocampus.ORIGINS) is the reality monitor's tag: the bench PERCEIVED the answer it recorded, a
    lesson was TOLD by its operator, an idea is IMAGINED, a backfilled row is RECALLED from a file. It is given
    by the caller, never chosen here from the source, because a library guessing the tag is the cortex tagging
    after the fact -- the failure the tag exists to prevent.
    """
    if source not in SCHOOL_SOURCES:
        raise ValueError("history needs a declared source, one of %s; got %r" % (", ".join(SCHOOL_SOURCES), source))
    if origin not in hippocampus.ORIGINS:
        raise ValueError("history needs an origin tag assigned by the channel that writes it, one of %s; got %r"
                         % (", ".join(hippocampus.ORIGINS), origin))
    if not (intent or "").strip():
        raise ValueError("an episode with no intent is not history")
    if not (outcome or "").strip():
        raise ValueError("an episode with no outcome is not history; say what happened")
    conn = hippocampus.open_db(resolve_db(db))
    try:
        # The live daemon holds this file open; wait for its lock rather than lose the row to a silent None.
        conn.execute("PRAGMA busy_timeout = 5000")
        return hippocampus.ingest_episode(
            conn, ts_utc=ts_utc or now_utc(), intent=intent.strip(), skill=skill,
            action=((action or "")[:2000] or None),
            outcome=outcome.strip().lower(), narration=((narration or "")[:4000] or None),
            reason=((reason or "")[:2000] or None), operator=operator, latency_ms=latency_ms,
            confidence=confidence, source=source, origin=origin,
            origin_ref=((origin_ref or "")[:500] or None),
            # a store that cannot be written raises; None means strengthened or already there (helper audit #5)
            strict=True)
    finally:
        conn.close()


def live_writer(rpc=RPC_DEFAULT, timeout=8):
    """Ask the process that does the writing. {"reachable": bool, "stats": dict|None, "error": str|None}."""
    try:
        req = urllib.request.Request(
            rpc, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "byxin.episodic.stats",
                                  "params": {}}).encode(),
            headers={"Content-Type": "application/json"})
        doc = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
        if "result" not in doc or not isinstance(doc["result"], dict):
            return {"reachable": False, "stats": None, "error": "no result: %.200r" % doc}
        return {"reachable": True, "stats": doc["result"], "error": None}
    except Exception as e:
        return {"reachable": False, "stats": None, "error": "%s: %s" % (type(e).__name__, e)}


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"), uri=True)


def status(db=None, rpc=RPC_DEFAULT, hours=24.0, live=None):
    """Everything the answer rests on, as one dict. `live` may be a dict or a callable (tests)."""
    path = resolve_db(db)
    since = time.time() - hours * 3600.0
    out = {"db": str(path), "asked_at": now_utc(), "hours": hours, "total": 0, "by_source_recent": {},
           "by_origin_recent": {}, "declared_recent": 0, "history_recent": 0, "school_recent": {}, "newest": None,
           "newest_history": None, "consolidated": 0, "strength_rows": 0, "newest_strengthen": None}
    marks = ",".join("?" * len(HISTORY_SOURCES))
    if path.exists():
        conn = _ro(path)
        try:
            out["total"] = conn.execute("select count(*) from episodes").fetchone()[0]
            out["by_source_recent"] = {
                (r[0] or ""): r[1] for r in conn.execute(
                    "select source, count(*) from episodes where ts_epoch > ? group by source", (since,))}
            try:
                out["by_origin_recent"] = {
                    (r[0] or "untagged"): r[1] for r in conn.execute(
                        "select origin, count(*) from episodes where ts_epoch > ? group by origin", (since,))}
            except sqlite3.Error:
                out["by_origin_recent"] = {}
            out["declared_recent"] = sum(n for s, n in out["by_source_recent"].items() if s not in UNDECLARED)
            out["history_recent"] = sum(n for s, n in out["by_source_recent"].items() if s in HISTORY_SOURCES)
            out["school_recent"] = {s: n for s, n in out["by_source_recent"].items() if s in SCHOOL_SOURCES}
            out["newest"] = conn.execute("select max(ts_utc) from episodes").fetchone()[0]
            row = conn.execute(
                "select ts_utc, source, outcome, intent from episodes where source in (%s) "
                "order by ts_epoch desc, id desc limit 1" % marks, HISTORY_SOURCES).fetchone()
            if row:
                out["newest_history"] = {"ts_utc": row[0], "source": row[1], "outcome": row[2],
                                         "intent": (row[3] or "")[:120]}
            out["consolidated"] = conn.execute("select count(*) from episodes where consolidated = 1").fetchone()[0]
            try:
                out["strength_rows"] = conn.execute("select count(*) from episode_strength").fetchone()[0]
                out["newest_strengthen"] = conn.execute("select max(last_seen) from episode_strength").fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    # THE LAST SELF-TEST. The loop's own state file first, because it holds the LATEST run whole: a check
    # that recovered to a verdict seen before is strengthened in the record, not written, so the record's
    # newest row for it can be an old red one while the truth is green (measured 2026-09-30, guards). The
    # record then supplies what the state file cannot: the newest outcome per check across all runs, and
    # the newest drift.
    out["selftest_last_run"] = None
    try:
        last = json.loads(io.open(SELFTEST_LAST, encoding="utf-8").read())
        out["selftest_last_run"] = {"run_id": last.get("run_id"),
                                    "checks": {k: v.get("outcome") for k, v in (last.get("results") or {}).items()},
                                    "drift": last.get("drift") or {}, "slots": last.get("slots") or {}}
    except (OSError, ValueError, AttributeError):
        pass
    out["selftest"] = {}
    if path.exists():
        conn = _ro(path)
        try:
            for skill, outcome, ts in conn.execute(
                    "select skill, outcome, ts_utc from episodes where source = 'selftest' "
                    "order by ts_epoch desc, id desc"):
                if skill not in out["selftest"]:
                    out["selftest"][skill] = {"outcome": outcome, "at": ts}
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    if live is None:
        live = live_writer(rpc)
    elif callable(live):
        live = live()
    out["live_writer"] = {"reachable": bool(live.get("reachable")), "error": live.get("error"),
                          "writer": (live.get("stats") or {}).get("writer"),
                          "gate": (live.get("stats") or {}).get("surprise_gate"),
                          "writes_last_24h": (live.get("stats") or {}).get("writes_last_24h")}
    try:
        out["exchanges_log_mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(EXCHANGES_LOG)))
    except OSError:
        out["exchanges_log_mtime"] = None

    reasons = []
    if out["history_recent"] == 0:
        undeclared = sum(n for s, n in out["by_source_recent"].items() if s in UNDECLARED)
        probes = out["declared_recent"]
        reasons.append("no history episode (school or trusted channel) in the last %gh: %d undeclared row(s) "
                       "(the pulse) and %d probe(s) in that window" % (hours, undeclared, probes))
    lw = out["live_writer"]
    if not lw["reachable"]:
        reasons.append("the live writer cannot be asked (%s)" % lw["error"])
    elif not lw["gate"]:
        reasons.append("the live writer does not report its gate -- a daemon older than 2026-09-30 is running")
    elif not lw["gate"].get("enabled") and "unavailable" in str(lw["gate"].get("reason", "")):
        reasons.append("the live gate cannot decide: %s" % lw["gate"]["reason"])
    out["reasons"] = reasons
    out["writing_history"] = not reasons
    return out


def render(st):
    lines = ["ARE WE WRITING HISTORY?  asked %s, window %gh, store %s" % (st["asked_at"], st["hours"], st["db"]),
             ""]
    lines.append("  episodes in the store            %d  (consolidated %d)" % (st["total"], st["consolidated"]))
    lines.append("  written in the window            %d  by source: %s" % (
        sum(st["by_source_recent"].values()),
        ", ".join("%s=%d" % (s or "(blank)", n) for s, n in sorted(st["by_source_recent"].items(),
                                                                 key=lambda kv: -kv[1])) or "none"))
    lines.append("  of which history                 %d  school: %s  (declared probes, not history: %d)" % (
        st["history_recent"],
        ", ".join("%s=%d" % kv for kv in sorted(st["school_recent"].items())) or "none",
        st["declared_recent"] - st["history_recent"]))
    nd = st["newest_history"]
    lines.append("  newest history episode           %s" % (
        "%s  [%s/%s]  %s" % (nd["ts_utc"], nd["source"], nd["outcome"], nd["intent"]) if nd else "none ever"))
    lines.append("  newest row of any kind           %s" % (st["newest"] or "none"))
    bo = st.get("by_origin_recent") or {}
    lines.append("  origin tagged in the window      %s" % (
        ", ".join("%s=%d" % kv for kv in sorted(bo.items(), key=lambda kv: -kv[1])) or "none"))
    lines.append("  strengthened instead of written  %d intent(s), newest %s" % (
        st["strength_rows"], st["newest_strengthen"] or "never"))
    lw = st["live_writer"]
    if lw["reachable"]:
        g = lw["gate"] or {}
        lines.append("  live writer                      reachable; %s" % (lw["writer"] or "path not reported"))
        lines.append("  live surprise gate               %s (%s)" % (
            "ON" if g.get("enabled") else "OFF", g.get("reason", "not reported")))
    else:
        lines.append("  live writer                      NOT REACHABLE: %s" % lw["error"])
    lines.append("  old exchange log                 last written %s" % (st["exchanges_log_mtime"] or "never"))
    lr = st.get("selftest_last_run")
    sts = st.get("selftest") or {}
    if lr and lr.get("checks"):
        # Sentences, not key=value rows: asked "did the last self-test find drift or open a slot", the model read
        # "drift it found  bench: a -> b" and "lesson slots it moved  opened ['x']" as no drift and no slot
        # (measured 2026-09-30). A statement with a verb is read as what it says.
        lines.append("  last self-test run (background)  %s: %s" % (
            lr["run_id"], ", ".join("%s=%s" % kv for kv in sorted(lr["checks"].items()))))
        if lr.get("drift"):
            lines.append("  In that run the self-test FOUND DRIFT on %d check(s): %s." % (
                len(lr["drift"]), "; ".join("%s changed from %s to %s" % (k, a, b)
                                            for k, (a, b) in sorted(lr["drift"].items()))))
        else:
            lines.append("  In that run the self-test found no drift: every check gave the verdict it gave before.")
        slots = lr.get("slots") or {}
        if slots.get("opened") or slots.get("closed"):
            lines.append("  In that run it OPENED a lesson slot for %s and CLOSED the slot for %s." % (
                ", ".join(slots.get("opened") or []) or "no question",
                ", ".join(slots.get("closed") or []) or "no question"))
        else:
            lines.append("  In that run it opened no lesson slot and closed none.")
    elif sts:
        newest = max(v["at"] for v in sts.values())
        checks = ", ".join("%s=%s" % (k, v["outcome"]) for k, v in sorted(sts.items()) if k != "drift")
        lines.append("  last self-test (from the record) %s: %s" % (newest, checks))
    else:
        lines.append("  last self-test (background)      none recorded")
    if "drift" in sts:
        lines.append("  last drift written to the record %s: %s" % (sts["drift"]["at"], sts["drift"]["outcome"]))
    lines.append("")
    if st["writing_history"]:
        lines.append("WRITING HISTORY: YES")
    else:
        lines.append("WRITING HISTORY: NO")
        for r in st["reasons"]:
            lines.append("  - " + r)
    return "\n".join(lines)


def recent(n=20, db=None):
    path = resolve_db(db)
    if not path.exists():
        return []
    conn = _ro(path)
    try:
        marks = ",".join("?" * len(HISTORY_SOURCES))
        return [dict(ts_utc=r[0], source=r[1], outcome=r[2], skill=r[3], operator=r[4], intent=r[5])
                for r in conn.execute(
                    "select ts_utc, source, outcome, skill, operator, intent from episodes "
                    "where source in (%s) order by ts_epoch desc, id desc limit ?" % marks,
                    HISTORY_SOURCES + (int(n),))]
    finally:
        conn.close()


def backfill_bench(saved_json, condition, ts_utc, db=None):
    """Today's bench runs, recorded after the fact from their saved answers, labelled as such.

    A saved run holds every answer and verdict (run) or every answer and every verdict (run_repeated). The
    timestamp is the caller's -- the file's mtime is the honest choice -- and the reason names the file, so a
    reader can tell a backfilled row from one written as it happened. Repetitions of one question are spaced a
    second apart, because the store keys a row on (ts_utc, intent, source) and would otherwise keep only the
    first verdict of three.
    """
    import calendar
    import byxin_bench as bench
    by_id = {q["id"]: q for q in bench.QUESTIONS}
    rows = json.loads(Path(saved_json).read_text(encoding="utf-8"))
    base = calendar.timegm(time.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ"))
    n = 0
    for r in rows:
        q = by_id.get(r.get("id"))
        if not q:
            continue
        answers = r.get("answers") if isinstance(r.get("answers"), list) else [r.get("answer", "")]
        verdicts = r.get("verdicts") if isinstance(r.get("verdicts"), list) else [r.get("byxin", "")]
        for i, (ans, v) in enumerate(zip(answers, verdicts)):
            if not v:
                continue
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i))
            record(q["question"], source="bench", skill=condition, outcome=v, narration=ans,
                   reason="%s; backfilled from %s" % (r["id"], os.path.basename(saved_json)),
                   operator="bench", ts_utc=ts, db=db,
                   origin="recalled", origin_ref="saved run %s" % os.path.basename(saved_json))
            n += 1
    return n


def _local_ledger_ts_to_utc(at):
    """A legacy ledger stamp is local time without a zone ("%Y-%m-%dT%H:%M:%S"); since 2026-10-01 the lessons
    ledger stamps UTC with a Z like everything else, and such a stamp is kept as it is."""
    if isinstance(at, str) and at.endswith("Z"):
        return at
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime(time.strptime(at, "%Y-%m-%dT%H:%M:%S"))))
    except (ValueError, TypeError, OverflowError):
        return None


def backfill_lessons(db=None):
    """Every lesson already in school/byxin_lessons.jsonl, recorded at its own `at`, labelled backfilled."""
    import byxin_lessons as L
    n = 0
    for row in L.load():
        ts = _local_ledger_ts_to_utc(row.get("at"))
        if not ts:
            continue
        record("lesson taught: %s" % row["slug"], source="lesson", skill="school", outcome="taught",
               narration=row.get("lesson"), operator=row.get("taught_by"), ts_utc=ts,
               reason="trigger=/%s/; mistake: %s; backfilled from school/byxin_lessons.jsonl"
                      % (row.get("trigger"), str(row.get("mistake"))[:300]), db=db,
               origin="recalled", origin_ref="school/byxin_lessons.jsonl row %s" % row["slug"])
        n += 1
    return n


def backfill_ideas(db=None):
    """Every idea already in school/byxin_ideas.jsonl, at its own `at`. Refusals were never written down there,
    so only the proposals can be recovered; from now on add() records both."""
    import byxin_ideas as I
    n = 0
    for row in I.load():
        ts = _local_ledger_ts_to_utc(row.get("at"))
        if not ts:
            continue
        record("idea #%d by %s: %s" % (row["n"], row["by"], row["idea"][:160]), source="idea",
               skill="ideas_round", outcome="proposed", narration=row["idea"], operator=str(row["by"]), ts_utc=ts,
               reason="grounded in: %s; how we know: %s; backfilled from school/byxin_ideas.jsonl"
                      % (row.get("grounded_in"), row.get("how_we_know")), db=db,
               origin="recalled", origin_ref="school/byxin_ideas.jsonl row #%d" % row["n"])
        n += 1
    return n


#: What counts as the brain's own development: the paths a commit must touch to be a brain commit.
BRAIN_PATHS = ("tools/byxin_*.py", "tools/hippocampus*.py", "tools/sleep_consolidator.py", "tools/ltm_head.py",
               "tools/byxin_bm25.py", "config/skills/answer_from_knowledge", "school",
               "byxin/src/cerebellum.cpp", "byxin/src/ollama_client.cpp", "byxin/config/cerebellum.json")


def _git_log(root, since_days, paths=()):
    import subprocess
    cmd = ["git", "-C", root, "log", "--since=%d.days" % since_days, "--format=%h%x09%ad%x09%s", "--date=short"]
    if paths:
        cmd += ["--"] + list(paths)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="utf-8",
                         errors="replace").stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"hash": parts[0], "date": parts[1], "subject": parts[2].strip()})
    return rows


def recent_development(days=7, limit=12, root=None):
    """
    """
    root = root or os.path.dirname(HERE)
    all_rows = _git_log(root, days)
    brain = _git_log(root, days, BRAIN_PATHS)
    import byxin_world as _world
    os_rows = _git_log(root, days, tuple(_world.os_paths())) if _world.os_paths() else []
    return {"days": days, "all": len(all_rows), "brain": len(brain), "os": len(os_rows), "newest": brain[:limit]}


def render_development(d):
    """Sentences, because the model read key=value rows as their opposite (29eaf2d)."""
    if not d or not d.get("all"):
        return ("In the last %d days git records no commit at all, so nothing changed -- or this tree is not the "
                "repository." % (d or {}).get("days", 7))
    import byxin_world as _world
    out = ["In the last %d days the repository took %d commits; %d of them changed the brain (the answering layer, "
           "the retriever, the school, the hippocampus, the brain pages) and %d changed %s (%s)."
           % (d["days"], d["all"], d["brain"], d["os"], _world.os_name(), ", ".join(_world.os_paths()) or "no OS paths")]
    if d["newest"]:
        out.append("The newest brain commits, newest first, each as 'date hash: what it did':")
        for r in d["newest"]:
            out.append("- %s %s: %s" % (r["date"], r["hash"], r["subject"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="store to read (default: the live one; BYXIN_HIPPOCAMPUS_DB overrides)")
    ap.add_argument("--rpc", default=RPC_DEFAULT)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--recent", type=int, metavar="N", help="list the last N declared episodes")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill-bench", metavar="SAVED_JSON")
    ap.add_argument("--condition", default="BRAIN", help="with --backfill-bench: BARE / LAYERED / BRAIN")
    ap.add_argument("--at", help="with --backfill-bench: the UTC timestamp to record (default: file mtime)")
    ap.add_argument("--backfill-lessons", action="store_true", help="record every lesson in the ledger at its own time")
    ap.add_argument("--backfill-ideas", action="store_true", help="record every idea in the ledger at its own time")
    a = ap.parse_args(argv)
    if a.backfill_lessons:
        print("backfilled %d lesson(s) from school/byxin_lessons.jsonl" % backfill_lessons(db=a.db))
        return 0
    if a.backfill_ideas:
        print("backfilled %d idea(s) from school/byxin_ideas.jsonl" % backfill_ideas(db=a.db))
        return 0
    if a.backfill_bench:
        ts = a.at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(a.backfill_bench)))
        n = backfill_bench(a.backfill_bench, a.condition, ts, db=a.db)
        print("backfilled %d bench answer(s) from %s at %s as %s" % (n, a.backfill_bench, ts, a.condition))
        return 0
    if a.recent:
        for r in recent(a.recent, db=a.db):
            print("%s  %-8s %-10s %-22s %s" % (r["ts_utc"], r["source"], r["outcome"], (r["skill"] or "")[:22],
                                              (r["intent"] or "")[:80]))
        return 0
    st = status(db=a.db, rpc=a.rpc, hours=a.hours)
    print(json.dumps(st, indent=1, ensure_ascii=False) if a.json else render(st))
    return 0 if st["writing_history"] else 1


if __name__ == "__main__":
    sys.exit(main())
