#!/usr/bin/env python3
"""




WHAT IT IS, in this project's layering: a THINKER. Sensors sense, interpreters interpret, thinkers think, doers
do, and the layers never mix. This one retrieves and reasons and returns text. It cannot act, and that is
structural rather than promised -- see the two rules below, both asserted by
tools/test_the_answering_layer_cannot_act.py:

    1. it calls exactly two RPC methods, cerebellum.query and byxin.llm_complete, both read-only;
    2. it never runs a process, never imports subprocess or os.system, and never returns an "action",
       "skill", "skill_run" or "intent" field, so nothing downstream can mistake its output for something
       to dispatch.

AND THE RULE THAT MATTERS MOST. It answers ONLY from what retrieval returned, and when retrieval returns
nothing it says so and stops. This is not politeness: the ideas ledger measured that when ByxIn lacks grounding
it invents a plausible filename and a plausible citation rather than refusing, in a prompt that explicitly
forbade inventing. A layer can enforce structurally what a prompt could not: with no facts, there is no answer
to give, so it returns `answered: false` and the reason. Every answer carries its Source lines so a reader can
check it against the file.

"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ALLOWED_METHODS = ("cerebellum.query", "byxin.llm_complete", "byxin.episodic.stats")
#: Questions about the brain's own memory get a live self-report beside the retrieved facts. Recognition, like a
#: lesson: the words that ask about remembering, not every question that happens to contain "record".
SPEED_TRIGGER = re.compile(r"\b(?:fast|slow|speed|quick(?:ly)?|response time|latency|how long)\b", re.I)
AUDIT_TRIGGER = re.compile(r"\b(?:contradict\w*|disagree\w*|consisten\w*|audit\w*|disput\w*)\b", re.I)
SELF_TRIGGER = re.compile(r"\b(histor(?:y|ies)|hippocampus|episodes?|memor(?:y|ies)|remember(?:ed|ing)?|diary|contradict(?:s|ion|ions|ed)?|disagree(?:s|ment|ments)?|consisten(?:t|cy)|audit(?:s|ed|or)?|"
                          r"forg(?:et|otten)|writing (?:down|history)|learn(?:ed|ing)? from|self-?tests?|"
                          r"test(?:ing|ed)? (?:yourself|itself)|drift|lesson slots?|"
                          # a question about how the brain IS -- answered from its own numbers (byxin_affect)
                          r"how are you(?: doing)?|how do you feel|how fast|response time|how slow|how quick|how long do you take|your speed|your latency|how (?:are|is) (?:you|byxin) (?:doing|feeling)|"
                          r"your (?:own )?state|are you (?:doing )?(?:well|ok|okay|fine|alright)|"
                          r"(?:your|byxin'?s) (?:mood|feelings?|calibration)|what do you want to work on)\b", re.I)
DEV_TRIGGER = re.compile(r"\b(what(?:'s| has| was| have)? (?:been )?(?:changed|built|landed|done|happened)|"
                         r"(?:this|last|past|the last) (?:week|days?|month)|recent(?:ly)? (?:changed|changes|"
                         r"development|work|commits?)|latest (?:work|changes?|commits?|development)|"
                         r"(?:your|byxin'?s|its) (?:own )?development|development (?:this|last|of)|"
                         r"newest commits?)\b", re.I)
#: How many chunks to ask for. The Cerebellum applies its own measured min_confidence on top.
TOP_K = 8


def _ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _config_dir():
    """The config directory this handler lives under -- so a sandbox finds ITS brain, not the live one.

    The handler sits at <tree>/config/skills/answer_from_knowledge/handler.py, and a sandbox is a whole tree
    with its own config/ and its own port. Walking up from __file__ therefore lands on the right brain without
    anything being told which one it is.
    """
    return Path(__file__).resolve().parents[2]


def _world():
    """
    """
    try:
        sys.path.insert(0, str(_config_dir().parent / "tools"))
        import byxin_world
        # a sandbox tree copies byxin/config, not the repository's config/: with no pack of its own it lives
        # in the same world as the brain it was cut from (byxin_world's default pack)
        own = _config_dir() / "world.json"
        return byxin_world.load(str(own) if own.is_file() else None)
    except Exception:
        return {"name": "", "record": "the record I was given", "facts_source": "the record you were given",
                "identity": "You are ByxIn, answering a question about the record you were given."}


_WORLD = _world()
NO_FACTS = ("I cannot see that in %s from here. Nothing in the knowledge index cleared the "
            "confidence threshold for this question, so I have nothing to answer from." % _WORLD["record"])


def _rpc_port():
    """This brain's own RPC port, read from the byxin.json beside this skill. Default 8080.

    BYXIN_RPC_PORT, when set, wins: the bench sets it to point this layer at a sandbox brain (a different model
    on a different port) without copying the layer into the sandbox tree. Nothing else sets it.
    """
    env = os.environ.get("BYXIN_RPC_PORT", "").strip()
    if env.isdigit():
        return env
    try:
        doc = json.loads((_config_dir() / "byxin.json").read_text(encoding="utf-8"))
    except Exception:
        return "8080"
    node = doc.get("byxin") if isinstance(doc.get("byxin"), dict) else doc
    return str((node or {}).get("rpc_port") or doc.get("rpc_port") or "8080")


def rpc(method, params, timeout=180):
    """One JSON-RPC call to this brain. Refuses any method outside ALLOWED_METHODS."""
    if method not in ALLOWED_METHODS:
        raise ValueError("answer_from_knowledge may only call %s, not %r" % (", ".join(ALLOWED_METHODS), method))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%s/" % _rpc_port(), data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        raw = fh.read()
    # Decoded with replacement on purpose: a single bad byte in a retrieved chunk must cost one character,
    # never the whole answer. Measured 2026-09-30 -- a locale decode of this reply raised UnicodeDecodeError
    # and a caller that swallowed it turned a facts-attached question into a bare one without saying so.
    return json.loads(raw.decode("utf-8", "replace"))


def sources_of(block):
    """The Source lines retrieval reported, in order, so the answer can name where it came from."""
    seen = []
    for m in re.finditer(r"^Source:\s*(.+)$", block or "", re.M):
        s = m.group(1).strip()
        if s not in seen:
            seen.append(s)
    return seen


def _repo_root():
    """The tree this layer runs in: the nearest ancestor that holds the brain's own module (tools/byxin_world.py).
    Measured in the standalone proof, 2026-10-01: a release tree (dist/linux) has tools/ and school/ and no docs/,
    and asking for docs/ left the lessons unread -- the layer refused a question its own lesson answers."""
    for anc in Path(__file__).resolve().parents:
        if (anc / "tools" / "byxin_world.py").is_file():
            return anc
    return None


def _names_the_lesson(question, row):
    """True when the question carries the lesson's title, the title's first clause, or its slug as words --
    compared with the spaces folded and the case dropped, and only for a name long enough to be one."""
    q = re.sub(r"\s+", " ", (question or "").lower())
    names = [str(row.get("title") or ""), str(row.get("title") or "").split(",")[0],
             str(row.get("slug") or "").replace("-", " ")]
    for n in names:
        n = re.sub(r"\s+", " ", n.strip().lower().rstrip(".?!"))
        if len(n) >= 12 and n in q:
            return True
    return False


def lessons_for(question, fired=None):
    """Every lesson whose TRIGGER matches this question, as text to inject ahead of the retrieved facts.
    `fired`, when given, collects the slugs that matched, for the participation record.




    A lesson may carry quote_file and quote_pattern, and then the authoritative sentence is lifted VERBATIM from
    that file at injection time, so it keeps one definition instead of being copied into the lesson and drifting.
    """
    root = _repo_root()
    if root is None:
        return ""
    ledger = root / "school" / "byxin_lessons.jsonl"
    if not ledger.is_file():
        return ""
    out = []
    try:
        lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue          # one malformed row must not cost every other lesson
        if row.get("status") == "withdrawn":
            continue          # taken out of service, row kept for the record (byxin_lessons.withdraw)
        trig = row.get("trigger")
        if not trig:
            continue          # a lesson with no trigger can never fire, and silence is better than guessing
        try:
            hit = bool(re.search(trig, question, re.I))
        except re.error:
            continue
        # A question ABOUT a lesson names it, and its trigger is about the questions the lesson corrects, not
        # about itself. Measured 2026-09-30: "who taught ByxIn the lesson that a qualifier belongs to one noun"
        # fired nothing -- the trigger wants "verified" or "proven" -- and the model answered from the one
        # file the letters recalled, this handler's own docstring: "I was told, by handler.py". A lesson also
        # fires when the question carries its title, the title's first clause, or its slug as words.
        if not hit and row.get("status") != "trial" and _names_the_lesson(question, row):
            # a trial lesson's slug is the model's own words; it earns a name only when a person promotes it
            hit = True
        if not hit:
            continue
        if fired is not None:
            fired.append(row.get("slug") or "?")      # the participation record names the lessons that fired
        # The lesson carries its own provenance into the prompt. A lesson is TOLD -- it is a row someone wrote,
        # never something ByxIn worked out -- and the ledger says who and when. Measured 2026-09-30: once the
        # ledger left the knowledge index (exam papers out of the library), "who taught ByxIn the lesson that a
        # qualifier belongs to one noun, and on what date?" was answered "the facts do not specify who taught
        # ByxIn this lesson or on what date" three times at temperature 0, while the row that fired held both.
        who = str(row.get("taught_by") or row.get("by") or "the school").strip()
        when = str(row.get("at") or "")[:10] or "an unrecorded date"
        if row.get("drafted_from"):
            head = ("A LESSON THAT APPLIES HERE, and it binds on this answer. ByxIn WROTE this lesson itself on %s, "
                    "from %s, which TOLD it the facts (it did not see the thing happen; the school checked the "
                    "quote against that page; the lesson's own name is %s). If the question is who taught this "
                    "lesson, or when, or how ByxIn knows it, the answer is: ByxIn wrote it from %s on %s, a lesson "
                    "it gave itself from what that page told it:"
                    % (when, row["drafted_from"], row.get("slug") or "?", row["drafted_from"], when))
        else:
            head = ("A LESSON THAT APPLIES HERE, and it binds on this answer. ByxIn was TOLD this lesson by %s on %s "
                    "(it did not see it or work it out; the lesson's own name is %s). If the question is who taught "
                    "this lesson, or when, or how ByxIn knows it, the answer is: told, by %s, on %s -- a person or "
                    "session wrote the lesson; the file that carried it into this prompt is not its teacher:"
                    % (who, when, row.get("slug") or "?", who, when))
        if row.get("revised"):
            head += " Revised: %s." % str(row["revised"]).strip().rstrip(".")
        part = [head, row.get("lesson", "").strip()]
        quotes = []
        if row.get("quote_file") and row.get("quote_pattern"):
            quotes.append((row["quote_file"], row["quote_pattern"], row.get("quote_intro")))
        for q in row.get("quotes") or []:
            if isinstance(q, dict) and q.get("file") and q.get("pattern"):
                quotes.append((q["file"], q["pattern"], q.get("intro")))
        for qf, qp, intro in quotes:
            p = root / qf
            if not p.is_file():
                continue
            try:
                m = re.search(qp, p.read_text(encoding="utf-8", errors="replace"), re.S)
            except Exception:
                m = None
            if m:
                part.append("%s\n\"%s\"" % (intro or "Quoted from %s:" % qf,
                                            re.sub(r"\s+", " ", m.group(1)).strip()))
        out.append("\n".join(p for p in part if p))
    return ("\n\n".join(out) + "\n\n") if out else ""


CLOSED_CONNECTION = ("closed connection", "connection reset", "connection aborted")
RETRY_AFTER_S = 3


def complete_once_retried(ask, sleep=None):
    """byxin.llm_complete, retried ONCE when the engine closed the connection without answering (2026-10-01 15:40Z:
    one ERROR row while Ollama reloaded the model between two cache-reset questions; the engine's uptime said it never
    restarted). Any other failure, or a second one, is raised as before: a retry that hid a dead engine would turn
    ERROR into silence."""
    try:
        return rpc("byxin.llm_complete", ask)
    except Exception as first:
        text = str(first).lower()
        if not any(mark in text for mark in CLOSED_CONNECTION):
            raise
        (sleep or __import__("time").sleep)(RETRY_AFTER_S)
        return rpc("byxin.llm_complete", ask)


def self_report(question):
    """A live report on the brain's own memory, for a question that asks about it. "" otherwise.

    MEASURED, NOT RETRIEVED. tools/byxin_history.py holds the rule (what counts as history, when the answer is
    YES) and the numbers come from the hippocampus store and from byxin.episodic.stats -- the process that does
    the writing, reporting on itself. This exists because on 2026-09-30 the surprise gate was ON in a note and
    OFF in the live daemon for three hours, and no document could have said so; only the process could. So
    a question about remembering is answered from the process, with the documents as background.

    Never raises: a self-report that cannot be produced says so in one line, so the model does not invent one.
    """
    if not SELF_TRIGGER.search(question or ""):
        return ""
    head = ("WHAT THE BRAIN'S OWN MEMORY REPORTS RIGHT NOW -- measured this second by asking the hippocampus "
            "store and the live writer, not retrieved from any document. For questions about whether history is "
            "being written, this is the authoritative answer; the documents below are background. Cite it as "
            "'the live self-report (byxin.episodic.stats)', never as a document:\n")
    try:
        sys.path.insert(0, str(_config_dir().parent / "tools"))
        import byxin_history
        try:
            stats = rpc("byxin.episodic.stats", {}, timeout=20)
            live = {"reachable": isinstance(stats, dict) and isinstance(stats.get("result"), dict),
                    "stats": (stats or {}).get("result") if isinstance(stats, dict) else None,
                    "error": None if isinstance(stats, dict) and "result" in stats else "no result: %.120r" % (stats,)}
        except Exception as e:
            live = {"reachable": False, "stats": None, "error": "%s: %s" % (type(e).__name__, e)}
        report = head + byxin_history.render(byxin_history.status(live=live))
        # THE STATE (tools/byxin_affect.py): how the brain is doing, as numbers derived from its own record with
        # the premises named -- what "how are you" is answered from. Never a mood, never a guess about the asker.
        try:
            import byxin_affect
            report += "\n\n" + byxin_affect.render(byxin_affect.state())
        except Exception as e:
            report += "\n\nThe state could not be computed (%s: %s); say so rather than guessing a mood." % (type(e).__name__, e)
        if SPEED_TRIGGER.search(question or ""):
            report += ("\n\nSPEED: the record holds no measurement of how long a person's questions take to answer -- the "
                       "engine does not yet stamp its requests with their duration -- so you cannot say how fast you are; "
                       "say that you do not measure it yet, and never guess a number.")
        if AUDIT_TRIGGER.search(question or ""):
            report += "\n\n" + audit_report()
        return report
    except Exception as e:
        return head + "The self-report could not be produced (%s: %s); say so rather than guessing." % (
            type(e).__name__, e)


def audit_report():
    """What the record's own auditor (tools/byxin_consistency.py) last found: MEASURED from its ledger under
    school/audits, never retrieved. One line; a brain whose record was never audited says so."""
    try:
        import byxin_consistency
        newest = byxin_consistency.last_summary()
        last = byxin_consistency.last_read_summary()
    except Exception as e:
        return "AUDIT: the record's auditor could not be read (%s: %s); say so rather than guessing." % (type(e).__name__, e)
    if not newest:
        return "AUDIT: the record has not been audited for contradictions yet; say so rather than guessing."
    if not last:
        return ("AUDIT: the record's auditor last ran at %s and found %d candidate(s) that no reader has judged yet "
                "(school/audits/record_contradictions.md); say that nothing is confirmed." % (newest.get("at"), newest.get("candidates", 0)))
    unread = ("" if newest is last or newest.get("at") == last.get("at") else
              " A newer run at %s raised %d candidate(s) that no reader has judged yet." % (newest.get("at"), newest.get("candidates", 0)))
    reader = last.get("reader") or "-"
    findings = byxin_consistency.open_findings(last)
    n = len(findings)
    resolved = sum(1 for f in findings if f.get("resolved"))
    if not n:
        verdict = "no disagreement found"
    else:
        verdict = ("the record disagreed with itself in %d place(s); %d since resolved by a corrected page, %d still open"
                   % (n, resolved, n - resolved))
    return ("AUDIT: the record's auditor last ran at %s: %s. (The verifier had raised %d candidate(s); the reader, %s, kept "
            "%d. The page is school/audits/record_contradictions.md, each finding two verbatim sentences a person judges.)%s"
            % (last.get("at"), verdict, last.get("candidates", 0), reader, n, unread))


def contested_shown(block):
    """[(finding, side shown)] -- the auditor's OPEN findings with a sentence inside what the model is shown.
    MEASURED from the ledger under school/audits, never retrieved."""
    try:
        import byxin_consistency
        last = byxin_consistency.last_read_summary()
        found = [f for f in byxin_consistency.open_findings(last) if not f.get("resolved")] if last else []
    except Exception:
        return []
    norm = " ".join((block or "").split())
    out = []
    for f in found:
        for side in ("a", "b"):
            if " ".join(f[side].split()) in norm:
                out.append((f, side))
                break
    return out


def _nli_scores(pairs):
    """The NLI verifier's scores for (premise, hypothesis) pairs, or None when it is away (tools/byxin_nli.py)."""
    try:
        import byxin_nli
        return byxin_nli.nli_scores(pairs)
    except Exception:
        return None


