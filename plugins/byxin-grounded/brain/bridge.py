#!/usr/bin/env python3
"""bridge.py -- ByxIn's answering layer, split around the content model so Claude Code's own model is that model,
and the SHARED BRAIN every Claude Code session in one project reads and writes.

config/skills/answer_from_knowledge/handler.py runs retrieve -> lessons -> self-report -> LLM -> comparator in one
process against an Ollama-served model. Inside Claude Code the model is the session's, so this bridge exposes the
two halves the handler composes, unchanged, by importing its functions:

  prepare  {question, root}           -> the system block (identity + lessons + facts), sources, participation seed
  check    {answer, question, state}  -> the comparator's verdict: citations, numbers, (claims), contested notes
  <op>     {...}                      -> thin wrappers over the other layers (state, lessons, history, mind)



What a session did is PERCEIVED by the mod (it watched the turn happen); a note is TOLD (a person said it). Neither is
a fact about the code, and the block a session receives says so.

JSON in on stdin, JSON out on stdout, always exit 0 with {"ok": false, "error": ...} on failure: a bridge that
dies silently would read as 'nothing found', the exact confusion lesson an-empty-result-is-not-a-negative-result forbids.
SPDX-License-Identifier: BSD-2-Clause
"""
import hashlib, importlib.util, json, os, random, re, socket, subprocess, sys, tempfile, time, traceback

MIN_TERMS_ANCHORED, MIN_TERMS_PLAIN = 1, 2   # distinct RARE question terms a lexical hit must carry
DF_MAX = 0.02       # a term is vocabulary of the record only if it is in at most this share of the chunks
COVERAGE_ANCHORED = 0.5   # a question that names an identifier or path needs less of its words covered
COVERAGE = 0.6   # fraction of the question's IDF mass a lexical hit must cover; a person moves it in config/brain_tunables.json "COVERAGE"
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: what makes a directory a ByxIn brain tree
MARKERS = (("tools", "byxin_rerank.py"), ("tools", "byxin_history.py"),
           ("config", "skills", "answer_from_knowledge", "handler.py"))
#: a session is live while its presence beat is younger than this; a session elsewhere while its last event is
LIVE_S, ELSEWHERE_S = 180, 20 * 60
#: how often a beat syncs with the shared branch (each turn, note, start and end syncs anyway)
SYNC_EVERY_S = 120
#: the plumbing commits on the shared branch carry this identity, so nobody mistakes them for a person's
BRAIN_IDENT = {"GIT_AUTHOR_NAME": "ByxIn shared brain", "GIT_AUTHOR_EMAIL": "byxin@localhost",
               "GIT_COMMITTER_NAME": "ByxIn shared brain", "GIT_COMMITTER_EMAIL": "byxin@localhost"}
#: The envelopes Claude Code wraps around a prompt no person typed: a background task's notification, a reminder, a CI
#: event, a local command's echo. A turn that opens with one is no one asking (lesson a-status-line-is-not-someone-
#: asking): measured 2026-10-04, a session watching a long-running job recorded every monitor event as "asked: <task-
#: notification>". hooks/register.tsx keeps the same list and reads the engine's own origin of the prompt first.
ENVELOPES = ("task-notification", "system-reminder", "ci-monitor-event", "local-command-caveat", "local-command-stdout",
             "command-name", "bash-input", "bash-stdout", "bash-stderr",
             "agent-message")
_ENVELOPE = re.compile(r"\s*<(%s)[\s>]" % "|".join(re.escape(e) for e in ENVELOPES))
CTX = {}


def envelope_of(text):
    """The envelope a prompt opens with, or None when a person may have typed it."""
    m = _ENVELOPE.match(text or "")
    return m.group(1) if m else None


# ── git, without a window and without touching anyone's index ────────────────────────────────────────────────

def _git(args, cwd, env=None, check=True, timeout=60, input=None):
    # BYTES on both pipes. A text-mode pipe on Windows writes every "\n" as "\r\n": measured 2026-10-03, the paths
    # fed to hash-object --stdin-paths and update-index --index-info arrived with a carriage return each, nothing was
    # added, and every shared-branch commit carried the empty tree while the sync reported success.
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                       input=input.encode("utf-8") if input is not None else None,
                       stdin=None if input is not None else subprocess.DEVNULL,
                       timeout=timeout, creationflags=_NO_WINDOW, env=dict(os.environ, **(env or {})))
    out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args[:2]), (err or out).strip()[:300]))
    return out


def common_dir(start):
    """The repository's shared .git: the same answer from every worktree. None outside git."""
    try:
        out = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], start, timeout=20).strip()
    except Exception:
        return None
    return os.path.abspath(out) if out else None


# ── which project, which brain, which hub ──────────────────────────────────────────────────────────────────

def is_brain_tree(p):
    return bool(p) and all(os.path.isfile(os.path.join(p, *m)) for m in MARKERS)


def project_of(a):
    """(project, common_dir): the main working tree of the repository the session is in, else the directory given."""
    given = os.path.abspath(a.get("project") or a.get("root") or os.getcwd())
    cd = common_dir(given)
    if cd and os.path.basename(cd).lower() == ".git":
        return os.path.dirname(cd), cd
    return given, cd


def brain_tree(project):
    env = os.environ.get("BYXIN_ROOT", "").strip()
    if env and is_brain_tree(env):
        return os.path.abspath(env), "named by BYXIN_ROOT"
    if is_brain_tree(project):
        return project, "the project is a ByxIn tree: its own brain serves it"
    return HERE, "the brain vendored with the mod (the project is not a ByxIn tree)"


