#!/usr/bin/env python3
"""byxin_rerank.py -- reorder retrieved chunks by WHERE they apply, because similarity cannot tell hosts apart.



The narrative chunk is more SIMILAR because it says those numbers many times in many sentences. It is about a
different machine. Cosine similarity has no way to know that, and no amount of prompting downstream can recover
a fact that was never put in front of the model.

WHAT IS AND IS NOT HERE. One signal, derived from the source PATH and never hand-written per chunk -- ledger #19
says a wrong place tag is worse than none, and a tag a human maintains will go wrong. A literal-overlap signal
was hypothesised first and REFUTED by the evidence: the rank-1 chunk contains both of the question's port
numbers, so "does the chunk mention what was asked about" would have kept it first. The place is what separates
them.

"""
import argparse
import io
import json
import os
import re
import subprocess
import sys

#: The repository this tools/ directory belongs to -- the same resolution min_confidence() uses.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root():
    return ROOT


BOARD, TWIN, DESK, ANY = "board", "twin", "desk", "timeless"

#: Where a chunk applies, decided by its path, and what place a QUESTION is about: HOST FACTS of the world this
#: brain lives in, read from the world pack (config/world.json, tools/byxin_world.py) -- the first path match
#: wins, so the pack lists the most specific patterns first; a question's place counts only on an explicit
#: phrase, because a wrong guess reorders confidently in the wrong direction. A nameless world has no places
#: and nothing is reordered. Moved out of this file 2026-10-01 (the brain/world split): the patterns are the
#: same bytes the code carried, checked pattern for pattern before the move.
try:
    import byxin_world as _world
except ImportError:                      # imported by path rather than from tools/ on sys.path
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import byxin_world as _world
_PLACE_BY_PATH, _PLACE_BY_QUESTION = _world.places()

AUTHORITY, DESIGN, EVIDENCE = "authority", "design", "evidence"
_CLASS_BY_PATH = _world.claim_kinds()
_ASKS_ABOUT_PROOF = re.compile(r"\b(formally|verified|verification|proven|proof|proofs|machine[- ]checked|"
                               r"guaranteed|certified)\b", re.I)
CLAIM_BONUS = 0.05
CLAIM_PENALTY = 0.15

LEXICAL_KEEP = 2
BM25_KEEP = 0
#: THE CLAIM COMPARATOR's two lenses (tools/byxin_nli.py), OFF at 0: NUMBER_CHECK reads every number the answer
#: states against what it was shown (no model); NLI_ON reads each sentence of the answer against its premises
#: through the NLI server on :8086 (tools/nli_server.py). Either makes the attribution unverified when it finds
#: a number the record never said or a claim the record contradicts.
NUMBER_CHECK = 0
NLI_ON = 0
TUNABLES = {
    "LEXICAL_KEEP":   {"min": 1,    "max": 3,    "what": "identifier-recall hits kept after the embedding hits"},
    "LEXICAL_WINDOW": {"min": 1000, "max": 2500, "what": "characters around the identifier's first occurrence"},
    "CLAIM_BONUS":    {"min": 0.0,  "max": 0.10, "what": "lift for an authority page on a proof-status question"},
    "CLAIM_PENALTY":  {"min": 0.05, "max": 0.25, "what": "demotion for a design page on a proof-status question"},
    "RERANK_ON":      {"min": 0,    "max": 1,    "what": "1: a cross-encoder (llama-server --reranking on :8085) orders and floors the embedding hits"},
    "RERANK_FLOOR":   {"min": 0.05, "max": 0.9,  "what": "the cross-encoder relevance below which an embedding hit is dropped"},
    "BM25_KEEP":      {"min": 0,    "max": 3,    "what": "BM25 chunks kept after the identifier hits; 0 leaves BM25 out"},
    "NUMBER_CHECK":   {"min": 0,    "max": 1,    "what": "1: every number the answer states must appear in what it was shown"},
    "NLI_ON":         {"min": 0,    "max": 1,    "what": "1: a small NLI model (tools/nli_server.py on :8086) reads each claim against its premises"},
}
TUNABLES_FILE = os.path.join(ROOT, "config", "brain_tunables.json")
RERANK_ON = 0
RERANK_FLOOR = 0.3
RERANK_URL = "http://127.0.0.1:8085/v1/rerank"