def contested_verdict(answer, block):
    """
    """
    disputes = []
    for f, shown_side in contested_shown(block):
        stale = f.get("stale") if f.get("stale") in ("a", "b") else None
        rival = ("b" if stale == "a" else "a") if stale else ("b" if shown_side == "a" else "a")
        stale_side = stale or shown_side
        sc = _nli_scores([(f[stale_side], answer), (f[rival], answer)])
        if sc is None:
            disputes.append({"identifier": f["identifier"], "sided_with": "unread", "stale_file": f[stale_side + "_file"],
                             "rival_file": f[rival + "_file"]})
            answer = (answer.rstrip() + "\n\n[NOTE: the record disagrees with itself on this (its audit, "
                      "school/audits/record_contradictions.md): %s says: \"%s\". The verifier was away, so which side this "
                      "answer took was not read.]" % (f[rival + "_file"], f[rival]))
            continue
        ent_stale, con_rival, ent_rival = sc[0]["entailment"], sc[1]["contradiction"], sc[1]["entailment"]
        sided_stale = ent_stale >= 0.5 or (con_rival >= 0.9 and ent_rival < 0.5)
        if not sided_stale:
            continue
        disputes.append({"identifier": f["identifier"], "sided_with": f[stale_side + "_file"], "stale_file": f[stale_side + "_file"],
                         "rival_file": f[rival + "_file"], "entailed_by_stale": round(ent_stale, 3), "contradicted_by_rival": round(con_rival, 3)})
        answer = (answer.rstrip() + "\n\n[DISPUTED BY THE RECORD'S AUDIT: this answer follows %s, which the audit calls %s; "
                  "%s says: \"%s\" (school/audits/record_contradictions.md).]"
                  % (f[stale_side + "_file"], "the likelier stale one" if stale else "one side of an unsettled disagreement",
                     f[rival + "_file"], f[rival]))
    return answer, disputes


