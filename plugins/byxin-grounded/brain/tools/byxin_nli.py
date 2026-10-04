#!/usr/bin/env python3
"""The claim comparator: two lenses on an answer, after the citation comparator and before speech.

NUMBERS (deterministic, no model): every number the answer states must appear in what the answer was shown --
the question, the lessons, the self-report, the retrieved facts and the names of the sources. A number the
record never said is the commonest fabrication in this domain (a millisecond that became a second, a step
count, a short hash), and it is exactly where a small NLI model is blind: measured 2026-10-01, the xsmall NLI
model called "PERMIT 0 de-energizes in 8 seconds" ENTAILED by a sentence saying 0.8 ms. So numbers are read by
eye, not by a model.

CLAIMS (a small NLI cross-encoder): each sentence of the answer is a hypothesis and the lessons, the
self-report and the retrieved chunks are the premises. A claim some premise ENTAILS is supported; a claim no
premise entails and the best premise CONTRADICTS is contradicted; the rest are neutral -- said, but not in the
record as shown. school/research/DEEP_RESEARCH_RECENT_2026-10.md item 2 (2608.15574, 2026-08-16): the small
NLI verifier was the stable one and the LLM-as-judge swung 0-100% with phrasing. Red check 2026-10-01 on five
pairs (two true, two fabricated, one unrelated): cross-encoder/nli-deberta-v3-xsmall 3/5, nli-deberta-v3-base
4/5, MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli 5/5 -- the last is what tools/nli_server.py serves.

Both lenses are behind tunables (tools/byxin_rerank.py: NUMBER_CHECK, NLI_ON; 0 by default) and the bench
judges them like any other knob. An NLI server that does not answer changes nothing and says so.
SPDX-License-Identifier: BSD-2-Clause
"""
import json
import re

NLI_URL = "http://127.0.0.1:8086/v1/nli"
ENTAILED_AT = 0.5        # the best premise's entailment probability at or above which a claim is supported
CONTRADICTED_AT = 0.9    # the best premise's contradiction probability at or above which an unsupported claim is contradicted
HALF_SUPPORTED_AT = 0.2  # a claim some premise entails this much is at most neutral, never contradicted (replay 2026-10-01:
                         # guard_not_flashed's right answer read ent 0.43 by one chunk and con 0.95 by another)
PREMISE_WINDOW = 1800    # characters; the model reads 512 tokens, so a long chunk is read in overlapping windows
PREMISE_OVERLAP = 300
MAX_PREMISES = 24
MIN_CLAIM_CHARS = 25

# A number: digits with optional thousands commas and a decimal part, not glued to a word on either side nor
# to a dot that continues a number (so 242d44e5 and v8.1 are not numbers; 3,829.6, 0.8, 16-235's halves and a
# sentence-final "392." are -- the first draft of this regex refused the trailing full stop, so a number the
# evidence stated at the end of a sentence read as unsupported when the answer stated it mid-sentence).
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?!\w|\.\d)")
# A short hash: 7..40 hex characters with at least one digit, as a word of its own.
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_NOTE = re.compile(r"\[attribution check:.*?\]", re.S)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9]|[-*•]\s)|\n+")
_REFUSAL = ("cannot see that in the", "i don't know", "i do not know", "not in the record")
_PROVENANCE = re.compile(r"\b(i was told|was told,? by|this information|the source for|stated in the|derived from|"
                         r"according to the (record|document|evidence)|in the (record|document|evidence)\b|"
                         r"the (record|document|evidence) (says|states|shows|calls|marks)|marked as\b|"
                         r"computed from my own record|live self-report|live development report)", re.I)


def numbers_of(text):
    """The numbers and short hashes a text states, normalised (thousands commas removed), in order, once each."""
    out = []
    for m in _NUM.finditer(text or ""):
        v = m.group(1).replace(",", "")
        if v and v not in out:
            out.append(v)
    for m in _HEX.finditer((text or "").lower()):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def compare_numbers(answer, shown_text):
    """Every number the answer states against every number in what it was shown."""
    stated = numbers_of(_NOTE.sub("", answer or ""))
    seen = set(numbers_of(shown_text or ""))
    return {"numbers": stated, "unsupported_numbers": [n for n in stated if n not in seen]}


def claims_of(answer):
    """The sentences of an answer that make a claim: the attribution note, refusals and fragments left out."""
    text = _NOTE.sub("", answer or "")
    out = []
    for s in _SPLIT.split(text):
        s = s.strip().lstrip("-*• ").strip()
        if len(s) < MIN_CLAIM_CHARS or any(r in s.lower() for r in _REFUSAL) or _PROVENANCE.search(s):
            continue
        if s not in out:
            out.append(s)
    return out


def premises_of(texts):
    """The premises the model reads: every non-empty text, long ones in overlapping windows, at most MAX_PREMISES."""
    out = []
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        if len(t) <= PREMISE_WINDOW:
            out.append(t)
            continue
        i = 0
        while i < len(t):
            out.append(t[i:i + PREMISE_WINDOW])
            if i + PREMISE_WINDOW >= len(t):
                break
            i += PREMISE_WINDOW - PREMISE_OVERLAP
    return out[:MAX_PREMISES]


def nli_scores(pairs, url=NLI_URL, timeout=180):
    """{entailment, neutral, contradiction} for each (premise, hypothesis) pair from the NLI server, aligned with
    pairs, or None when the server is away or answers in another shape."""
    import urllib.request
    body = json.dumps({"pairs": [[p, h] for p, h in pairs]}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
    except Exception:
        return None
    rows = r.get("results") if isinstance(r, dict) else None
    out = [None] * len(pairs)
    for x in rows or []:
        try:
            out[int(x["index"])] = {k: float(x[k]) for k in ("entailment", "neutral", "contradiction")}
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out if out and all(v is not None for v in out) else None


def compare_claims(answer, premise_texts, scores=nli_scores, entailed_at=ENTAILED_AT, contradicted_at=CONTRADICTED_AT,
                   half_supported_at=HALF_SUPPORTED_AT):
    """Each claim of the answer against the premises: entailed, contradicted or neutral, with the best premise's
    probabilities kept so a reader can see how close the call was. Contradicted means no premise so much as
    half-supports it AND the best premise contradicts it outright; a claim one chunk half-supports and another
    contradicts is neutral -- the record disagrees with itself, the answer is not at fault."""
    claims = claims_of(answer)
    premises = premises_of(premise_texts)
    if not claims or not premises:
        return {"claims": len(claims), "entailed": [], "contradicted": [], "neutral": [], "nli": "nothing to read"}
    pairs = [(p, c) for c in claims for p in premises]
    sc = scores(pairs)
    if sc is None:
        return {"claims": len(claims), "entailed": [], "contradicted": [], "neutral": [], "nli": "unreachable"}
    out = {"claims": len(claims), "entailed": [], "contradicted": [], "neutral": [], "nli": "read", "detail": []}
    n = len(premises)
    for i, c in enumerate(claims):
        rows = sc[i * n:(i + 1) * n]
        best_ent = max(r["entailment"] for r in rows)
        best_con = max(r["contradiction"] for r in rows)
        if best_ent >= entailed_at:
            kind = "entailed"
        elif best_con >= contradicted_at and best_ent < half_supported_at:
            kind = "contradicted"
        else:
            kind = "neutral"
        out[kind].append(c)
        out["detail"].append({"claim": c[:160], "kind": kind, "entailment": round(best_ent, 3), "contradiction": round(best_con, 3)})
    return out
