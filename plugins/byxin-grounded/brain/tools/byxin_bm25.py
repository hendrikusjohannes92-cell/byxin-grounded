#!/usr/bin/env python3
"""byxin_bm25.py -- lexical recall by BM25 over the watched corpus, chunked the way the cerebellum chunks.


WHAT IS HERE.
  * an index over the watched, git-tracked text files (byxin/config/cerebellum.json watch_directories -- the
    list the cerebellum ingests and identifier recall greps, read through byxin_rerank._watched_dirs so there
    is one reader of it), chunked as byxin/src/cerebellum.cpp chunks: CHUNK_WINDOW 3000 characters,
    CHUNK_OVERLAP 400, a whole file when it fits, cuts snapped to line boundaries;
  * standard BM25 (k1 = 1.2, b = 0.75, length-normalised). §8a names the trap: over chunks of unequal length an
    un-normalised score favours the long chunk that says "port" nine times;
  * tokens: lower-cased words, and identifier-shaped tokens kept whole -- shield.en_pull, smart_agent,
    2026-09-21, 128062a, qwen2.5 are single terms. The INDEX also carries each identifier's parts, so a question
    saying "en_pull" reaches a chunk saying "shield.en_pull"; the QUESTION is taken at its word, whole tokens
    only, so a fabricated identifier matches nothing through its innocent-looking pieces;
  * a cache at data/bm25_index.json keyed by the tracked file list with every file's size and mtime, so a
    rebuild happens only when a file changed: the build takes seconds, a query milliseconds;
  * query(question, k=5, root=None) -> [{"source", "content", "score", "terms"}], and [] for a question whose
    terms occur nowhere -- the green must be able to go red.


"""
import argparse
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import byxin_rerank as R  # noqa: E402

#: The cerebellum's chunk geometry, byxin/src/cerebellum.cpp CHUNK_WINDOW / CHUNK_OVERLAP. That one is a C++
#: constexpr, so this is a second copy of the number -- the test reads the C++ source and holds the two equal.
CHUNK_WINDOW = 3000
CHUNK_OVERLAP = 400
#: Standard BM25 parameters (Robertson et al.); b = 0.75 is the length normalisation §8a asks for.
K1 = 1.2
B = 0.75
#: Skipped without reading: a file over this size, or one whose first bytes hold a NUL, or one of these
#: extensions. .hex is text (Intel HEX) but it is a firmware image, not knowledge: its tokens can match a
#: question only by accident.
MAX_FILE_BYTES = 2 * 1024 * 1024
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".bin", ".hex", ".elf", ".img",
               ".xlsx", ".xls", ".pdf", ".zip", ".gz", ".7z", ".gguf", ".db", ".sqlite", ".exe", ".dll", ".so",
               ".o", ".a", ".pyc", ".wav", ".mp3", ".mp4", ".woff", ".woff2", ".ttf"}
#: Never evidence: the school's ledgers and saved runs, and the answering layer's OWN source -- its docstrings
#: carry the bench's questions, wrong answers and the epistemic frame, and a question about ByxIn would find
#: its own instrument (review 2026-10-01: handler.py was the lexical hit for twin_vs_metal_origin).
_NEVER = ("school/", "tools/bench_runs/", "config/skills/answer_from_knowledge/")
CACHE_REL = os.path.join("data", "bm25_index.json")
#: Bumped when the tokenizer, the chunker or the cache layout changes, so a cache built by an older rule is
#: rebuilt rather than trusted.
INDEX_FORMAT = 1
#: How long an in-process index is trusted before the corpus key is recomputed (one git spawn plus a stat per
#: file, tens of milliseconds). The answering layer asks seconds apart; a test that changes a file passes 0.
MEMO_SECONDS = 5.0

_MEMO = {}
_FAILURE_REPORTED = False


# ── tokens ─────────────────────────────────────────────────────────────────────────────────────────────────────