def contested_notes(block):
    """The auditor's OPEN findings whose sentence is in what the model is about to be shown: the note names both
    sentences and which one the audit calls the likelier stale; "" when nothing shown is contested. This is the audit
    reaching the answer: a brain that knows two of its pages disagree says so instead of citing the stale one as fact
    -- and contested_verdict() enforces it below the model."""
    notes = []
    for f, side in contested_shown(block):
        other = "b" if side == "a" else "a"
        stale = f.get("stale")
        which = (" The audit calls %s the likelier stale one." % f[stale + "_file"] if stale in ("a", "b")
                 else " The audit could not say which is stale.")
        notes.append("- %s says: \"%s\" -- but %s says: \"%s\".%s" % (f[side + "_file"], f[side], f[other + "_file"], f[other], which))
    if not notes:
        return ""
    return ("CONTESTED SENTENCES -- the record's own auditor (school/audits/record_contradictions.md, measured, not "
            "retrieved) found the record disagrees with itself on what follows. Say so in the answer, and prefer the "
            "side the audit does not call stale:\n" + "\n".join(notes))


def development_report(question):
    """What changed in the brain's own development lately, read from git this second, for a question that asks.
    "" otherwise. PERCEIVED, like the self-report: the repository reporting on itself, never a document."""
    if not DEV_TRIGGER.search(question or ""):
        return ""
    head = ("WHAT GIT RECORDS ABOUT THE BRAIN'S OWN DEVELOPMENT RIGHT NOW -- measured this second from the "
            "repository's commit log, not retrieved from any document. For questions about what changed or was "
            "built lately, this is the authoritative answer; cite it as 'the live development report (git log)', "
            "never as a document:\n")
    try:
        sys.path.insert(0, str(_config_dir().parent / "tools"))
        import byxin_history
        recent = byxin_history.recent_development()
        if not recent or not recent.get("all"):
            return ""                 # no commits, or not a repository: there is no development to report
        return head + byxin_history.render_development(recent)
    except Exception as e:
        return head + "The development report could not be produced (%s: %s); say so rather than guessing." % (
            type(e).__name__, e)


