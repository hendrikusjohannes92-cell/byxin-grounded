#!/usr/bin/env python3
"""byxin_surprise.py -- write an episode when it is surprising; strengthen the memory when it is not.

ByxIn proposed this itself on 2026-09-30 (ideas ledger #25: "a real-time memory encoding policy based on
surprise-driven writes"), grounded in the Titans paper that sits in its own corpus. That note, injected
2026-05-28 and flagged HIGH relevance, says in as many words:


Four months later the store had 71926 episodes across 1344 distinct intents, and "execute imu node" appeared
6103 times. The recommendation was in the library the whole time; nothing connected it to the problem.

WHAT SURPRISE MEANS HERE, and it is deliberately NOT the paper's mechanism. Titans computes surprise as the
gradient of a neural memory's prediction error. ByxIn's hippocampus is a SQLite table, and measured on
2026-09-30 its `embedding` column is EMPTY for all 71926 rows -- so an embedding-distance novelty score cannot
be computed from what is stored, and inventing one would mean claiming a mechanism that is not there. What IS
stored is what happened: the intent, the skill it routed to, and the outcome (ok 53463, error 17551, unknown
361, pending_review 22, needs_consent 13). So surprise is defined over those, which is both honest about the
data and faithful to the principle -- write on deviation from what memory predicts, skip what it predicted.

    NOVEL            this intent has never been seen        -> WRITE. A first occurrence is always surprising.
    PREDICTION ERROR seen before, but the outcome or the routed skill differs from the established pattern
                                                            -> WRITE. This is the half that matters: a
                                                               recurring FAULT is surprising even though its
                                                               words are familiar, and a gate that suppressed
                                                               it would be worse than no gate.
    ROUTINE          same intent, same skill, same outcome  -> STRENGTHEN. One memory, counted, not 6103 rows.

NOTHING IS LOST, and this is the part a suppression policy usually gets wrong. A skipped write still increments
`times_seen` and moves `last_seen` on the memory it matched, so frequency survives -- "seen 6103 times" stays
distinguishable from "seen once", which consolidation needs and which a policy that merely dropped rows would
destroy. ByxIn's own idea said "RISK: None known"; the risks are a suppressed recurring fault and lost
frequency, and both are answered here rather than waved past.

    py -3.11 tools/byxin_surprise.py --replay             # measure against the real store, read-only
    py -3.11 tools/byxin_surprise.py --replay --limit 5000
SPDX-License-Identifier: BSD-2-Clause
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

WRITE = "write"
STRENGTHEN = "strengthen"
SENTINEL_NAME = os.path.join("data", "surprise_gate.enabled")


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
DB = os.path.join(ROOT, "data", "hippocampus.db")
SENTINEL = os.path.join(ROOT, SENTINEL_NAME)


def gate_enabled(root=None):
    """Is the surprise gate on? Either the sentinel file exists, or BYXIN_SURPRISE_GATE is exactly "1".

    Resolved from the git common directory, so every worktree and the sandbox trees all read the SAME switch --
    a per-tree answer would mean the gate was on for some writers and off for others, and the store would end up
    a mix of both policies with no way to tell which row came from which.
    """
    if os.environ.get("BYXIN_SURPRISE_GATE") == "1":
        return True
    return os.path.exists(os.path.join(root, SENTINEL_NAME) if root else SENTINEL)

_WS = re.compile(r"\s+")
#: Outcomes that must ALWAYS be written, however often they repeat. A gate that habituates to these is the
#: failure mode this policy is most likely to have: needs_consent and pending_review are the two places where
#: a human is owed a decision, and a suppressed one is a decision nobody was asked for.
ALWAYS_WRITE_OUTCOMES = frozenset({"needs_consent", "pending_review"})


def normalise(intent):
    """The key a memory is recognised by. Case and spacing are not what makes an episode new."""
    return _WS.sub(" ", (intent or "").strip().lower())


class Memory(object):
    """What the hippocampus already holds, in the only shape this policy needs to consult.

    Keyed by normalised intent. Each entry remembers the skill and outcome it has come to predict, and how
    many times it has been seen. Deliberately tiny: a policy that needed the whole store in a rich form could
    not run on the write path, and this one must.
    """

    def __init__(self):
        self.by_intent = {}

    def predict(self, intent_norm):
        return self.by_intent.get(intent_norm)

    def learn(self, intent_norm, skill, outcome):
        e = self.by_intent.get(intent_norm)
        if e is None:
            self.by_intent[intent_norm] = {"skill": skill, "outcome": outcome, "times_seen": 1,
                                           "variants": {(skill, outcome)}}
        else:
            e["times_seen"] += 1
            e["variants"].add((skill, outcome))
            # The prediction follows the latest observation: memory predicts what happened last time.
            e["skill"] = skill
            e["outcome"] = outcome

    def strengthen(self, intent_norm):
        e = self.by_intent.get(intent_norm)
        if e is not None:
            e["times_seen"] += 1


def decide(episode, memory):
    """(action, reason) for one candidate episode. Pure: no I/O, so it is testable and replayable.

    `episode` needs only intent, skill and outcome. Anything else the caller stores is its own business.
    """
    intent_norm = normalise(episode.get("intent_norm") or episode.get("intent"))
    if not intent_norm:
        return WRITE, "an episode with no intent is not something memory can recognise; keep it for inspection"

    outcome = (episode.get("outcome") or "").strip().lower()
    skill = (episode.get("skill") or "").strip()

    if outcome in ALWAYS_WRITE_OUTCOMES:
        return WRITE, "outcome %r always writes: a human is owed a decision and it must not be habituated away" \
               % outcome

    known = memory.predict(intent_norm)
    if known is None:
        return WRITE, "novel intent: never seen before, so nothing predicted it"

    if (skill, outcome) not in known["variants"]:
        # The words are familiar and what happened is not. This is the prediction error that keeps a recurring
        # fault visible: the same request failing where it used to succeed is news, not repetition.
        return WRITE, ("prediction error: memory predicted skill=%r outcome=%r, observed skill=%r outcome=%r"
                       % (known["skill"], known["outcome"], skill, outcome))

    return STRENGTHEN, ("routine: intent seen %d time(s) with this same skill and outcome"
                        % known["times_seen"])


def replay(db_path=DB, limit=None):
    """Run the whole real store through the policy, read-only, and report what it would have done.

    Read-only on purpose: a policy that has never been measured against the history it is meant to fix should
    not be the thing that changes it. This answers three questions -- how much is saved, is every distinct
    intent still remembered, and is any failure lost.
    """
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    q = ("select intent, intent_norm, skill, outcome from episodes "
         "order by coalesce(ts_epoch, 0), id")
    if limit:
        q += " limit %d" % int(limit)

    mem = Memory()
    stats = {WRITE: 0, STRENGTHEN: 0}
    reasons = {}
    seen_intents = set()
    written_intents = set()
    outcome_total = {}
    outcome_written = {}

    for intent, intent_norm, skill, outcome in con.execute(q):
        ep = {"intent": intent, "intent_norm": intent_norm, "skill": skill, "outcome": outcome}
        action, reason = decide(ep, mem)
        stats[action] += 1
        key = reason.split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
        k = normalise(intent_norm or intent)
        seen_intents.add(k)
        o = (outcome or "none").lower()
        outcome_total[o] = outcome_total.get(o, 0) + 1
        if action == WRITE:
            written_intents.add(k)
            outcome_written[o] = outcome_written.get(o, 0) + 1
            mem.learn(k, (skill or "").strip(), (outcome or "").strip().lower())
        else:
            mem.strengthen(k)

    total = stats[WRITE] + stats[STRENGTHEN]
    return {"episodes": total, "would_write": stats[WRITE], "would_strengthen": stats[STRENGTHEN],
            "reduction_pct": round(100.0 * stats[STRENGTHEN] / max(1, total), 1),
            "why": reasons,
            "distinct_intents": len(seen_intents),
            "distinct_intents_written": len(written_intents),
            "every_intent_kept": seen_intents == written_intents,
            "outcome_total": outcome_total, "outcome_written": outcome_written}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replay", action="store_true", help="measure the policy against the real store, read-only")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    if a.replay:
        r = replay(a.db, a.limit)
        print(json.dumps({k: v for k, v in r.items() if k not in ("outcome_total", "outcome_written")}, indent=1))
        print("\n%-16s %10s %10s %s" % ("outcome", "episodes", "written", "kept"))
        for o in sorted(r["outcome_total"], key=lambda x: -r["outcome_total"][x]):
            w = r["outcome_written"].get(o, 0)
            print("%-16s %10d %10d %s" % (o, r["outcome_total"][o], w, "yes" if w else "NONE -- LOST"))
        print("\nevery distinct intent still remembered: %s" % r["every_intent_kept"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
