#!/usr/bin/env python3
"""The world ByxIn lives in, as a pack it loads -- never as names in the brain's code.

    py -3.11 tools/byxin_world.py          # the loaded world, one line per field



"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAMELESS = {
    "name": "",
    "record": "the record I was given",
    "identity": "You are ByxIn, answering a question about the record you were given.",
    "facts_source": "the record you were given",
    "exam": "",
    "authority_pages": [],
    "world_model": {"deriver": "", "sources": []},
    "places": {"by_path": [], "by_question": []},
    "claim_kinds": {"by_path": []},
    "derived_pages": {"pages": []},
    "guards": [],
    "diary_dirs": [],
    "not_anchors": ["ByxIn"],
    "knowledge_header": "[Knowledge — confidence: %.2f]",
    "os_name": "the OS",
    "os_paths": [],
}


def _find_pack():
    """The pack of the tree this module runs in: config/world.json under the nearest ancestor that has one, so
    the deployed copy of the brain (byxin/build/Release/tools) and the repository's tools/ read the same pack."""
    d = HERE
    for _ in range(6):
        d = os.path.dirname(d)
        p = os.path.join(d, "config", "world.json")
        if os.path.isfile(p):
            return p
    return os.path.join(ROOT, "config", "world.json")


PACK = None   # resolved on first use (see _find_pack); a caller may pass another pack's path to any accessor


def load(path=None):
    """The pack as a dict, every field present (the nameless defaults fill what the pack leaves out)."""
    global PACK
    if PACK is None:
        PACK = _find_pack()
    path = path or PACK
    doc = {}
    try:
        doc = json.load(io.open(path, encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    out = dict(NAMELESS)
    for k, v in (doc or {}).items():
        if k in NAMELESS and v is not None:
            out[k] = v
    return out


def name(path=None):
    return str(load(path)["name"])


def record(path=None):
    return str(load(path)["record"])


def identity(path=None):
    return str(load(path)["identity"])


def facts_source(path=None):
    """Part of ByxIn's brain."""
    return str(load(path)["facts_source"])


def authority_pages(path=None):
    return [str(p) for p in load(path)["authority_pages"]]


def exam_path(path=None):
    """The world's exam file (the bench questions about THIS world), absolute, or "" for a world with none."""
    e = str(load(path)["exam"] or "")
    if not e:
        return ""
    base = os.path.dirname(os.path.dirname(path or PACK or _find_pack()))
    return e if os.path.isabs(e) else os.path.join(base, e)


def world_model(path=None):
    wm = load(path)["world_model"] or {}
    return {"deriver": str(wm.get("deriver") or ""), "sources": [str(s) for s in wm.get("sources") or []]}


def places(path=None):
    """(by_path, by_question): lists of (compiled regex, place), in the pack's order -- the first match wins."""
    pl = load(path)["places"] or {}
    out = []
    for key in ("by_path", "by_question"):
        rows = []
        for entry in pl.get(key) or []:
            try:
                pat, place = entry[0], entry[1]
                rows.append((re.compile(str(pat), re.I), str(place)))
            except (re.error, IndexError, TypeError):
                continue
        out.append(rows)
    return tuple(out)


def claim_kinds(path=None):
    """[(compiled regex, kind)] by path, in the pack's order -- the first match wins."""
    rows = []
    for entry in (load(path)["claim_kinds"] or {}).get("by_path") or []:
        try:
            rows.append((re.compile(str(entry[0]), re.I), str(entry[1])))
        except (re.error, IndexError, TypeError):
            continue
    return rows


def not_anchors(path=None):
    """The names that are in every question of this world and anchor nothing (the brain's own name always)."""
    out = [str(x) for x in load(path)["not_anchors"] or []]
    return out if "ByxIn" in out else ["ByxIn"] + out


def stop_words(path=None):
    """The world's own names, lower-cased, for the stop-word lists that pick a question's content words."""
    return sorted({str(x).lower() for x in not_anchors(path)} | ({str(load(path)["name"]).lower()} if load(path)["name"] else set()))


def knowledge_header(path=None):
    """Part of ByxIn's brain."""
    h = str(load(path)["knowledge_header"])
    return h if "%.2f" in h else NAMELESS["knowledge_header"]


def derived_pages(path=None):
    """[(name, check argv)]: the world's rendered pages and the commands whose --check says whether each is stale."""
    out = []
    for p in (load(path)["derived_pages"] or {}).get("pages") or []:
        try:
            if p["name"] and p["check"]:
                out.append((str(p["name"]), [str(a) for a in p["check"]]))
        except (KeyError, TypeError):
            continue
    return out


def guards(path=None):
    """The world's own guard tests (file names under tools/), run by the self-test beside the brain's."""
    return [str(g) for g in load(path)["guards"] or []]


def diary_dirs(path=None):
    """Directories of dated logs (relative, forward slashes): two entries of the diary disagree whenever the world
    changed between them, so the record's auditor pairs a diary page only with a STANDING page."""
    return [str(d).replace("\\", "/").rstrip("/") + "/" for d in (load(path)["diary_dirs"] or [])]


def os_name(path=None):
    return str(load(path)["os_name"])


def os_paths(path=None):
    return [str(p) for p in load(path)["os_paths"]]


def main(argv=None):
    w = load()
    print("world: %s" % (w["name"] or "(nameless)"))
    for k in ("record", "identity", "os_name"):
        print("%-16s %s" % (k, w[k]))
    print("%-16s %s" % ("authority_pages", ", ".join(w["authority_pages"]) or "-"))
    print("%-16s %s <- %s" % ("world_model", w["world_model"].get("deriver") or "-", ", ".join(w["world_model"].get("sources") or []) or "-"))
    bp, bq = places()
    print("%-16s %d by path, %d by question" % ("places", len(bp), len(bq)))
    print("%-16s %s" % ("os_paths", ", ".join(w["os_paths"]) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