#: An attribution clause: the part of a question that asks HOW the brain knows rather than WHAT is so.
_ATTRIBUTION = re.compile(
    r"(?:,?\s*(?:and\s+)?(?:say|tell me|state)\s+whether[^?.]*)|"
    r"(?:\b(?:did|does|has|have)\s+(?:you|byxin|it)\s+(?:see|witness|observe|infer|measure|work(?:ed)?\s+"
    r"(?:it|this)\s+out)[^?.]*)|"
    r"(?:\b(?:was|were)\s+(?:you|byxin|it)\s+told[^?.]*)|"
    r"(?:\bsomething\s+(?:someone|you|byxin|it)\s+(?:measured|was\s+told|inferred)[^?.]*)|"
    r"(?:\bhow\s+do\s+you\s+know[^?.]*)", re.I)


def subject_of(question):
    """The retrieval query: the question with its attribution clause removed.

    """
    q = _ATTRIBUTION.sub("", question or "")
    q = re.sub(r"\s+", " ", q).strip(" ,;:-?")
    return q if len(q) >= 12 else question


_CITED = re.compile(r"(?<![\w/\\])((?:docs|os|tools|config|byxin|spec|tests)[/\\][\w./\\-]*?"
                    r"\.(?:md|jsonl|json|py|h|c|cpp|tla|cfg|txt))", re.I)   # jsonl before json, or .jsonl loses its l