#: A token: a run of word characters, joined by single dots or hyphens into one term when both sides are word
#: characters. shield.en_pull, smart_agent, 2026-09-21, 128062a, qwen2.5, byxin.json, self-modified, 0.60 are
#: each one token; a sentence-ending dot is not part of the token before it.
_TOKEN = re.compile(r"[a-z0-9_]+(?:[.\-][a-z0-9_]+)*")
_SPLIT_JOINERS = re.compile(r"[.\-]")
_SPLIT_ALL = re.compile(r"[.\-_]")


def tokens(text, expand=True):
    """The terms of a text, lower-cased, in order. With expand (the index side) an identifier-shaped token is
    followed by its parts -- shield.en_pull also yields shield, en_pull, en, pull -- so a question can reach it
    by any name it has. Without (the question side) a token is looked up exactly as written."""
    out = []
    for m in _TOKEN.finditer((text or "").lower()):
        t = m.group(0)
        if len(t) < 2:
            continue
        out.append(t)
        if expand and ("." in t or "-" in t or "_" in t):
            seen = {t}
            for part in _SPLIT_JOINERS.split(t) + _SPLIT_ALL.split(t):
                if len(part) >= 2 and part not in seen:
                    seen.add(part)
                    out.append(part)
    return out


# ── chunks ─────────────────────────────────────────────────────────────────────────────────────────────────────

