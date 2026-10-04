"""

Companion to the existing semantic Cerebellum. Where Cerebellum answers
"what is X" via embedding similarity, Hippocampus answers "when did we
last do X / what did we do yesterday / what did I tell the brain about Y"
via time-keyed structured retrieval.


Storage: SQLite (stdlib, no new deps, single file `data/hippocampus.db`).
Schema indexed for fast time-range and per-operator queries. Optional
embedding column for hybrid time+content retrieval — populated lazily.

RPC contract exposed via byxin.episodic.recall (Code session will wire
the C++ side per the design doc).

Speed Priority: time-range queries on indexed SQLite columns are sub-ms
for any reasonable corpus size. Embedding-similarity is opt-in via
`content_query` param and only runs against the candidate set filtered by
the time/operator/skill predicates first.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "hippocampus.db"
DECISION_TRACES = REPO_ROOT / "data" / "decision_traces.jsonl"
CONVERSATION_MEM = REPO_ROOT / "data" / "conversation_memory.jsonl"
EXCHANGES = REPO_ROOT / "_agent_hook" / "main-pc" / "exchanges.jsonl"


# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,
    ts_epoch        REAL    NOT NULL,
    operator        TEXT,
    intent          TEXT    NOT NULL,
    intent_norm     TEXT,
    skill           TEXT,
    action          TEXT,
    confidence      REAL,
    outcome         TEXT,
    narration       TEXT,
    reason          TEXT,
    latency_ms      REAL,
    embedding       BLOB,
    source          TEXT,
    origin          TEXT,
    origin_ref      TEXT,
    consolidated    INTEGER DEFAULT 0,
    UNIQUE(ts_utc, intent, source)
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts_epoch ON episodes(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_episodes_operator ON episodes(operator);
CREATE INDEX IF NOT EXISTS idx_episodes_skill    ON episodes(skill);
CREATE INDEX IF NOT EXISTS idx_episodes_source   ON episodes(source);
"""


# ── Connection helpers ───────────────────────────────────────────────────────

ORIGINS = ("perceived", "told", "recalled", "imagined", "inferred")


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after the table was first created reach an EXISTING store here.

    CREATE TABLE IF NOT EXISTS is a no-op on a table that exists, so a column added to SCHEMA would reach every
    new store and never the live one -- the 21.9 MB store that matters. Each ADD COLUMN is idempotent by check.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
    for col in ("origin", "origin_ref"):
        if col not in have:
            conn.execute("ALTER TABLE episodes ADD COLUMN %s TEXT" % col)
    conn.commit()