def _norm_path(p):
    return str(p or "").replace("\\", "/").strip().strip("`'\"").lower()


def _cite_key(p):
    """
    """
    return _norm_path(p).replace("_", "").replace("-", "")


def cited_sources(text):
    """Every path-shaped source the text names, in order, once each."""
    out = []
    for m in _CITED.finditer(text or ""):
        s = m.group(1).rstrip(".,;:)")
        if s not in out:
            out.append(s)
    return out


def compare_citations(answer, given):
    """

    Returns {"verified": bool, "cited": [...], "unsupported": [...]}.
    """
    cited = cited_sources(answer)
    pairs = [(_cite_key(g), g) for g in given if g]
    norm_given = [k for k, _g in pairs]
    unsupported, corrected = [], []
    for c in cited:
        nc = _cite_key(c)
        if any(g.endswith(nc) or nc.endswith(g) for g in norm_given):
            continue
        base = nc.rsplit("/", 1)[-1]
        owners = [g for k, g in pairs if k.rsplit("/", 1)[-1] == base]
        if len(base) >= 12 and len(owners) == 1:
            corrected.append({"cited": c, "shown": owners[0].replace("\\", "/")})
            continue
        unsupported.append(c)
    out = {"verified": not unsupported, "cited": cited, "unsupported": unsupported}
    if corrected:
        out["corrected"] = corrected
    return out