def crossencoder_scores(question, texts, url=RERANK_URL, timeout=120):
    """
    """
    import urllib.request
    body = json.dumps({"model": "reranker", "query": question, "documents": list(texts), "top_n": len(texts)}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
    except Exception:
        return None
    rows = r.get("results") if isinstance(r, dict) else r
    out = [None] * len(texts)
    for x in rows or []:
        try:
            out[int(x["index"])] = float(x.get("relevance_score", x.get("score")))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out if out and all(v is not None for v in out) else None



def tunable(key, root=None):
    """The live value of one tunable: the file's, when it is set and inside the range, else the module constant
    of the same name -- the constant IS the default, so there is one description of it. A value outside its
    range is IGNORED and the default used: a trainer that writes past the fence changes nothing, and
    tunables_check() names the fault."""
    spec = TUNABLES[key]
    default = globals()[key]
    path = TUNABLES_FILE if root is None else os.path.join(root, "config", "brain_tunables.json")
    try:
        with open(path, encoding="utf-8") as fh:
            v = (json.load(fh).get("values") or {}).get(key)
    except (OSError, ValueError, AttributeError):
        return default
    if v is None:
        return default
    try:
        v = type(default)(v)
    except (TypeError, ValueError):
        return default
    return v if spec["min"] <= v <= spec["max"] else default


def tunables_check(root=None):
    """Every fault in the tunables file, as sentences; empty when the file is absent or clean."""
    path = TUNABLES_FILE if root is None else os.path.join(root, "config", "brain_tunables.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            values = json.load(fh).get("values") or {}
    except (OSError, ValueError, AttributeError) as e:
        return ["config/brain_tunables.json is not readable: %s" % e]
    out = []
    for k, v in values.items():
        if k not in TUNABLES:
            out.append("%s is not a tunable; the tunables are %s" % (k, ", ".join(sorted(TUNABLES))))
        elif not (TUNABLES[k]["min"] <= v <= TUNABLES[k]["max"]):
            out.append("%s=%r is outside %r..%r and is ignored" % (k, v, TUNABLES[k]["min"], TUNABLES[k]["max"]))
    return out


def claim_class_of_source(source):
    """authority / evidence / design for a chunk, from its path; None when the path says nothing."""
    s = str(source or "")
    for rx, cls in _CLASS_BY_PATH:
        if rx.search(s):
            return cls
    return None


def asks_about_proof(question):
    return bool(_ASKS_ABOUT_PROOF.search(str(question or "")))


PLACE_PENALTY = 0.15
#: A small lift for a chunk whose place is the one asked about, so the right host is preferred and not merely
#: the wrong one demoted.
PLACE_BONUS = 0.05


def place_of_source(source):
    """board / twin / timeless for a chunk, from its path alone. Unknown paths are timeless, never guessed."""
    s = str(source or "")
    for rx, place in _PLACE_BY_PATH:
        if rx.search(s):
            return place
    return ANY


def place_of_question(question):
    """The place a question is explicitly about, or None. Silence means no reordering by place.

    """
    q = str(question or "")
    places = []
    for rx, place in _PLACE_BY_QUESTION:
        if rx.search(q) and place not in places:
            places.append(place)
    return places[0] if len(places) == 1 else None


SELF_EVIDENCE = ("config/skills/answer_from_knowledge/",)
SELF_PENALTY = 1.0


def is_self_evidence(source):
    s = _norm(source)
    return any(p in s for p in SELF_EVIDENCE)


def rerank(question, hits):
    """Reorder hits, returning (hit, adjusted_similarity, reason) highest first.

    """
    want = place_of_question(question)
    proof = asks_about_proof(question)
    out = []
    for h in hits:
        sim = float(h.get("similarity") or 0.0)
        place = place_of_source(h.get("source"))
        adj, why = sim, "place %s, nothing to compare" % place
        if want is None:
            why = "the question names no place; order unchanged"
        elif place == ANY:
            why = "timeless (%s); applies to any host" % place
        elif place == want:
            adj = sim + PLACE_BONUS
            why = "about %s, which is what was asked" % place
        else:
            adj = sim - PLACE_PENALTY
            why = "about %s, but the question is about %s" % (place, want)
        if is_self_evidence(h.get("source")):
            adj -= SELF_PENALTY
            why = "the answering layer's own code; never evidence"
        if proof:
            cls = claim_class_of_source(h.get("source"))
            if cls == AUTHORITY:
                adj += tunable("CLAIM_BONUS")
                why += "; the authority on proof status, lifted"
            elif cls == DESIGN:
                adj -= tunable("CLAIM_PENALTY")
                why += "; design intent, demoted for a proof-status question"
        out.append((h, adj, why))
    out.sort(key=lambda t: -t[1])
    return out


def min_confidence(root=None):
    """The retrieval floor, read from byxin/config/cerebellum.json -- the same file the C++ reads.

    Two readers of one config, not two copies of one number. The value there is measured and carries its own
    note; restating it here would give this project two thresholds that drift apart silently, which is the
    failure its own rules name first.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    base = root or os.path.dirname(here)
    try:
        with open(os.path.join(base, "byxin", "config", "cerebellum.json"), encoding="utf-8") as fh:
            return float((json.load(fh).get("rag") or {}).get("min_confidence", 0.60))
    except Exception:
        return 0.60


def retrieval_char_budget(root=None):
    """How many characters of retrieved text may be injected, derived the way the C++ derives it.

    byxin/include/byxin/retrieval_budget.h takes one seventh of the usable window (num_ctx - num_predict) for
    retrieval, and byxin_worldmodel counts 3.0 characters per token. Both numbers come from byxin.json's
    world_model block, which is the single place they are set.

    MEASURED 2026-09-30, and this function exists because assembling the block here dropped that budget. Taking
    8 chunks produced 41137 characters against a whole-prompt budget of 43008, leaving about 1200 for the
    instruction, the lesson and the answer -- and the bench's instability rose from 2 unstable questions out of
    10 to 4, because the model was working at the very edge of its context where small differences flip the
    outcome. Reimplementing an assembly without its budget is the one-description failure this project keeps
    finding; this is the budget coming back.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    base = root or os.path.dirname(here)
    ctx, predict = 16384, 2048
    try:
        with open(os.path.join(base, "byxin", "config", "byxin.json"), encoding="utf-8") as fh:
            wm = json.load(fh).get("world_model") or {}
        ctx = int(wm.get("context_tokens", ctx))
        predict = int((wm.get("budget") or {}).get("answer_reserve_tokens", predict))
    except Exception:
        pass
    usable = max(1, ctx - predict)
    # HALF the usable window, not the C++ path's one seventh, and the difference was measured.
    #
    # retrieval_budget.h takes 1/7 because the path it serves also carries a large system prompt. This layer's
    # own prompt is about 2 KB -- an instruction and at most a lesson -- so the same share starves it. With 1/7
    # (6144 chars) exactly ONE chunk fits, because chunks run to 6000 characters, and the brain cannot compare
    # or combine two sources: measured 1 right, 7 honest, 2 wrong. With no budget at all, 8 chunks filled 41137
    # of 43008 characters, the model worked at the edge of its window, and instability doubled from 2 unstable
    # questions to 4 -- measured 3 right, 1 wrong, but too noisy to claim.
    #
    # Half leaves room for several chunks and keeps the whole prompt well short of the window. The real fix is
    # smaller chunks (ledger #8 -- BUILT 2026-09-30 late: CHUNK_WINDOW is 3000 now, so this budget holds seven
    # chunks and the 79 per-rebuild embedder rejections of dense text went to zero): a 6000-character chunk was most of any sane retrieval budget by itself, which
    # is why this number matters at all.
    return int(usable / 2.0 * 3.0)


#: An identifier-shaped token: shield.en_pull, smart_agent, byxin.json, gate_logic.h -- something with an
#: underscore, or a dotted name with letters on both sides. Not "e.g.", not "2.6", not "qwen2.5".
#: ... and a hyphenated word of two real parts (self-modified, machine-checked, verdict-only): measured
#: 2026-09-30, "self-modified" occurs word-exact in ONE watched file, at the heading of the section that
#: answers "what must never be self-modified", and that section was absent from the embedding's top forty.
_IDENT = re.compile(r"\b(?:[A-Za-z]\w{2,}\.[A-Za-z]\w{2,}(?:\.[A-Za-z]\w+)*|[A-Za-z]\w*_\w+(?:\.[A-Za-z]\w*)?|"
                    r"[A-Za-z]{3,}-[A-Za-z]{3,}(?:-[A-Za-z]{2,})*)\b")
LEXICAL_WINDOW = 1500
_GREP_FAILURE_REPORTED = False


def _norm(p):
    return str(p or "").replace("\\", "/").lower()


#: An exact token an embedding smears and a grep does not: a date, or a number of three digits or more (a step
#: count, a port, a similarity threshold's digits). Years alone are too common to anchor on.
_EXACT = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d+\.\d+|\d{3,})\b")
#: A git short hash: seven to forty hex characters with at least one digit and one letter -- 128062a names one
#: commit exactly, and the examiner's leak check must see it (2026-10-01).
_HASH = re.compile(r"\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_QUOTED = re.compile(u"[\"'‘’“”]([^\"'‘’“”]{12,160})[\"'‘’“”]")


_STOP = {"what", "which", "where", "when", "does", "this", "that", "there", "their", "with", "from", "into",
         "about", "right", "have", "been", "being", "were", "will", "would", "could", "should", "answer",
         "answers", "byxin", "project", "mean", "means", "precisely", "sentence", "sentences",
         "explain", "name", "give", "tell", "please", "exactly", "really", "actually", "still"} | set(_world.stop_words())


def _content_words(question):
    """The question's own words worth counting in a candidate file: four letters or more, not filler."""
    out = []
    for w in re.findall(r"[a-z][a-z0-9_-]{3,}", (question or "").lower()):
        if w not in _STOP and w not in out:
            out.append(w)
    return out


def identifiers_in(question):
    """The identifier-shaped tokens a question names, in order, once each."""
    out = []
    for m in _IDENT.finditer(question or ""):
        t = m.group(0).rstrip(".")
        if t not in out:
            out.append(t)
    return out


#: A CamelCase name (LoRA, MiniLM, GateHold) is an exact token; the project's own names are in every question
#: and anchor nothing.
_CAMEL = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]*\b")
_NOT_ANCHORS = set(_world.not_anchors())


def exact_tokens_in(question):
    """Identifiers, hyphenated words, CamelCase names, dates, decimals, numbers of three or more digits, and
    quoted phrases -- the tokens worth looking up by their letters."""
    out = []
    for m in _CAMEL.finditer(question or ""):
        t = m.group(0)
        if t not in _NOT_ANCHORS and t not in out:
            out.append(t)
    for m in _QUOTED.finditer(question or ""):
        phrase = " ".join(m.group(1).split())
        if len(phrase.split()) >= 3 and phrase not in out:
            out.append(phrase)
    out += [t for t in identifiers_in(question) if t not in out]
    for m in _HASH.finditer(question or ""):
        if m.group(0) not in out:
            out.append(m.group(0))
    for m in _EXACT.finditer(question or ""):
        t = m.group(0)
        if re.fullmatch(r"\d{4}", t):
            continue                                   # a bare year anchors nothing
        if t not in out:
            out.append(t)
    return out


def _watched_dirs(root=None):
    root = root or repo_root()
    try:
        cfg = json.load(io.open(os.path.join(root, "byxin", "config", "cerebellum.json"), encoding="utf-8"))
        return [d for d in (cfg.get("watch_directories") or []) if os.path.isdir(os.path.join(root, d))]
    except Exception:
        return []


def identifier_hits(question, floor, root=None, max_files=3):
    """
    """
    root = root or repo_root()
    hits = []
    dirs = _watched_dirs(root)
    tokens = exact_tokens_in(question)
    if not dirs or not tokens:
        return hits
    window = tunable("LEXICAL_WINDOW")
    # Which tracked, watched files contain which tokens. A file is ranked by how MANY of the question's exact
    # tokens it contains (395 and 392 and 2026-09-21 together name one evidence note; 392 alone names dozens
    # of source files), then prose before code, then by name.
    holders = {}
    for tok in tokens:
        # A phrase is matched as text (-F), a single token as a whole word (-w): "go red" inside "must be
        # able to go red" is the phrase found, not a word broken.
        wflag = [] if " " in tok else ["-w"]
        try:
            out = subprocess.run(["git", "-C", root, "grep", "-l", "-F", "-i"] + wflag + ["--", tok] + dirs,
                                 capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except Exception as e:
            # Said, not swallowed: under a pythonw parent this spawn died with WinError 6 and identifier recall
            # silently vanished from every answer the background self-test measured (2026-09-30).
            global _GREP_FAILURE_REPORTED
            if not _GREP_FAILURE_REPORTED:
                _GREP_FAILURE_REPORTED = True
                sys.stderr.write("[byxin_rerank] identifier recall unavailable: git grep failed: %s: %s\n"
                                 % (type(e).__name__, e))
            continue
        for f in out.splitlines():
            f = f.strip()
            if f:
                holders.setdefault(f, []).append(tok)
    words = _content_words(question)
    texts = {}
    for rel in holders:
        try:
            texts[rel] = io.open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            texts[rel] = ""
    def word_hits(rel):
        low = texts[rel].lower()
        return sum(1 for w in words if w in low)
    files = sorted(holders, key=lambda f: (-len(holders[f]), 0 if claim_class_of_source(f) == AUTHORITY else 1,
                                           0 if f.lower().endswith((".md", ".txt")) else 1, -word_hits(f), f))
    files = [f for f in files if not is_self_evidence(f)]
    for rel in files[:max_files]:
        text = texts.get(rel) or ""
        path = os.path.join(root, rel)
        held = holders[rel]
        i = text.lower().find(held[0].lower())
        if i < 0:
            continue
        if len(text) <= 2 * window:
            a, b = 0, len(text)
        else:
            a = text.rfind("\n", 0, max(0, i - window // 2)) + 1
            b = text.find("\n", i + window // 2)
            b = len(text) if b < 0 else b
        hits.append({"source": path.replace("/", os.sep), "content": text[a:b].strip(),
                     "similarity": round(floor + 0.001, 4), "lexical": held[0],
                     "why_lexical": "lexical: the question names %s and this file contains %s"
                                    % (", ".join(held), "all of them" if len(held) == len(tokens) else
                                       "%d of %d" % (len(held), len(tokens)))})
    return hits


_BM25_FAILURE_REPORTED = False


def bm25_hits(question, floor, keep, found=None, root=None):
    """BM25 RECALL, the sibling of identifier recall (tools/byxin_bm25.py; HORIZON_SCAN_2026-10 §8 item 1).
    Called only when the tunable BM25_KEEP is above 0. The top `keep` BM25 chunks whose file is not already
    among the hits (`found`, keyed by _norm) join at the floor plus a hair, explained as lexical and naming the
    terms they matched, so a reader can tell them from an embedding match. Not a rank fusion: the floor is
    applied to adjusted similarity and a fused rank has none (the why is in byxin_bm25's docstring)."""
    if keep <= 0:
        return []
    found = {} if found is None else found
    try:
        try:
            import byxin_bm25
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import byxin_bm25
        ranked = byxin_bm25.query(question, k=keep + len(found) + 4, root=root)
    except Exception as e:
        # Said, not swallowed, like identifier recall's grep failure above.
        global _BM25_FAILURE_REPORTED
        if not _BM25_FAILURE_REPORTED:
            _BM25_FAILURE_REPORTED = True
            sys.stderr.write("[byxin_rerank] bm25 recall unavailable: %s: %s\n" % (type(e).__name__, e))
        return []
    out = []
    for r in ranked:
        key = _norm(r.get("source"))
        if key in found:
            continue
        terms = [str(t) for t in (r.get("terms") or [])]
        h = {"source": r["source"], "content": r.get("content") or "", "similarity": round(floor + 0.001, 4),
             "lexical": "bm25:" + ",".join(terms), "why_lexical": "lexical (bm25): matched %s" % ", ".join(terms)}
        found[key] = h
        out.append(h)
        if len(out) >= keep:
            break
    return out


def retrieve(question, top_k=12, max_chunks=8, url="http://127.0.0.1:8080/"):
    """Retrieve, rerank by place, apply the floor, and assemble the block. Returns (block, kept, top_adjusted).

    The floor is applied to the ADJUSTED score on purpose: a chunk about the wrong host has been demoted
    precisely because it should not be treated as though it answered the question, and letting it back in under
    its raw similarity would undo the reranking at the last step.

    The block is assembled here rather than taken from cerebellum.query, because query returns text already
    ordered and cut by similarity alone -- the thing this module exists to correct. Its shape follows the C++
    formatter's so a reader sees one format, and the CONTENT comes from the same index either way.
    """
    hits = _lookup(question, top_k, url=url)
    floor = min_confidence()
    # Identifier recall: a file that contains the identifier the question names joins the hits at the floor,
    # unless the embedding already found that file, in which case the embedding's own score stands.
    found = {_norm(h.get("source")): h for h in hits}
    for lh in identifier_hits(question, floor):
        key = _norm(lh["source"])
        prior = found.get(key)
        if prior is None or float(prior.get("similarity") or 0.0) < floor:
            hits.append(lh)
            found[key] = lh
    # BM25 recall joins after identifier recall, only when its tunable says so (0 by default: with BM25_KEEP at
    # 0 this function's output is what it was before BM25 existed). bm25_hits() records what it adds in `found`.
    keep_bm25 = tunable("BM25_KEEP")
    if keep_bm25 > 0:
        hits.extend(bm25_hits(question, floor, keep_bm25, found))
    if not hits:
        return "", [], 0.0
    budget = retrieval_char_budget()
    # Identifier hits are capped by LEXICAL_KEEP and BM25 hits by BM25_KEEP; both are lexical hits and share the
    # reserved room and the tail position.
    lexical = [h for h in hits if h.get("lexical") and not str(h["lexical"]).startswith("bm25:")][:tunable("LEXICAL_KEEP")]
    lexical += [h for h in hits if str(h.get("lexical") or "").startswith("bm25:")][:keep_bm25]
    # Room is RESERVED for identifier recall. Measured 2026-09-30: the lexical hit for shield.en_pull was made,
    # ranked last at the floor, and never kept -- four embedding chunks of 4-6 KB filled the budget and the loop
    # stopped at the first one that did not fit, so the 1.5 KB window behind it was never reached.
    reserve = sum(len(h.get("content") or "") + 80 for h in lexical)
    kept = []
    used = 0
    ranked = rerank(question, hits)
    # THE CROSS-ENCODER (RERANK_ON, off by default): the embedding hits are ordered and floored by a reranker
    # that reads the question against each chunk, not by cosine alone; an unreachable server keeps the
    # embedding order and says so. Identifier and BM25 hits stay in their reserved room either way.
    rerank_note = None
    if tunable("RERANK_ON"):
        cands = [(h, adj, why) for h, adj, why in ranked if not h.get("lexical")]
        scores = crossencoder_scores(question, [h.get("content") or "" for h, _a, _w in cands]) if cands else []
        if scores is None:
            rerank_note = "cross-encoder unreachable; embedding order kept"
        else:
            rf = tunable("RERANK_FLOOR")
            scored = sorted(zip(cands, scores), key=lambda t: -t[1])
            ranked = [(h, sc, "cross-encoder relevance %.3f; %s" % (sc, why)) for (h, adj, why), sc in scored if sc >= rf]
            ranked += [(h, adj, why) for h, adj, why in rerank(question, hits) if h.get("lexical")]
            floor = min(floor, rf)
    for h, adj, why in ranked:
        if h.get("lexical"):
            continue                                   # placed after the embedding hits, below
        if adj < floor:
            continue
        piece = len(h.get("content") or "") + 80      # the Source/confidence header costs about 80 characters
        if kept and used + piece > budget - reserve:
            continue                                   # this one does not fit; a smaller one further down may
        kept.append((h, adj, why))
        used += piece
        if len(kept) >= max_chunks:
            break
    shown = {_norm(h.get("source")) for h, _a, _w in kept}
    for h in lexical:
        piece = len(h.get("content") or "") + 80
        if _norm(h.get("source")) in shown or used + piece > budget:
            continue
        kept.append((h, h["similarity"], h["why_lexical"]))
        used += piece
    if not kept:
        return "", [], 0.0
    parts = []
    for h, adj, why in kept:
        parts.append((_world.knowledge_header() + "\nSource: %s\n\n%s")
                     % (adj, h.get("source") or "?", (h.get("content") or "").strip()))
        # The kept hit carries its own reranking verdict, so a caller assembling a participation record can say
        # not only WHICH chunks took part but WHY they were placed where they were.
        h["adjusted"] = adj
        h["why"] = why
    if rerank_note:
        for h, _a, _w in kept:
            h["why"] = (h.get("why") or "") + "; " + rerank_note
    return "\n\n".join(parts), [h for h, _a, _w in kept], kept[0][1]


def _lookup(question, top_k, url="http://127.0.0.1:8080/", timeout=120):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "cerebellum.lookup",
                       "params": {"query": question, "top_k": top_k, "min_similarity": 0.0}})
    raw = subprocess.run(["curl", "-s", "--max-time", str(timeout), "-X", "POST", url,
                          "-H", "Content-Type: application/json", "-d", body],
                         capture_output=True, timeout=timeout + 30, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or b""
    return (json.loads(raw.decode("utf-8", "replace")).get("result") or {}).get("results") or []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--explain", metavar="QUESTION", required=True)
    ap.add_argument("--top-k", type=int, default=12)
    a = ap.parse_args(argv)
    hits = _lookup(a.explain, a.top_k)
    if not hits:
        print("no hits -- nothing to rerank")
        return 1
    want = place_of_question(a.explain)
    print("the question is about: %s\n" % (want or "no place named"))
    print("%-4s %-4s %-7s %-7s %-44s %s" % ("was", "now", "sim", "adj", "source", "why"))
    ranked = rerank(a.explain, hits)
    order = {id(h): i for i, h in enumerate(hits)}
    for new_i, (h, adj, why) in enumerate(ranked):
        print("%-4d %-4d %-7.3f %-7.3f %-44s %s"
              % (order[id(h)] + 1, new_i + 1, float(h.get("similarity") or 0), adj,
                 str(h.get("source"))[-44:], why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