def open_db(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the Hippocampus SQLite store. Caller closes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # The live daemon holds this file open; a second writer waits for its lock instead of failing at once.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _normalize_intent(text: str) -> str:
    """Lightweight lowercase + strip for dedupe + match-key. Mirrors the
    smart_agent normalize() so episodic retrieval composes with intent_cache.
    """
    if not text:
        return ""
    return text.lower().strip().rstrip("?.!").strip()


def _to_epoch(ts: str | float | int | None) -> Optional[float]:
    """Accept ISO 8601 string or epoch number. Return float seconds since epoch."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    # Strip trailing Z to make fromisoformat happy across versions
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _from_epoch(epoch: float | None) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Ingest ───────────────────────────────────────────────────────────────────

_GATE_UNAVAILABLE_REPORTED = False


def surprise_gate_state() -> dict:
    """The surprise gate as THIS process sees it: {"enabled", "reason", "policy", "sentinel"}.

    Asked of tools/byxin_surprise.py, so there is ONE answer to "is the gate on". A local copy of the test would
    drift, and then some writers would gate and others would not -- a store that is a mix of two policies with
    no way to tell which row came from which.

    WHY THE REASON IS PART OF THE ANSWER. From 2026-09-30 15:33 to 18:2x the sentinel existed, the record said
    the gate was ON, and the live brain wrote every routine repeat anyway: Byxin runs this file from
    byxin/build/Release/tools/, and byxin_surprise.py had not been deployed there, so the import below failed
    and the old _surprise_gate_on() answered False -- correctly, since a gate that cannot decide must not be
    the reason something was forgotten, but silently. A fail-safe that nobody can see is a fault that nobody
    can find. So the failure is named here, reported to stderr once (Byxin keeps it in logs/byxin_err.log),
    and carried by stats() so byxin.episodic.stats tells the live truth rather than the intended one.
    """
    global _GATE_UNAVAILABLE_REPORTED
    here = Path(__file__).resolve()
    try:
        sys.path.insert(0, str(here.parent))
        import byxin_surprise as surprise
    except Exception as exc:
        reason = "policy module unavailable next to %s: %s: %s" % (here, type(exc).__name__, exc)
        if not _GATE_UNAVAILABLE_REPORTED:
            _GATE_UNAVAILABLE_REPORTED = True
            try:
                sys.stderr.write("[Hippocampus] SURPRISE GATE OFF -- %s\n" % reason)
                sys.stderr.flush()
            except Exception:
                pass
        return {"enabled": False, "reason": reason, "policy": None, "sentinel": None}
    try:
        on = bool(surprise.gate_enabled())
    except Exception as exc:
        return {"enabled": False, "reason": "gate_enabled() failed: %s: %s" % (type(exc).__name__, exc),
                "policy": getattr(surprise, "__file__", None), "sentinel": getattr(surprise, "SENTINEL", None)}
    return {"enabled": on,
            "reason": "sentinel present" if on else "sentinel absent",
            "policy": getattr(surprise, "__file__", None),
            "sentinel": getattr(surprise, "SENTINEL", None)}


def _surprise_gate_on() -> bool:
    """Is the surprise gate on, for the write path. On any failure to ask, no: nothing must be lost."""
    try:
        return bool(surprise_gate_state()["enabled"])
    except Exception:
        return False


def _strength_table(conn: sqlite3.Connection) -> None:
    """The count of routine sightings, beside the episodes rather than inside them.

    A side table on purpose: adding columns to a 21.9 MB live `episodes` table would change the shape every
    existing reader sees, for a feature that is off by default. This keeps the canonical episode row exactly as
    it is and records how often it has been seen again -- which is the half a suppression policy usually loses,
    and the half consolidation needs to tell "seen 6103 times" from "seen once".
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS episode_strength (
                        intent_norm TEXT PRIMARY KEY,
                        times_seen  INTEGER NOT NULL DEFAULT 1,
                        last_seen   TEXT)""")
    conn.commit()


def _surprise_verdict(conn: sqlite3.Connection, *, intent: str, skill, outcome) -> str:
    """"write" or "strengthen" for this episode, decided by tools/byxin_surprise.py.

    The policy lives in that module and nowhere else, so the rule the write path applies and the rule the replay
    measured are the same rule. Any failure here returns "write": a gate that cannot decide must not be the
    reason something was forgotten.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import byxin_surprise as surprise
    except Exception:
        return "write"
    try:
        norm = _normalize_intent(intent)
        mem = surprise.Memory()
        # What memory already holds about this intent, read from the episodes themselves: every skill/outcome
        # pair seen for it. Bounded by the intent, so this stays cheap on the write path.
        rows = list(conn.execute(
            "select skill, outcome, count(*) from episodes where intent_norm = ? group by skill, outcome",
            (norm,)))
        if rows:
            for sk, oc, _n in rows:
                mem.learn(norm, (sk or "").strip(), (oc or "").strip().lower())
            seen = sum(n for _s, _o, n in rows)
            entry = mem.predict(norm)
            if entry is not None:
                entry["times_seen"] = seen
        action, _reason = surprise.decide(
            {"intent": intent, "intent_norm": norm, "skill": skill, "outcome": outcome}, mem)
        return action
    except Exception:
        return "write"


def _strengthen(conn: sqlite3.Connection, intent_norm: str, ts_utc: str) -> None:
    """Record that a remembered episode was seen again. Never raises into the caller's write path."""
    try:
        _strength_table(conn)
        conn.execute("""INSERT INTO episode_strength (intent_norm, times_seen, last_seen)
                        VALUES (?, 1, ?)
                        ON CONFLICT(intent_norm) DO UPDATE
                          SET times_seen = times_seen + 1, last_seen = excluded.last_seen""",
                     (intent_norm, ts_utc))
        conn.commit()
    except sqlite3.Error:
        pass


def ingest_episode(
    conn: sqlite3.Connection,
    *,
    ts_utc: str,
    intent: str,
    skill: Optional[str] = None,
    action: Optional[str] = None,
    confidence: Optional[float] = None,
    outcome: Optional[str] = None,
    narration: Optional[str] = None,
    reason: Optional[str] = None,
    latency_ms: Optional[float] = None,
    operator: Optional[str] = None,
    # PROVENANCE IS DECLARED, NEVER DEFAULTED TO A CLAIM. This said "live" until 2026-09-30, and that word is
    # why the brain could not learn from its own history: 70267 of 71932 episodes claimed to be lived
    # experience because nobody had said otherwise, so "execute imu node" recorded 6103 times by a curriculum
    # sweep was indistinguishable from a person asking something once. A default that turns silence into a claim
    # is worse than no label, because a reader cannot tell the two apart. "unknown" is true, and
    # tools/sleep_consolidator.py can then refuse what it cannot vouch for.
    source: str = "unknown",
    # The reality monitor's tag and its reference (see ORIGINS). Declared by the channel or absent; a value
    # outside the vocabulary is refused loudly, because a tag that means nothing is worse than no tag.
    origin: Optional[str] = None,
    origin_ref: Optional[str] = None,
    strict: bool = False,
) -> Optional[int]:
    """Insert one episode. Returns row id or None on dedupe conflict.

    SURPRISE GATE, off unless BYXIN_SURPRISE_GATE=1. ByxIn proposed it (ideas ledger #25) from the Titans note
    in its own corpus, which has said since 2026-05-28 that "only novel/surprising events should be written,
    routine events should be skipped" and named this module as where it belongs. Measured 2026-09-30, four
    months later: 71926 episodes, 1344 distinct intents, and "execute imu node" recorded 6103 times.

    With the gate on, a routine repeat increments a count in the episode_strength side table instead of adding
    a row -- the `episodes` schema is untouched, so every existing reader is unaffected. A novel intent, a
    changed outcome or skill, and anything needing consent or review are always written; see
    tools/byxin_surprise.py for the rule and tools/test_the_surprise_gate_keeps_what_matters.py for the red
    halves. Replayed over the whole real store it writes 1716 of 71926, keeps all 1323 distinct intents and
    every outcome class.

    DEFAULT OFF on purpose: this changes what the brain remembers, and it should be switched on deliberately by
    someone who has read the replay, not by a library import.
    """
    if origin is not None and origin not in ORIGINS:
        raise ValueError("origin must be one of %s, got %r" % (", ".join(ORIGINS), origin))
    ts_epoch = _to_epoch(ts_utc)
    if ts_epoch is None:
        return None
    if _surprise_gate_on():
        verdict = _surprise_verdict(conn, intent=intent, skill=skill, outcome=outcome)
        if verdict == "strengthen":
            _strengthen(conn, _normalize_intent(intent), ts_utc)
            return None
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO episodes
               (ts_utc, ts_epoch, operator, intent, intent_norm,
                skill, action, confidence, outcome, narration,
                reason, latency_ms, source, origin, origin_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts_utc, ts_epoch, operator,
                intent, _normalize_intent(intent),
                skill, action, confidence, outcome,
                narration, reason, latency_ms, source,
                origin, origin_ref,
            ),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    except sqlite3.Error as e:
        if strict:
            # the school's writers (byxin_history.record) must never lose a row to a silent None: a store that
            # cannot be written is an error, not a strengthened or duplicate episode (helper audit #5)
            raise RuntimeError("episode not written to the store: %s: %s" % (type(e).__name__, e)) from e
        return None


def backfill_from_decision_traces(
    conn: sqlite3.Connection,
    path: Path = DECISION_TRACES,
    operator: Optional[str] = None,
) -> int:
    """Walk decision_traces.jsonl and ingest each row as an episode.
    Returns count of newly inserted rows (post-dedupe).
    """
    if not path.exists():
        return 0
    inserted = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        chosen = d.get("chosen_skill", "") or ""
        # chosen_skill format: "action:skill" — split if present
        if ":" in chosen:
            _act, _skill = chosen.split(":", 1)
        else:
            _act, _skill = d.get("action") or "", chosen
        rid = ingest_episode(
            conn,
            ts_utc=d.get("timestamp", ""),
            intent=d.get("intent", ""),
            skill=_skill or None,
            action=_act or None,
            confidence=d.get("confidence"),
            reason=d.get("reason"),
            latency_ms=d.get("latency_ms"),
            operator=operator,
            source="decision_traces",
        )
        if rid is not None:
            inserted += 1
    return inserted


def backfill_from_conversation(
    conn: sqlite3.Connection,
    path: Path = CONVERSATION_MEM,
    operator: Optional[str] = None,
) -> int:
    """Pair adjacent (user, assistant) rows from conversation_memory.jsonl
    and store as episodes with narration set to the assistant's reply."""
    if not path.exists():
        return 0
    inserted = 0
    pending_user: Optional[dict] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = d.get("role")
        if role == "user":
            pending_user = d
            continue
        if role == "assistant" and pending_user is not None:
            narration = d.get("content", "")
            # Strip the action envelope if present; keep the human text
            if isinstance(narration, str) and narration.startswith("{"):
                try:
                    env = json.loads(narration)
                    narration = env.get("response", env.get("input", narration))
                except json.JSONDecodeError:
                    pass
            rid = ingest_episode(
                conn,
                ts_utc=pending_user.get("ts", d.get("ts", "")),
                intent=pending_user.get("content", ""),
                narration=narration if isinstance(narration, str) else None,
                operator=operator,
                source="conversation",
            )
            if rid is not None:
                inserted += 1
            pending_user = None
    return inserted


def backfill_from_exchanges(
    conn: sqlite3.Connection,
    path: Path = EXCHANGES,
) -> int:
    """Walk agent-hook exchanges.jsonl and store shell_exec calls as episodes."""
    if not path.exists():
        return 0
    inserted = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("kind") != "shell_exec":
            continue
        req = d.get("request") or {}
        resp = (d.get("response") or {}).get("result") or {}
        raw_intent = resp.get("raw_intent") or "{}"
        try:
            ri = json.loads(raw_intent)
        except json.JSONDecodeError:
            ri = {}
        inner = resp.get("result") or {}
        narration = ""
        if isinstance(inner, dict):
            narration = inner.get("response") or inner.get("greeting") or ""
        latency = resp.get("latency_ms")
        if isinstance(latency, dict):
            latency = latency.get("total_ms")
        rid = ingest_episode(
            conn,
            ts_utc=d.get("ts", ""),
            intent=req.get("input", ""),
            skill=ri.get("skill"),
            action=ri.get("action"),
            confidence=ri.get("confidence"),
            outcome="ok" if resp.get("ok") else (resp.get("status_label") or "error").lower(),
            narration=narration if isinstance(narration, str) else None,
            latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
            operator=d.get("session"),
            source="exchanges",
        )
        if rid is not None:
            inserted += 1
    return inserted


# ── Retrieval ────────────────────────────────────────────────────────────────

def recall(
    conn: sqlite3.Connection,
    *,
    since: str | float | None = None,
    before: str | float | None = None,
    operator: Optional[str] = None,
    skill: Optional[str] = None,
    intent_contains: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Time-keyed structured retrieval. Returns ordered-by-most-recent rows.
    All predicates are AND-combined. None of them dispatch the embedder.
    """
    clauses: list[str] = []
    params: list[Any] = []
    since_e = _to_epoch(since)
    before_e = _to_epoch(before)
    if since_e is not None:
        clauses.append("ts_epoch >= ?")
        params.append(since_e)
    if before_e is not None:
        clauses.append("ts_epoch < ?")
        params.append(before_e)
    if operator:
        clauses.append("operator = ?")
        params.append(operator)
    if skill:
        clauses.append("skill = ?")
        params.append(skill)
    if intent_contains:
        clauses.append("intent_norm LIKE ?")
        params.append(f"%{_normalize_intent(intent_contains)}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""SELECT id, ts_utc, operator, intent, skill, action,
                     confidence, outcome, narration, reason, latency_ms, source
              FROM episodes
              {where}
              ORDER BY ts_epoch DESC
              LIMIT ?"""
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Quick summary — useful for the byxin.episodic.stats RPC."""
    total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    by_source = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source, COUNT(*) FROM episodes GROUP BY source"
        )
    }
    by_skill = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT skill, COUNT(*) FROM episodes WHERE skill IS NOT NULL "
            "GROUP BY skill ORDER BY 2 DESC LIMIT 10"
        )
    }
    oldest = conn.execute(
        "SELECT MIN(ts_utc) FROM episodes"
    ).fetchone()[0]
    newest = conn.execute(
        "SELECT MAX(ts_utc) FROM episodes"
    ).fetchone()[0]
    # "ARE WE WRITING HISTORY?" -- the numbers that answer it, from the live process that does the writing.
    # A row count alone cannot: 4 status probes an hour is a pulse, not a record, and a gate that is on in
    # the record and off in the process shows up only here, where the process reports on itself.
    import time as _time
    day_ago = _time.time() - 86400.0
    writes_24h = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE ts_epoch > ?", (day_ago,)).fetchone()[0]
    sources_24h = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source, COUNT(*) FROM episodes WHERE ts_epoch > ? GROUP BY source", (day_ago,))
    }
    # The reality monitor's coverage: how many recent rows say HOW their channel knows. NULL is "did not say".
    try:
        origins_24h = {
            (row[0] or "untagged"): row[1]
            for row in conn.execute(
                "SELECT origin, COUNT(*) FROM episodes WHERE ts_epoch > ? GROUP BY origin", (day_ago,))
        }
    except sqlite3.Error:
        origins_24h = {}
    strength_rows = newest_strengthen = None
    try:
        strength_rows = conn.execute("SELECT COUNT(*) FROM episode_strength").fetchone()[0]
        newest_strengthen = conn.execute("SELECT MAX(last_seen) FROM episode_strength").fetchone()[0]
    except sqlite3.Error:
        pass
    return {
        "total_episodes": total,
        "by_source": by_source,
        "top_skills": by_skill,
        "oldest": oldest,
        "newest": newest,
        "writes_last_24h": writes_24h,
        "sources_last_24h": sources_24h,
        "origins_last_24h": origins_24h,
        "strength_rows": strength_rows,
        "newest_strengthen": newest_strengthen,
        "surprise_gate": surprise_gate_state(),
        "writer": str(Path(__file__).resolve()),
    }


def mark_consolidated(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Sleep-Consolidator hook (Phase 3): flag rows that have been replayed
    + folded into the Cerebellum's permanent store. Returns rows updated.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE episodes SET consolidated=1 WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return cur.rowcount


# ── CLI smoke ────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Hippocampus episodic memory CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the DB + schema")
    sub.add_parser("stats", help="show episode counts and recent range")

    bf = sub.add_parser("backfill", help="ingest historical data from data/*.jsonl")
    bf.add_argument("--source", choices=["all", "decisions", "conversation", "exchanges"],
                    default="all")

    rc = sub.add_parser("recall", help="time-keyed structured retrieval")
    rc.add_argument("--since")
    rc.add_argument("--before")
    rc.add_argument("--operator")
    rc.add_argument("--skill")
    rc.add_argument("--contains")
    rc.add_argument("--limit", type=int, default=10)

    args = p.parse_args(argv)
    conn = open_db()

    if args.cmd == "init":
        print(f"OK -- {DEFAULT_DB}")
        return 0

    if args.cmd == "stats":
        print(json.dumps(stats(conn), indent=2, default=str))
        return 0

    if args.cmd == "backfill":
        total = 0
        if args.source in ("all", "decisions"):
            n = backfill_from_decision_traces(conn)
            print(f"decision_traces: +{n}")
            total += n
        if args.source in ("all", "conversation"):
            n = backfill_from_conversation(conn)
            print(f"conversation_memory: +{n}")
            total += n
        if args.source in ("all", "exchanges"):
            n = backfill_from_exchanges(conn)
            print(f"exchanges: +{n}")
            total += n
        print(f"\nINSERTED total: {total}")
        return 0

    if args.cmd == "recall":
        out = recall(
            conn,
            since=args.since, before=args.before,
            operator=args.operator, skill=args.skill,
            intent_contains=args.contains, limit=args.limit,
        )
        print(json.dumps(out, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