def chunks_of(text):
    """(start, end) character offsets of the chunks of one text: the cerebellum's geometry with the cuts snapped
    to line boundaries. A text that fits in CHUNK_WINDOW is one chunk. Otherwise a window of CHUNK_WINDOW
    characters whose END moves back to the last line end in the window's second half; the next window begins
    CHUNK_OVERLAP characters before that end, and its START moves forward to the first line start at or before
    the previous end. So every chunk begins at a line start and ends at a line end, no character falls between
    two chunks, and no chunk exceeds CHUNK_WINDOW. A line longer than half the window is cut where the window
    falls, as the C++ does (a markdown paragraph is one line, and one of 1500 characters is rare).

    Stepping from the snapped end rather than by a fixed stride is what keeps this gap-free: with a fixed stride
    a window that snapped back by more than the overlap would leave the characters between the two uncovered."""
    n = len(text)
    if n == 0:
        return []
    if n <= CHUNK_WINDOW:
        return [(0, n)]
    out = []
    pos = 0
    prev_end = 0
    while pos < n:
        start = pos
        if pos > 0 and text[pos - 1] != "\n":
            j = text.find("\n", pos, prev_end + 1)
            if j >= 0:
                start = j + 1
        end = min(pos + CHUNK_WINDOW, n)
        if end < n:
            j = text.rfind("\n", max(start, pos + CHUNK_WINDOW // 2), end)
            if j > start:
                end = j
        if end > start:
            out.append((start, end))
            prev_end = end
        if end >= n:
            break
        pos = max(pos + 1, end - CHUNK_OVERLAP)
    return out


# ── the corpus and its key ─────────────────────────────────────────────────────────────────────────────────────

def _run_git(root, args, timeout=60):
    return subprocess.run(["git", "-C", root] + args, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def tracked_files(root, dirs):
    """The git-tracked files under the watched directories, as the cerebellum and identifier recall see them."""
    if not dirs:
        return []
    p = _run_git(root, ["ls-files", "-z", "--"] + list(dirs))
    if p.returncode != 0:
        raise RuntimeError("git ls-files failed in %s: %s" % (root, p.stderr.decode("utf-8", "replace").strip()))
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f]


def eligible(rel):
    """Whether a tracked path may be indexed at all: not an exam paper, not a binary by its name."""
    r = rel.replace("\\", "/")
    if r.startswith(_NEVER):
        return False
    return os.path.splitext(r)[1].lower() not in _BINARY_EXT


def corpus_key(root=None):
    """(key, files): a digest of every indexable file's path, size and mtime plus the index format and geometry,
    and the list of those files. The same tree gives the same key; one changed byte in one file, one added or
    removed file, or a new chunk rule gives another. `git rev-parse HEAD:` would not do: the working tree the
    cerebellum reads is not always the committed one."""
    root = root or R.repo_root()
    dirs = R._watched_dirs(root)
    h = hashlib.sha256()
    h.update(("bm25 format %d window %d overlap %d k1 %r b %r\n"
              % (INDEX_FORMAT, CHUNK_WINDOW, CHUNK_OVERLAP, K1, B)).encode("utf-8"))
    files = []
    for rel in tracked_files(root, dirs):
        if not eligible(rel):
            continue
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        if st.st_size > MAX_FILE_BYTES:
            continue
        h.update(("%s\0%d\0%d\n" % (rel, st.st_size, st.st_mtime_ns)).encode("utf-8", "replace"))
        files.append(rel)
    return h.hexdigest(), files


def read_text(path):
    """The text of one file as identifier recall reads it (utf-8, errors replaced, universal newlines), or None
    for a binary: a NUL in the first 8 KB."""
    try:
        with open(path, "rb") as fh:
            if b"\0" in fh.read(8192):
                return None
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ── build, cache, load ─────────────────────────────────────────────────────────────────────────────────────────

def build(root=None, key=None, files=None):
    """The index over the corpus: chunk table, postings and the average chunk length. Pure; nothing is written."""
    root = root or R.repo_root()
    if key is None or files is None:
        key, files = corpus_key(root)
    t0 = time.time()
    chunks = []                                        # [rel, start, end, length-in-tokens]
    postings = {}                                      # term -> [chunk, tf, chunk, tf, ...]
    indexed = 0
    for rel in files:
        text = read_text(os.path.join(root, rel))
        if text is None:
            continue
        indexed += 1
        for a, b in chunks_of(text):
            toks = tokens(text[a:b])
            if not toks:
                continue
            idx = len(chunks)
            tf = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            chunks.append([rel, a, b, len(toks)])
            for t, c in tf.items():
                postings.setdefault(t, []).extend((idx, c))
    avgdl = (sum(c[3] for c in chunks) / float(len(chunks))) if chunks else 0.0
    return {"format": INDEX_FORMAT, "key": key, "root": root, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "build_seconds": round(time.time() - t0, 3), "files": indexed, "window": CHUNK_WINDOW,
            "overlap": CHUNK_OVERLAP, "k1": K1, "b": B, "avgdl": avgdl, "chunks": chunks, "postings": postings}


def cache_path(root=None):
    return os.path.join(root or R.repo_root(), CACHE_REL)


def save(index, root=None):
    """Write the cache: whole file to a .tmp, then rename, so a reader never sees a torn index."""
    path = cache_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def _read_cache(root, key):
    try:
        with io.open(cache_path(root), encoding="utf-8") as fh:
            idx = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(idx, dict) or idx.get("format") != INDEX_FORMAT or idx.get("key") != key:
        return None
    return idx


def load(root=None, max_age=None, rebuild=False):
    """The index for a tree: the in-process one while it is fresh, else the cache when its key is the corpus's,
    else a new build, saved. `max_age` (seconds; MEMO_SECONDS by default) bounds how stale the in-process copy
    may be before the key is recomputed; 0 recomputes it now."""
    root = os.path.abspath(root or R.repo_root())
    if max_age is None:
        max_age = MEMO_SECONDS
    memo = _MEMO.get(root)
    now = time.time()
    if memo and not rebuild and now - memo["checked"] < max_age:
        return memo["index"]
    key, files = corpus_key(root)
    if memo and not rebuild and memo["index"].get("key") == key:
        memo["checked"] = now
        return memo["index"]
    index = None if rebuild else _read_cache(root, key)
    if index is None:
        index = build(root, key, files)
        save(index, root)
    _MEMO[root] = {"index": index, "checked": now}
    return index


# ── query ──────────────────────────────────────────────────────────────────────────────────────────────────────

def idf(n_containing, n_chunks):
    """Robertson/Sparck Jones idf with the +1 that keeps it positive: ln(1 + (N - n + 0.5) / (n + 0.5))."""
    return math.log(1.0 + (n_chunks - n_containing + 0.5) / (n_containing + 0.5))


def query(question, k=5, root=None, index=None):
    """The top k chunks for a question by BM25, each {"source": absolute path, "content": the chunk, "score",
    "terms": the question's terms found in it, strongest first}. [] when no term of the question occurs
    anywhere. A term found in more than half of all chunks is ignored: it separates nothing."""
    root = os.path.abspath(root or R.repo_root())
    if index is None:
        index = load(root)
    chunks = index.get("chunks") or []
    n_chunks = len(chunks)
    if not n_chunks:
        return []
    postings, avgdl = index["postings"], float(index.get("avgdl") or 1.0)
    qterms = []
    for t in tokens(question, expand=False):
        if t not in qterms:
            qterms.append(t)
    scores, contrib = {}, {}
    for t in qterms:
        plist = postings.get(t)
        if not plist:
            continue
        n = len(plist) // 2
        if 2 * n > n_chunks:
            continue
        w = idf(n, n_chunks)
        for i in range(0, len(plist), 2):
            idx, tf = plist[i], plist[i + 1]
            dl = chunks[idx][3]
            s = w * tf * (K1 + 1.0) / (tf + K1 * (1.0 - B + B * dl / avgdl))
            scores[idx] = scores.get(idx, 0.0) + s
            contrib.setdefault(idx, {})[t] = s
    if not scores:
        return []
    order = sorted(scores, key=lambda i: (-scores[i], i))[:max(0, int(k))]
    texts, out = {}, []
    for idx in order:
        rel, a, b, _dl = chunks[idx]
        if rel not in texts:
            texts[rel] = read_text(os.path.join(root, rel)) or ""
        c = contrib[idx]
        out.append({"source": os.path.join(root, rel).replace("/", os.sep), "content": texts[rel][a:b].strip(),
                    "score": round(scores[idx], 4), "terms": sorted(c, key=lambda t: (-c[t], t))})
    return out


def stats(root=None, index=None):
    root = os.path.abspath(root or R.repo_root())
    if index is None:
        index = load(root)
    path = cache_path(root)
    return {"root": root, "files": index.get("files"), "chunks": len(index.get("chunks") or []),
            "terms": len(index.get("postings") or {}), "avgdl": round(float(index.get("avgdl") or 0.0), 1),
            "built_at": index.get("built_at"), "build_seconds": index.get("build_seconds"),
            "cache": path, "cache_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "key": index.get("key")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", nargs="?", help="the question to look up")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--root", default=None)
    ap.add_argument("--rebuild", action="store_true", help="ignore the cache and build again")
    ap.add_argument("--stats", action="store_true", help="print the index's size and where it lives")
    a = ap.parse_args(argv)
    t0 = time.time()
    index = load(a.root, rebuild=a.rebuild)
    loaded = time.time() - t0
    if a.stats or not a.question:
        s = stats(a.root, index)
        s["load_seconds"] = round(loaded, 3)
        print(json.dumps(s, indent=2))
        if not a.question:
            return 0
    t1 = time.time()
    hits = query(a.question, k=a.k, root=a.root, index=index)
    dt = (time.time() - t1) * 1000.0
    if not hits:
        print("no chunk holds any term of that question (%.1f ms)" % dt)
        return 1
    print("%d hits in %.1f ms" % (len(hits), dt))
    for h in hits:
        print("%8.3f  %-60s  %s" % (h["score"], h["source"][-60:], ", ".join(h["terms"])))
        print("          " + " ".join(h["content"][:160].split()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