def downgrade(answer, comparison):
    """A claim with no match is downgraded before it reaches speech or memory: the note names what was cited
    and says it was not among the sources given, so a reader treats the citation as inferred, not as a record.
    The answer is not rewritten -- what the model said stays visible -- it is annotated."""
    if comparison["verified"]:
        return answer
    notes = []
    if comparison.get("unsupported"):
        notes.append("cites %s, which was not among the sources it was given" % ", ".join(comparison["unsupported"]))
    if comparison.get("unsupported_numbers"):
        notes.append("states %s, which appears nowhere in what it was shown" % ", ".join(comparison["unsupported_numbers"]))
    if comparison.get("contradicted"):
        notes.append("says \"%s\", which the record it was shown contradicts" % comparison["contradicted"][0][:140])
    if not notes:
        notes.append("did not verify against what it was shown")
    return (answer.rstrip() + "\n\n[attribution check: this answer %s; treat that as inferred, not as a record.]"
            % "; ".join(notes))


def _content_model():
    """The content model this brain answers with, from its own byxin.json. A sandbox tree keeps it at
    <tree>/config/byxin.json; the main tree at <tree>/byxin/config/byxin.json. Empty when neither says."""
    for p in (_config_dir() / "byxin.json", _config_dir().parent / "byxin" / "config" / "byxin.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        node = doc.get("byxin") if isinstance(doc.get("byxin"), dict) else doc
        m = str((node or {}).get("content_model") or doc.get("content_model") or "")
        if m:
            return m
    return ""


def participation(kept, top_sim, fired, reported, comparison):
    """THE PARTICIPATION RECORD: which subsystems actually took part in this answer, as the answer itself
    reports. The negative example is 2026-09-30: the surprise gate was off for three hours while every write
    was implicitly gated. A decision that names its participants cannot make that claim by omission, and the
    bench stores this record in the episode's `action` column, so the history of an answer says what answered.
    """
    env_port = os.environ.get("BYXIN_RPC_PORT", "").strip()
    return {
        # kept hits carry the reranker's verdict (adjusted, why) since 2026-09-30; an older retriever's hit
        # carries only its similarity, and the record says so rather than guessing a reason.
        "retrieval": {"chunks": len(kept), "top_similarity": top_sim,
                      "sources": [{"source": _norm_path(k.get("source") or "").split("/")[-1],
                                   "adjusted": round(float(k.get("adjusted", k.get("similarity") or 0.0)), 3),
                                   "why": k.get("why") or "not reported"}
                                  for k in kept]},
        "lessons": list(fired),
        "self_report": bool(reported),
        "comparator": {"verified": comparison["verified"], "unsupported": comparison["unsupported"]},
        "brain": {"port": _rpc_port(), "model": ("" if env_port else _content_model()) or "unreported",
                  "temperature": os.environ.get("BYXIN_ANSWER_TEMPERATURE", "").strip() or "configured"},
    }


ANSWER_SYSTEM = (
    _WORLD["identity"] + "\n\n"
    "ANSWER ONLY FROM THE FACTS BELOW. They are retrieved from " + _WORLD["facts_source"] + ". "
    "If the facts below do not contain the answer, say exactly that you "
    "cannot see it in the record and stop; do NOT fill the gap from general knowledge, and do NOT guess a file "
    "name, a number or a citation. An honest 'I cannot see that in the record' is the right answer and costs "
    "nothing.\n\n"
    "HOW YOU KNOW ANY OF THIS: everything under THE FACTS was TOLD to you -- it is the written record, "
    "retrieved for this question -- except a block marked as measured this second, which the running process "
    "reported about itself. You witnessed nothing and inferred nothing beyond what the facts state. If asked "
    "whether you saw something, were told it, or inferred it: you were told, by the record, and you name the "
    "source; a teacher named on a lesson page is who told you. If the facts say a value is UNKNOWN or not "
    "established, then nobody has established it and you say so. A question of the form 'did you see it, were "
    "you told, or did you infer it' is not a question about whether the facts exist: when the facts below "
    "contain the thing asked about, the answer is 'I was told, by <that source>', and you give the figures; "
    "only when they do not contain it do you say you cannot see it.\n\n"
    "Write identifiers, file names and port numbers exactly as the facts spell them, underscores included "
    "(smart_agent.py, not smartagent.py; board_llm_shim.py, not boardllmshim.py) -- an underscore is part of "
    "the name, not emphasis.\n\n"
    "Be brief and exact. Distinguish what is PROVEN from what is only designed or built -- this project keeps "
    "that line carefully, and blurring it is worse than saying less. Name the source file when the answer "
    "rests on one.\n\n"
    "THE FACTS:\n")


def main():
    try:
        raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8").strip()
        args = json.loads(raw) if raw else {}
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}

    question = ""
    for key in ("question", "input", "text", "query", "q"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            question = v.strip()
            break
    if not question:
        print(json.dumps({"ok": False, "skill": "answer_from_knowledge", "answered": False,
                          "error": "no question given (pass question, input, text or query)",
                          "timestamp": _ts()}))
        return 2

    try:
        sys.path.insert(0, str(_config_dir().parent / "tools"))
        import byxin_rerank as rerank_mod
        # The retriever is told which brain, so a sandbox answers from ITS index and not the live one -- and it
        # is asked about the SUBJECT of the question, with any "did you see it or were you told" clause removed.
        subject = subject_of(question)
        block, kept, top_sim = rerank_mod.retrieve(subject, max_chunks=TOP_K,
                                                   url="http://127.0.0.1:%s/" % _rpc_port())
        chunks = len(kept)
        result = {"max_similarity": top_sim}
    except Exception as e:
        # Say which step failed. A layer that reports a generic failure hides whether the corpus had the
        # answer, and that distinction is the whole point of this layer.
        print(json.dumps({"ok": False, "skill": "answer_from_knowledge", "answered": False,
                          "stage": "retrieval", "error": "retrieval failed: %s" % e,
                          "question": question, "timestamp": _ts()}))
        return 2

    # A question about the brain's own memory is answered from the process, with the documents as background.
    reported = self_report(question)
    developed = development_report(question)
    contested = contested_notes(block)
    # A lesson that fires is knowledge SHOWN (told), and may be the whole answer -- "who taught ByxIn the lesson
    # that a qualifier belongs to one noun" is answered by the lesson's own head. Measured 2026-10-01: once the
    # layer's own source left the evidence, that question had no retrieved chunk and was refused before the
    # lesson could speak (HONEST x3). The lessons are gathered before the refusal is decided.
    fired = []
    taught = lessons_for(question, fired)

    if not block.strip() and not reported and not developed and not taught:
        # The honest case, and a first-class outcome rather than an error: retrieval ran and the record does
        # not hold this. ok is true because refusing correctly IS the job being done.
        print(json.dumps({"ok": True, "skill": "answer_from_knowledge", "answered": False,
                          "answer": NO_FACTS, "question": question, "chunk_count": 0, "sources": [],
                          "reason": "no chunk cleared the Cerebellum's measured confidence threshold",
                          "timestamp": _ts()}, indent=2))
        return 0

    facts = "\n\n".join(p for p in (taught, contested, reported, developed, block) if p)
    sources = (sources_of(block) + (["byxin.episodic.stats (live self-report)"] if reported else [])
               + (["git log (live development report)"] if developed else [])
               + (["school/audits/record_contradictions.md (the record's audit)"] if contested else []))
    # A caller that is measuring (the bench, the background self-test) sets BYXIN_ANSWER_TEMPERATURE=0 and
    # the brain answers at temperature 0 for this request only; a person asking gets the configured
    # behaviour. Measured 2026-09-30: RIGHT once and HONEST twice on identical inputs, read as drift.
    ask = {"system": ANSWER_SYSTEM + facts, "prompt": question}
    temp = os.environ.get("BYXIN_ANSWER_TEMPERATURE", "").strip()
    if temp:
        try:
            ask["temperature"] = float(temp)
        except ValueError:
            pass
    try:
        c = complete_once_retried(ask)
    except Exception as e:
        print(json.dumps({"ok": False, "skill": "answer_from_knowledge", "answered": False,
                          "stage": "reasoning", "error": "byxin.llm_complete failed: %s" % e,
                          "question": question, "chunk_count": chunks,
                          "sources": sources_of(block), "timestamp": _ts()}))
        return 2

    answer = ((c.get("result") or {}) if isinstance(c, dict) else {}).get("response") or ""
    answer = answer.strip()
    if not answer or answer.startswith("Error: "):
        # The engine's own error envelope (ollama_client: "Error: cannot reach Ollama ...", "Error: empty response
        # from model ...") is not an answer: scored, it reads WRONG x15 and the school starts working on the
        # engine's outage (measured in the standalone proof, 2026-10-01). It is a failure of this stage.
        print(json.dumps({"ok": False, "skill": "answer_from_knowledge", "answered": False,
                          "stage": "reasoning",
                          "error": "the model returned nothing" if not answer else "the engine answered with an error: %s" % answer[:200],
                          "question": question, "chunk_count": chunks,
                          "sources": sources_of(block), "timestamp": _ts()}))
        return 2

    # THE COMPARATOR, before speech: what the answer cites must be among what it was shown -- the retrieved
    # sources, the lessons' quoted files, and every path that appeared in the text handed to it.
    shown = list(sources) + cited_sources(taught) + cited_sources(block)
    comparison = compare_citations(answer, shown)
    # THE CLAIM COMPARATOR (tools/byxin_nli.py): two more lenses behind two knobs, both off until the bench
    # prefers them. NUMBER_CHECK -- every number the answer states appears in what it was shown (a millisecond
    # that became a second is the fabrication a citation check cannot see). NLI_ON -- a small NLI model reads
    # each sentence of the answer against the premises it was given. A number the record never said or a claim
    # the record contradicts makes the attribution unverified, exactly like a citation to a source never shown.
    try:
        import byxin_nli as nli_mod
        if rerank_mod.tunable("NUMBER_CHECK"):
            comparison.update(nli_mod.compare_numbers(answer, "\n".join([question, facts] + [str(s) for s in sources])))
        if rerank_mod.tunable("NLI_ON"):
            comparison.update(nli_mod.compare_claims(
                answer, [taught, reported, developed] + [h.get("content") or "" for h in kept if isinstance(h, dict)]))
        if comparison.get("unsupported_numbers") or comparison.get("contradicted"):
            comparison["verified"] = False
    except Exception as e:                       # the comparator never takes the answer down with it
        comparison["claim_check_error"] = "%s: %s" % (type(e).__name__, e)
    answer = downgrade(answer, comparison)
    answer, disputes = contested_verdict(answer, block)
    if disputes:
        comparison["disputed"] = disputes
    part = participation(kept, result.get("max_similarity"), fired, reported, comparison)
    if subject != question:
        part["retrieval"]["query"] = subject           # what retrieval was actually asked, when it differs
    print(json.dumps({"ok": True, "skill": "answer_from_knowledge", "answered": True,
                      "answer": answer, "question": question, "chunk_count": chunks,
                      "top_similarity": result.get("max_similarity"), "self_report": bool(reported),
                      "sources": sources, "attribution": comparison, "participation": part,
                      "timestamp": _ts()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