def hub_dir(project, cd):
    env = os.environ.get("BYXIN_HUB", "").strip()
    if env:
        return os.path.abspath(env)
    if cd:
        return os.path.join(cd, "byxin-brain")
    slug = "%s-%s" % (os.path.basename(project.rstrip("\\/")) or "root",
                      hashlib.sha1(project.lower().encode("utf-8")).hexdigest()[:10])
    return os.path.join(os.path.expanduser("~"), ".byxin", "projects", slug)


def _handler(tree):
    p = os.path.join(tree, "config", "skills", "answer_from_knowledge", "handler.py")
    spec = importlib.util.spec_from_file_location("byxin_answer_handler", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def setup(a):
    """Decide the project, the brain and the hub for this call, put that brain's tools first on the path, and keep a
    vendored brain's state out of the mod's own folder. Returns the answering layer's handler module."""
    project, cd = project_of(a)
    tree, why = brain_tree(project)
    hub = hub_dir(project, cd)
    CTX.update(project=project, common=cd, tree=tree, tree_why=why, hub=hub, vendored=(tree == HERE),
               root=os.path.abspath(a.get("root") or project), session=str(a.get("session") or "")[:80],
               host=os.environ.get("BYXIN_HOST", "").strip() or socket.gethostname())
    sys.path.insert(0, os.path.join(tree, "tools"))
    CTX["db"] = None
    if CTX["vendored"]:
        data = os.path.join(hub, "data")
        os.makedirs(data, exist_ok=True)
        CTX["db"] = os.path.join(data, "hippocampus.db")
        try:
            from pathlib import Path
            import byxin_history as BH
            BH.resolve_db = lambda db=None: Path(db) if db else Path(CTX["db"])
            BH.SELFTEST_LAST = os.path.join(data, "byxin_selftest_last.json")
        except Exception:
            pass
        try:
            import byxin_bm25 as B
            B.cache_path = lambda root=None: os.path.join(data, "bm25_%s.json" % hashlib.sha1(
                os.path.abspath(root or CTX["root"]).lower().encode("utf-8")).hexdigest()[:10])
        except Exception:
            pass
    return _handler(tree)


def _branch():
    if "branch" not in CTX:
        try:
            CTX["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], CTX["root"], timeout=20).strip()
        except Exception:
            CTX["branch"] = ""
    return CTX["branch"]


# ── the answering layer, split around the session's model ────────────────────────────────────────────────

def _corpus(R, root):
    """The watched corpus is the session's repository unless it carries its own cerebellum.json (a ByxIn tree)."""
    if R._watched_dirs(root):
        return "watched directories of the tree's cerebellum.json"
    # When the plugin lives inside the repository it indexes, with its vendored copy of the brain: every
    # hit would otherwise appear twice, once as the source and once as its copy. Git pathspec excludes keep both
    # `git grep` (identifier recall) and `git ls-files` (BM25) off it.
    skip = ["claude-mod"]
    rel = os.path.relpath(os.path.dirname(HERE), root).replace(os.sep, "/")
    if not rel.startswith("..") and rel not in skip:
        skip.append(rel)
    R._watched_dirs = lambda r=None: ["."] + [":(exclude)%s" % p for p in skip]
    R.repo_root = lambda: root
    return "session repository (minus %s)" % ", ".join(skip)


def _anchored(R, subject, common=lambda t: False, tokens=lambda s: [s]):
    """
    """
    return any(not t.replace("-", "").replace(".", "").isdigit() and not all(common(w) for w in tokens(t) or [t])
               for t in R.exact_tokens_in(subject))


#: Words a question is made of, never what it is about. Measured 2026-10-05 on a 61-page project: "how does the
#: AcmeOS tokenizer split input?" scored 0.40 coverage on the one page that answers it, because "how" and "does" were
#: in no page and so weighed as the rarest terms of all. A large record hides this (they are common there); a small
#: one does not.
QUESTION_WORDS = frozenset(
    "a about all also an and any are as at be been but by can could did do does each every for from had has have he her "
    "his how i if in into is it its just me many more most much my no not now of on only or our out over please she should show so "
    "some tell than that the their them then there these they this those to under up us very was we were what when "
    "where which who whom why will with would yes you your explain describe".split())


def _judge(R, B, subject, root):
    """How this record judges a question: the IDF weight of each of its rare terms (a word in more than DF_MAX of the
    chunks is not vocabulary of the record -- the project's own name), whether it is anchored, and covers(content,
    source) -> the share of that weight a passage carries, 0.0 under the minimum number of distinct rare terms."""
    idx = B.load(root)
    n_chunks, post = len(idx.get("chunks") or []), idx.get("postings") or {}
    df = lambda t: len(post.get(t) or []) // 2
    common = lambda t: bool(df(t)) and df(t) > DF_MAX * n_chunks
    weights = {t: (B.idf(df(t), n_chunks) if df(t) else B.idf(1, n_chunks)) for t in set(B.tokens(subject, expand=False))
               if not common(t) and t not in QUESTION_WORDS}
    total = sum(weights.values()) or 1.0
    anchored = _anchored(R, subject, common, lambda s: B.tokens(s, expand=False))
    need = MIN_TERMS_ANCHORED if anchored else MIN_TERMS_PLAIN

    def covers(content, source=""):
        have = set(B.tokens(content, expand=True)) | set(B.tokens(str(source).replace("\\", "/"), expand=True))   # a file is also its own name
        if sum(1 for t in weights if t in have) < need:
            return 0.0                          # too few of the question's own terms: a coincidence, not an answer
        return sum(w for t, w in weights.items() if t in have) / total
    return covers, (COVERAGE_ANCHORED if anchored else COVERAGE), anchored, bool(weights)


#: how long the answer to "is this engine this project's brain?" is kept: it changes only when an engine is swapped
ENGINE_SERVES_TTL = 600


def mark_ask():
    """MEASUREMENTS YIELD TO A PERSON (the user, 2026-10-01). Before this layer asks a brain tree's live engine anything,
    it leaves a mark in the tree -- when, which session, nothing of the question -- and the tree's bench reads it as a
    person present (byxin_bench.person_present). Measured 2026-10-04: every question in a Claude Code session made three
    engine calls the bench could not see. A vendored brain has no bench to tell."""
    if CTX.get("vendored") or not CTX.get("tree"):
        return
    try:
        d = os.path.join(CTX["tree"], "data")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "answering_layer_ask.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"at": _now(), "session": (CTX.get("session") or "")[:8], "host": CTX.get("host")}, fh)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def engine_serves(url):
    """
    """
    base = os.path.dirname(HERE) if CTX.get("vendored") else CTX.get("tree") or ""
    cache = os.path.join(_dir("runtime"), "engine_serves.json")
    key = "%s|%s" % (url, os.path.normcase(os.path.abspath(base)))
    try:
        with open(cache, encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("key") == key and time.time() - float(c.get("t") or 0) < ENGINE_SERVES_TTL:
            return c.get("serves")
    except (OSError, ValueError):
        pass
    serves = _engine_serves(url, base)
    if serves is not None:
        try:
            with open(cache + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"key": key, "t": time.time(), "serves": serves}, fh)
            os.replace(cache + ".tmp", cache)
        except OSError:
            pass
    return serves


def _engine_serves(url, base):
    import urllib.request
    try:
        req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "byxin.episodic.stats",
                                                           "params": {}}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            writer = (json.loads(r.read().decode("utf-8", "replace")).get("result") or {}).get("writer") or ""
    except Exception:
        return None
    w, b = os.path.normcase(os.path.abspath(writer)), os.path.normcase(os.path.abspath(base))
    return bool(writer) and w.startswith(b.rstrip(os.sep) + os.sep)


def prepare(a, H):
    root = os.path.abspath(a.get("root") or os.getcwd())
    env = envelope_of(a.get("question"))
    if env:
        # no one asked: nothing to look up, no engine to call. Here as well as in the mod, because this bridge is started
        # afresh for every call and so reaches a session still running an older module at once.
        return {"ok": True, "corpus": "", "root": root, "subject": "", "chunks": 0, "anchored": False, "answerable": False,
                "retrieval_note": "no one asked: a %s" % env, "system": "", "facts": "", "sources": [], "lessons": [],
                "state": {}}
    import byxin_rerank as R
    import byxin_bm25 as B
    corpus = _corpus(R, root)
    q = (a.get("question") or "").strip()
    subject = H.subject_of(q)
    covers, floor, anchored, judged = _judge(R, B, subject, root)
    block, kept, top, retrieval_note = "", [], 0.0, None
    url = "http://127.0.0.1:%s/" % H._rpc_port()
    mark_ask()                                     # the tree's bench learns a person is asking before the engine does
    try:                                           # the dense lane needs the C++ engine AND an embedder behind it
        if engine_serves(url) is False:
            raise LookupError("the engine on %s serves another brain tree, not this project's" % url)
        dense = R._lookup(subject, 12, url=url)
        if not dense:
            raise LookupError("the engine returned no dense hits (no embedder behind it)")
        # real dense hits: the retriever's own measured floor applies, and its lexical joins ride with it
        block, kept, top = R.retrieve(subject, max_chunks=H.TOP_K, url=url)
        if judged and kept:
            # then the coverage floor; a question whose every word is common to the record cannot be judged by it,
            # and keeps the retriever's own floor
            cov = [covers(h.get("content") or "", h.get("source") or "") for h in kept]
            held = [h for h, c in zip(kept, cov) if c >= floor]
            if len(held) < len(kept):
                retrieval_note = "%d of %d dense passage(s) under the coverage floor %.2f (coverage/similarity %s)" % (
                    len(kept) - len(held), len(kept), floor,
                    ",".join("%.2f/%.2f" % (c, float(h.get("adjusted") or h.get("similarity") or 0.0)) for h, c in zip(kept, cov)))
                kept = held
                block = "\n\n".join("Source: %s\n%s" % (h.get("source") or "?", (h.get("content") or "").strip()) for h in kept)
                top = max((float(h.get("adjusted") or h.get("similarity") or 0.0) for h in kept), default=0.0)
    except Exception as e:
        retrieval_note = "dense lane unavailable (%s: %s); lexical lanes only" % (type(e).__name__, str(e)[:160])
        lex = R.identifier_hits(subject, 0.0, root=root)
        rejected = []
        kept_lex = []
        for h in lex:
            c = covers(h.get("content") or "", str(h.get("source") or ""))
            (kept_lex if c >= floor else rejected).append(h if c >= floor else round(c, 2))
        lex = kept_lex
        for h in B.query(subject, k=R.tunable("BM25_KEEP") or 3, root=root):
            cov = covers(h["content"], h["source"])
            if cov < floor:
                rejected.append(round(cov, 2)); continue
            lex.append({"source": h["source"], "content": h["content"], "similarity": 0.0, "lexical": "bm25:" + ",".join(h.get("terms") or [])[:60], "coverage": round(cov, 2)})
        if rejected:
            retrieval_note += "; %d lexical hit(s) under the coverage floor %.2f (best %.2f)" % (len(rejected), floor, max(rejected))
        kept = lex[:H.TOP_K]
        block = "\n\n".join("Source: %s\n%s" % (os.path.relpath(h["source"], root) if os.path.isabs(h["source"]) else h["source"], h["content"]) for h in kept)
    reported = H.self_report(q)
    developed = H.development_report(q)
    contested = H.contested_notes(block)
    fired = []
    taught = H.lessons_for(q, fired)
    facts = "\n\n".join(p for p in (taught, contested, reported, developed, block) if p)
    sources = H.sources_of(block) + (["byxin.episodic.stats (live self-report)"] if reported else []) \
        + (["git log (live development report)"] if developed else [])
    return {"ok": True, "corpus": corpus, "root": root, "subject": subject, "chunks": len(kept),
            "retrieval_note": retrieval_note, "anchored": anchored, "answerable": bool(facts.strip()), "system": H.ANSWER_SYSTEM + facts,
            "facts": facts, "sources": sources, "lessons": fired,
            "state": {"taught": taught, "reported": reported, "developed": developed, "block": block,
                      "sources": sources, "facts": facts, "kept": [{"source": str(k.get("source")), "content": (k.get("content") or "")[:400]} for k in kept],
                      "top": top}}


def check(a, H):
    import byxin_rerank as R
    st = a.get("state") or {}
    answer, q = a.get("answer") or "", a.get("question") or ""
    shown = list(st.get("sources") or []) + H.cited_sources(st.get("taught") or "") + H.cited_sources(st.get("block") or "")
    # the session's own reads count as shown: a path the model opened this turn is a path it was given
    shown += list(a.get("reads") or [])
    cmp_ = H.compare_citations(answer, shown)
    try:
        import byxin_nli as N
        if R.tunable("NUMBER_CHECK"):
            cmp_.update(N.compare_numbers(answer, "\n".join([q, st.get("facts") or ""] + [str(s) for s in shown] + list(a.get("seen") or []))))
        if R.tunable("NLI_ON"):
            cmp_.update(N.compare_claims(answer, [st.get("taught") or "", st.get("reported") or ""] + [k.get("content") or "" for k in st.get("kept") or []]))
        if cmp_.get("unsupported_numbers") or cmp_.get("contradicted"):
            cmp_["verified"] = False
    except Exception as e:
        cmp_["claim_check_error"] = "%s: %s" % (type(e).__name__, e)
    annotated = H.downgrade(answer, cmp_)
    annotated, disputes = H.contested_verdict(annotated, st.get("block") or "")
    if disputes:
        cmp_["disputed"] = disputes
    note = annotated[len(answer.rstrip()):].strip() if annotated.startswith(answer.rstrip()) else ""
    return {"ok": True, "attribution": cmp_, "note": note}


def _operator():
    return "claude-code:%s@%s" % ((CTX.get("session") or "?")[:8], CTX.get("host") or "?")


def _record(intent, outcome, origin, skill, action=None, narration=None, reason=None, operator=None):
    """One episode in the brain's own record, under the answering layer's declared source. Returns where it went."""
    try:
        import byxin_history
        byxin_history.record(intent[:300] or "(empty)", source="answering_layer", outcome=outcome, origin=origin,
                             origin_ref="claude code session %s on %s" % (CTX.get("session") or "?", CTX.get("host")),
                             skill=skill, action=action, narration=narration, reason=reason,
                             operator=operator or _operator(), db=CTX.get("db"))
        return "the brain's record (%s)" % ("this project's record beside the mod's brain" if CTX.get("vendored")
                                            else os.path.join(CTX["tree"], "data"))
    except Exception as e:
        os.makedirs(os.path.join(CTX["hub"], "data"), exist_ok=True)
        with open(os.path.join(CTX["hub"], "data", "episodes.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), "intent": intent[:300], "outcome": outcome, "origin": origin,
                                 "skill": skill, "operator": operator or _operator()}) + "\n")
        return "the hub's local ledger, because the record refused it: %s: %s" % (type(e).__name__, e)


def remember(a, H):
    """History: an answered question is an episode with declared provenance (answering_layer, perceived)."""
    stored = _record(a.get("question") or "", a.get("outcome") or "answered", "perceived", "answer_from_knowledge",
                     action=json.dumps(a.get("participation"))[:2000])
    return {"ok": True, "stored": stored}


def state(a, H):
    import byxin_affect
    return {"ok": True, "text": byxin_affect.render(byxin_affect.state())}


def lessons(a, H):
    import byxin_lessons as L
    rows = L.load() if hasattr(L, "load") else []
    return {"ok": True, "lessons": [{"slug": r.get("slug"), "title": r.get("title"), "lesson": r.get("lesson")} for r in rows]}


def mind(a, H):
    import byxin_mind as M
    s = M.score(db=CTX.get("db"))
    held, n = s.get("held", 0), s.get("predictions", 0)
    return {"ok": True, "text": "the mind's predictions about itself: %d held of %d%s" % (
        held, n, "" if n else " (none recorded yet: the trainer records one per trial on the mind's focus)"), "score": s}


# ── the shared brain ─────────────────────────────────────────────────────────────────────────────────────────

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dir(name):
    d = os.path.join(CTX["hub"], name)
    os.makedirs(d, exist_ok=True)
    return d


def write_event(kind, origin="perceived", by="claude-code", **fields):
    """One write-once event file. Its name is unique, so two writers and two machines never collide."""
    sid = (CTX.get("session") or "nosession")[:8]
    eid = "%s-%s-%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), sid, kind,
                           "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6)))
    ev = {"id": eid, "at": _now(), "kind": kind, "origin": origin, "by": by, "session": CTX.get("session") or "",
          "host": CTX.get("host"), "branch": _branch()}
    ev.update({k: v for k, v in fields.items() if v not in (None, "", [])})
    path = os.path.join(_dir("events"), eid + ".json")
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(ev, fh, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return ev


def events():
    d, out = _dir("events"), []
    for n in os.listdir(d):
        if n.endswith(".json"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    out.sort(key=lambda e: (e.get("at") or "", e.get("id") or ""))
    return out


def _epoch(at):
    try:
        import calendar
        return calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0.0


def _ago(at):
    s = max(0, int(time.time() - _epoch(at)))
    return "%d s ago" % s if s < 90 else "%d min ago" % (s // 60) if s < 5400 else "%d h ago" % (s // 3600) if s < 172800 \
        else "%d days ago" % (s // 86400)


def beat_file(files):
    """This session's presence: overwritten in place, never synced -- liveness is local; elsewhere it is the events."""
    p = os.path.join(_dir("presence"), "%s.json" % (CTX.get("session") or "nosession").replace(os.sep, "_"))
    rec = {"session": CTX.get("session"), "host": CTX.get("host"), "branch": _branch(), "at": _now(),
           "files": sorted(set(_project_files(files)))[:200]}
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(p + ".tmp", p)


def live_sessions():
    """Other sessions alive right now: here by their beat, elsewhere by an event younger than ELSEWHERE_S and no end."""
    now, me, out = time.time(), CTX.get("session"), {}
    d = _dir("presence")
    for n in os.listdir(d):
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        age = now - _epoch(rec.get("at"))
        if age > 86400:
            try:
                os.remove(os.path.join(d, n))          # a beat a day old is a session that died without its end
            except OSError:
                pass
            continue
        if rec.get("session") != me and age <= LIVE_S:
            out[rec["session"]] = dict(rec, where="this machine")
    last, ended, evs = {}, set(), events()
    for ev in evs:
        if ev.get("kind") == "end":
            ended.add(ev.get("session"))
        last[ev.get("session")] = ev
    for sid, ev in last.items():
        if sid == me or sid in out or sid in ended or ev.get("host") == CTX.get("host"):
            continue
        if now - _epoch(ev.get("at")) <= ELSEWHERE_S:
            files = sorted({f for e in evs if e.get("session") == sid for f in (e.get("files") or [])})
            out[sid] = {"session": sid, "host": ev.get("host"), "branch": ev.get("branch"), "at": ev.get("at"),
                        "files": files, "where": "another machine, through the shared branch"}
    return list(out.values())


#: the most a block may carry before it is cut at a line (the full record stays one command away: /byxin brain)
BLOCK_CAP, NEWS_CAP = 5000, 2500


def _in_project(f):
    """A path the shared brain may name: relative, inside the project. Measured 2026-10-05: sessions were shown
    "editing" scratchpads, memory files and other repositories -- none of them this project's business."""
    f = str(f or "").replace("\\", "/")
    return bool(f) and not f.startswith("/") and not re.match(r"^[A-Za-z]:", f) and f != ".." and not f.startswith("../")


def _project_files(files):
    return [f for f in (files or []) if _in_project(f)]


def _edited(t):
    """What a turn edited, as the block says it: the project's files, or that it touched only files outside."""
    mine = _project_files(t.get("files"))
    if mine:
        return "edited " + ", ".join(mine[:6])
    return "edited only files outside the project" if t.get("files") else "edited nothing"


def _cap(lines, cap):
    out, n = [], 0
    for line in lines:
        if n + len(line) + 1 > cap:
            out.append("  ... (cut at %d characters; /byxin brain shows the whole record)" % cap)
            break
        out.append(line)
        n += len(line) + 1
    return "\n".join(out)


def news_text(since):
    """What changed since this session last heard: sessions that started or ended, turns that asked or edited, notes,
    corrections. A turn no one asked that edited nothing is not news."""
    me, evs = CTX.get("session"), events()
    new = [e for e in evs if (e.get("at") or "") > since and e.get("session") != me]
    lines = []
    for e in new:
        who = "session %s on %s, branch %s" % ((e.get("session") or "?")[:8], e.get("host"), e.get("branch") or "?")
        kind = e.get("kind")
        if kind == "start":
            lines.append("  - %s: started" % who)
        elif kind == "end":
            lines.append("  - %s: ended" % who)
        elif kind == "note":
            lines.append("  - note left for every session (told): %s (%s)" % (e.get("text"), who))
        elif kind == "retract":
            lines.append("  - correction: %s was wrong: %s (%s)" % (e.get("retracts"), e.get("why") or "no reason given", who))
        elif kind == "turn" and _asked(e):
            lines.append("  - %s: asked: %s%s; %s" % (who, (e.get("ask") or "")[:200],
                                                     " -> %s" % e["outcome"] if e.get("outcome") else "", _edited(e)))
        elif kind == "turn" and _project_files(e.get("files")):
            lines.append("  - %s: after a %s (no one asked): %s" % (who, _trigger(e), _edited(e)))
    if not lines:
        return ""
    return _cap(["BYXIN SHARED BRAIN -- new since your last prompt, as this mod recorded it (perceived; a note is told). "
                 "Read a file before relying on what another session did to it."] + lines, NEWS_CAP)


def _asked(t):
    """Did a person ask this turn? The mod writes no ask for a turn something else started; a record written before
    it knew (its ask the envelope itself) is read the same way."""
    return bool(t.get("ask")) and not envelope_of(t.get("ask"))


def _trigger(t):
    """What started a turn no one asked: the engine's origin of the prompt, else the envelope it came in."""
    return t.get("trigger") or envelope_of(t.get("ask")) or "notification"


def resume_text(since=None, limit_sessions=6, limit_notes=10):
    """What the other sessions of this project did and left, as a block a session can read before it acts."""
    me, evs = CTX.get("session"), events()
    # A retraction is its own write-once event naming the event it corrects: the wrong record stays, marked, the way
    # this project retracts in the record rather than editing a claim into a different one.
    retracted = {e.get("retracts"): e for e in evs if e.get("kind") == "retract" and e.get("retracts")}
    if since:
        return news_text(since)
    notes = [e for e in evs if e.get("kind") == "note"][-limit_notes:]
    by_session = {}
    for e in evs:
        if e.get("session") == me or e.get("kind") in ("note", "retract"):
            continue
        by_session.setdefault(e.get("session"), []).append(e)
    live = {s["session"]: s for s in live_sessions()}
    ordered = sorted(by_session.items(), key=lambda kv: kv[1][-1].get("at") or "", reverse=True)
    recent, older = ordered[:limit_sessions], ordered[limit_sessions:]
    open_threads = [e for e in evs if e.get("kind") == "turn" and e.get("outcome") == "unverified" and e.get("id") not in retracted
                    and _asked(e) and time.time() - _epoch(e.get("at")) < 2 * 86400][-5:]
    if not notes and not recent and not retracted:
        return ""
    lines = ["BYXIN SHARED BRAIN -- what the other Claude Code sessions in this project did, as this mod recorded it while "
             "it happened (perceived), and notes people left for every session (told). It is a record of those "
             "sessions, not a fact about the code: read a file before relying on what another session did to it."]
    if live:
        lines.append("")
        lines.append("Working in this project right now:")
        for s in live.values():
            editing = _project_files(s.get("files"))
            lines.append("  - session %s on %s (%s), branch %s, last seen %s%s" % (
                (s.get("session") or "?")[:8], s.get("host"), s.get("where"), s.get("branch") or "?", _ago(s.get("at")),
                "; editing " + ", ".join(editing[:8]) if editing else ""))
    if notes:
        lines.append("")
        lines.append("Notes left for every session (told):")
        for n in reversed(notes):
            lines.append("  - %s (%s, session %s on %s)" % (n.get("text"), _ago(n.get("at")), (n.get("session") or "?")[:8],
                                                            n.get("host")))
    for sid, es in recent:
        turns = [e for e in es if e.get("kind") == "turn" and e.get("id") not in retracted]
        files = sorted({f for e in turns for f in _project_files(e.get("files"))})
        ended = any(e.get("kind") == "end" for e in es)
        lines.append("")
        lines.append("Session %s on %s, branch %s, %s %s:" % ((sid or "?")[:8], es[-1].get("host"), es[-1].get("branch") or "?",
                                                               "ended" if ended else "last active", _ago(es[-1].get("at"))))
        # a turn no one asked (a notification, a peer, a schedule) is shown only for what it edited
        shown = [t for t in turns if _asked(t) or _project_files(t.get("files"))]
        quiet = [t for t in turns if not _asked(t) and not _project_files(t.get("files"))]
        answer = lambda t: "; its answer began: %s" % " ".join((t.get("answer") or "").split())[:140] if t.get("answer") else ""
        for t in shown[-3:]:
            if not _asked(t):
                lines.append("  - after a %s (no one asked): %s%s" % (_trigger(t), _edited(t), answer(t)))
                continue
            # what was ASKED is not what was DONE: measured 2026-10-03, a session read "asked: create SESSION_LOG.md"
            # as the file created, while the write had been refused. So every turn says what it edited, or that it
            # edited nothing, and how its answer began.
            lines.append("  - asked: %s%s; %s%s" % (
                (t.get("ask") or "")[:200], " -> %s" % t["outcome"] if t.get("outcome") else "", _edited(t), answer(t)))
        if quiet:
            lines.append("  - %d turn(s) no one asked (%s); edited nothing in the project" % (
                len(quiet), ", ".join(sorted({_trigger(t) for t in quiet}))))
        if files and not any(_project_files(t.get("files")) for t in shown[-3:]):
            lines.append("  - files edited: " + ", ".join(files[:10]))
    if older:
        # Measured 2026-10-03, the cloud session's run: the edit of a session older than the six shown fell out of the
        # block, and the model could only call it another session's claim. An edit never falls out: the older sessions
        # are summed up in one line with every file they changed.
        of = sorted({f for _sid, es in older for e in es if e.get("kind") == "turn" and e.get("id") not in retracted
                     for f in _project_files(e.get("files"))})
        lines.append("")
        lines.append("%d older session(s) not shown in full; %s (/byxin brain shows them)." % (
            len(older), "between them they edited " + ", ".join(of[:20]) if of else "they edited nothing"))
    if retracted:
        lines.append("")
        lines.append("Corrections (a record that was wrong, retracted, kept and marked):")
        for rid, r in list(retracted.items())[-5:]:
            lines.append("  - %s was wrong: %s (%s)" % (rid, r.get("why") or "no reason given", _ago(r.get("at"))))
    if open_threads:
        lines.append("")
        lines.append("Left open (answers the comparator could not verify):")
        for t in open_threads:
            lines.append("  - %s (session %s, %s)" % ((t.get("ask") or "")[:160], (t.get("session") or "?")[:8], _ago(t.get("at"))))
    text = "\n".join(lines)
    if len(text) > BLOCK_CAP and limit_sessions > 1:
        # too long: fewer sessions in full, the rest summed up in the older-sessions line, so no edit falls out
        return resume_text(None, limit_sessions - 1, limit_notes)
    return _cap(lines, BLOCK_CAP)


def share_config():
    if os.environ.get("BYXIN_SHARE", "").strip() == "0":
        return None          # a person or a test keeps this run's brain on this machine, share.json or not
    for base in (CTX.get("root"), CTX.get("project")):
        p = os.path.join(base or "", ".byxin", "share.json")
        if base and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                return {"remote": cfg.get("remote") or "origin", "branch": cfg.get("branch") or "byxin-brain",
                        "never": list(cfg.get("never") or ["public"]), "file": p}
            except (OSError, ValueError):
                return None
    return None


def sync(push=True):
    """Union the hub's events with the shared branch on the remote .byxin/share.json names. Plumbing only: a scratch
    index of this process's own, nothing staged in anyone's index, no branch checked out. Returns what moved."""
    cfg = share_config()
    if not cfg:
        return {"shared": False}
    if not CTX.get("common"):
        return {"shared": True, "error": "the project is not a git repository"}
    remote, branch, root = cfg["remote"], cfg["branch"], CTX["project"]
    if remote in cfg["never"]:
        return {"shared": True, "error": "refusing to sync to %r: .byxin/share.json lists it under never" % remote}
    ref = "refs/remotes/%s/%s" % (remote, branch)
    evdir, pulled, pushed, errors = _dir("events"), 0, 0, []
    for attempt in (1, 2):
        try:
            _git(["fetch", "--quiet", remote, "+refs/heads/%s:%s" % (branch, ref)], root, timeout=45)
        except Exception as e:
            errors.append("fetch: %s" % e)            # the branch may not exist yet; the first push makes it
        parent = _git(["rev-parse", "--verify", "--quiet", ref], root, check=False).strip()
        if parent:
            for name in (n.strip() for n in _git(["ls-tree", "--name-only", ref], root, check=False).splitlines()):
                if not name.endswith(".json") or os.path.exists(os.path.join(evdir, name)):
                    continue                          # write-once: an event already held is never fetched again
                blob = _git(["show", "%s:%s" % (ref, name)], root, check=False)
                try:
                    json.loads(blob)
                except ValueError:
                    continue
                with open(os.path.join(evdir, name + ".tmp"), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(blob)
                os.replace(os.path.join(evdir, name + ".tmp"), os.path.join(evdir, name))
                pulled += 1
        if not push:
            break
        idx = os.path.join(CTX["hub"], "index-%d" % os.getpid())
        env = dict(BRAIN_IDENT, GIT_INDEX_FILE=idx)
        try:
            if os.path.exists(idx):
                os.remove(idx)
            names = sorted(n for n in os.listdir(evdir) if n.endswith(".json"))
            if not names:
                return {"shared": True, "pulled": pulled, "pushed": 0, "branch": branch, "remote": remote}
            shas = _git(["hash-object", "-w", "--stdin-paths"], root, env=env,
                        input="\n".join(os.path.join(evdir, n) for n in names) + "\n").split()
            _git(["update-index", "--add", "--index-info"], root, env=env,
                 input="".join("100644 %s\t%s\n" % (s, n) for s, n in zip(shas, names)))
            tree = _git(["write-tree"], root, env=env).strip()
            listed = _git(["ls-tree", "--name-only", tree], root).split()
            if len(listed) != len(names):
                raise RuntimeError("the tree holds %d of %d events; refusing to push it" % (len(listed), len(names)))
            if parent and _git(["rev-parse", "%s^{tree}" % parent], root, check=False).strip() == tree:
                return {"shared": True, "pulled": pulled, "pushed": 0, "note": "in sync", "branch": branch, "remote": remote}
            have = set(_git(["ls-tree", "--name-only", parent], root, check=False).split()) if parent else set()
            args = ["commit-tree", tree, "-m", "byxin shared brain: %d event(s)" % len(names)] + (["-p", parent] if parent else [])
            commit = _git(args, root, env=env).strip()
            _git(["push", "--quiet", remote, "%s:refs/heads/%s" % (commit, branch)], root, timeout=60)
            _git(["update-ref", ref, commit], root, check=False)
            pushed = len([n for n in names if n not in have])
            return {"shared": True, "pulled": pulled, "pushed": pushed, "branch": branch, "remote": remote, "errors": errors}
        except Exception as e:
            errors.append("push attempt %d: %s" % (attempt, e))   # another session pushed first: fetch, union, again
        finally:
            try:
                os.remove(idx)
            except OSError:
                pass
    return {"shared": True, "pulled": pulled, "pushed": pushed, "branch": branch, "remote": remote, "errors": errors}


def _last_sync_due():
    p = os.path.join(_dir("presence"), "%s.sync" % (CTX.get("session") or "nosession"))
    try:
        due = time.time() - os.path.getmtime(p) >= SYNC_EVERY_S
    except OSError:
        due = True
    if due:
        with open(p, "w") as fh:
            fh.write(_now())
    return due


def where(a, H):
    rt = os.path.join(tempfile.gettempdir(), "byxin-grounded")
    os.makedirs(os.path.join(rt, "queue"), exist_ok=True)
    return {"ok": True, "project": CTX["project"], "tree": CTX["tree"], "tree_why": CTX["tree_why"], "hub": CTX["hub"],
            "vendored": CTX["vendored"],
            "record": CTX.get("db") or os.path.join(CTX["tree"], "data", "hippocampus.db"), "shared": share_config(),
            "host": CTX["host"], "branch": _branch(), "runtime": rt.replace("\\", "/")}


def start(a, H):
    write_event("start", cwd=CTX["root"])
    beat_file([])
    s = sync() if share_config() else {"shared": False}
    return {"ok": True, "text": resume_text(), "sync": s, "live": live_sessions(), "at": _now()}


def beat(a, H):
    beat_file(a.get("files"))
    s = sync() if share_config() and _last_sync_due() else None
    return {"ok": True, "live": live_sessions(), "news": resume_text(since=a.get("since")) if a.get("since") else "",
            "sync": s, "at": _now()}


def turn(a, H):
    """One turn: what was asked (empty when no person asked: `trigger` then says what started it), what it edited."""
    ask, trigger, env = a.get("ask") or "", a.get("trigger"), envelope_of(a.get("ask"))
    if env:
        # an older module sends the notification itself as the ask: the turn is no one asking, kept only for its edits
        if not a.get("files"):
            return {"ok": True, "event": None, "skipped": "no one asked: a %s, and nothing was edited" % env, "sync": None}
        ask, trigger = "", trigger or env
    ev = write_event("turn", ask=ask[:600], answer=(a.get("answer") or "")[:600],
                     files=list(a.get("files") or [])[:100], outcome=a.get("outcome"), trigger=trigger)
    stored = None
    if a.get("outcome") and ask:
        stored = _record(ask, a["outcome"], "perceived", "answer_from_knowledge",
                         action=json.dumps(a.get("participation"))[:2000], narration=(a.get("answer") or "")[:4000])
    # local first: the event is on disk before anything touches the network, and any later sync carries it
    s = sync() if share_config() and a.get("sync", True) else None
    return {"ok": True, "event": ev["id"], "stored": stored, "sync": s}


def note(a, H):
    text = (a.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "a note needs words: /byxin note <what every session in this project should know>"}
    ev = write_event("note", origin="told", by="person", text=text[:1000])
    stored = _record(text, "noted", "told", "note", operator="person via " + _operator())
    s = sync() if share_config() else None
    return {"ok": True, "event": ev["id"], "stored": stored, "sync": s}


def notes(a, H):
    return {"ok": True, "notes": [{"text": e.get("text"), "at": e.get("at"), "session": e.get("session"), "host": e.get("host")}
                                  for e in events() if e.get("kind") == "note"]}


def resume(a, H):
    s = sync() if share_config() else {"shared": False}
    return {"ok": True, "text": resume_text() or "The shared brain holds nothing from other sessions of this project yet.",
            "sync": s, "live": live_sessions()}


def end(a, H):
    write_event("end", files=list(a.get("files") or [])[:200], asked=a.get("asked"))
    try:
        os.remove(os.path.join(_dir("presence"), "%s.json" % (CTX.get("session") or "nosession")))
    except OSError:
        pass
    s = sync() if share_config() else None
    return {"ok": True, "sync": s}


def retract(a, H):
    """Mark one event of the shared brain as wrong, with the reason: a new write-once event, the old one untouched."""
    want, why = (a.get("event") or "").strip(), (a.get("why") or "").strip()
    if not want or not why:
        return {"ok": False, "error": "a retraction needs the event and the reason: /byxin retract <event-id> <why>"}
    hit = [e for e in events() if (e.get("id") or "").startswith(want) and e.get("kind") != "retract"]
    if len(hit) != 1:
        return {"ok": False, "error": "%d events start with %r; give enough of the id to name one" % (len(hit), want)}
    ev = write_event("retract", origin="told", by="person", retracts=hit[0]["id"], why=why[:500])
    s = sync() if share_config() else None
    return {"ok": True, "event": ev["id"], "retracts": hit[0]["id"], "sync": s}


def events_list(a, H):
    return {"ok": True, "events": [{k: e.get(k) for k in ("id", "kind", "at", "session", "host", "files", "outcome", "trigger")}
                                   for e in events()[-int(a.get("n") or 30):]]}


def share(a, H):
    """`on [remote] [branch]` writes .byxin/share.json in the session's working tree -- a file for a person to commit,
    so every clone of the project, a cloud session's included, shares the brain. `off` removes it; `status` reads it."""
    act = (a.get("act") or "status").lower()
    p = os.path.join(CTX["root"], ".byxin", "share.json")
    if act == "on":
        cfg = {"remote": a.get("remote") or "origin", "branch": a.get("branch") or "byxin-brain", "never": ["public"],
               "_what": "ByxIn's shared brain: every Claude Code session in this project unions its events with this orphan "
                        "branch (the byxin-grounded plugin). Commit this file to share; delete it to stop."}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cfg, fh, indent=1)
            fh.write("\n")
        return {"ok": True, "text": "wrote %s: commit it so every clone shares the brain through %s/%s" % (p, cfg["remote"], cfg["branch"])}
    if act == "off":
        try:
            os.remove(p)
            return {"ok": True, "text": "removed %s; commit the removal to stop sharing everywhere" % p}
        except OSError:
            return {"ok": True, "text": "nothing to remove: %s does not exist" % p}
    if act == "sync":
        return {"ok": True, "text": json.dumps(sync())}
    return {"ok": True, "text": json.dumps(share_config()) if share_config() else
            "not shared beyond this machine: no .byxin/share.json (use /byxin share on [remote] [branch])"}


OPS = {"prepare": prepare, "check": check, "remember": remember, "lessons": lessons,
       "where": where, "start": start, "beat": beat, "turn": turn, "note": note, "notes": notes, "resume": resume,
       "end": end, "share": share, "retract": retract, "events": events_list}

if __name__ == "__main__":
    try:
        a = json.loads(sys.stdin.read() or "{}")
        H = setup(a)
        out = OPS[a.get("op") or "prepare"](a, H)
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}
    sys.stdout.write(json.dumps(out))
