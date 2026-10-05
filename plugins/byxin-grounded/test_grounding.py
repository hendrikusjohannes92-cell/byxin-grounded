"""Grounding injects facts only when they cover the question (roadmap item 2).

A question about the project in general must not be anchored by the project's own name -- a name the whole record
is full of -- nor handed passages about the project in general with the instruction to answer only from them. Here a
scratch project whose every page says "AcmeOS" stands in for any project: the project's own name
neither anchors a question nor counts as covering it; a real term the record holds still does; a real identifier the
record lacks still gets the honest "cannot see it". The bridge runs as the mod runs it, with no engine (lexical lanes).
"""
import json, os, subprocess, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.environ.get("BYXIN_TEST_BRIDGE") or os.path.join(HERE, "brain", "bridge.py")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
TOPICS = ["logging", "colours", "fonts", "packaging", "release notes", "the licence", "keyboard shortcuts", "themes",
          "the installer", "translations", "accessibility", "window layout"]


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("acme")
    for i in range(60):
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / ("page_%02d.md" % i)).write_text(
            "# AcmeOS page %d\n\nAcmeOS keeps its notes about %s on this page, number %d.\n" % (i, TOPICS[i % len(TOPICS)], i),
            encoding="utf-8")
    (root / "docs" / "tokenizer.md").write_text(
        "# The AcmeOS tokenizer\n\nThe tokenizer splits input on whitespace and punctuation, then hands the tokens to "
        "the parser, which builds the syntax tree.\n", encoding="utf-8")
    env = dict(os.environ, **IDENT)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "pages"]):
        subprocess.run(["git"] + args, cwd=root, env=env, capture_output=True, check=True, creationflags=NO_WINDOW)
    return root


def prepare(root, q):
    env = dict(os.environ, BYXIN_RPC_PORT="9", PYTHONIOENCODING="utf-8")
    env.pop("BYXIN_ROOT", None)
    r = subprocess.run([sys.executable, BRIDGE], input=json.dumps({"op": "prepare", "question": q, "root": str(root),
                                                                    "session": "grounding-test"}),
                       capture_output=True, text=True, encoding="utf-8", cwd=os.path.dirname(BRIDGE), env=env,
                       timeout=300, creationflags=NO_WINDOW)
    d = json.loads(r.stdout)
    assert d["ok"], d
    return d


def test_the_projects_own_name_neither_anchors_nor_covers(project):
    d = prepare(project, "how perfect is the AcmeOS grounded plugin?")
    assert not d["anchored"], d
    assert not d["answerable"] and d["chunks"] == 0, d["sources"]


def test_a_question_the_record_holds_still_gets_its_facts(project):
    d = prepare(project, "how does the AcmeOS tokenizer split input?")
    assert d["answerable"] and d["chunks"] >= 1, d["retrieval_note"]
    assert any("tokenizer" in s for s in d["sources"]), d["sources"]


def test_a_real_identifier_the_record_lacks_is_still_anchored(project):
    d = prepare(project, "where is frobnicate_widget.py defined?")
    assert d["anchored"] and not d["answerable"], d


# Roadmap item 3: the live engine is asked less, and the bench sees the person. A ByxIn tree here is the vendored
# brain's own files under a project of their own, and the engine is a stand-in that counts what it is asked.

@pytest.fixture
def tree(tmp_path):
    import shutil
    t = tmp_path / "tree"
    shutil.copytree(os.path.join(HERE, "brain", "tools"), t / "tools")
    shutil.copytree(os.path.join(HERE, "brain", "config"), t / "config")
    (t / "docs").mkdir()
    (t / "docs" / "lexer.md").write_text("# The lexer\n\nThe lexer reads characters and emits tokens.\n", encoding="utf-8")
    env = dict(os.environ, **IDENT)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "tree"]):
        subprocess.run(["git"] + args, cwd=t, env=env, capture_output=True, check=True, creationflags=NO_WINDOW)
    return t


@pytest.fixture
def engine(tree):
    """A stand-in engine: answers byxin.episodic.stats with a writer inside the tree (so it serves this project) and
    every other method with an error; records each method and whether the ask mark existed when it arrived."""
    import http.server, threading
    seen = []
    mark = tree / "data" / "answering_layer_ask.json"

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append((body.get("method"), mark.exists()))
            res = ({"result": {"writer": str(tree / "data" / "hippocampus.db")}} if body.get("method") == "byxin.episodic.stats"
                   else {"error": {"code": -32601, "message": "not here"}})
            out = json.dumps(dict(res, jsonrpc="2.0", id=body.get("id"))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()


def prepare_at(root, q, port):
    env = dict(os.environ, BYXIN_RPC_PORT=str(port), PYTHONIOENCODING="utf-8")
    env.pop("BYXIN_ROOT", None)
    r = subprocess.run([sys.executable, BRIDGE], input=json.dumps({"op": "prepare", "question": q, "root": str(root),
                                                                    "session": "engine-test"}),
                       capture_output=True, text=True, encoding="utf-8", cwd=os.path.dirname(BRIDGE), env=env,
                       timeout=300, creationflags=NO_WINDOW)
    d = json.loads(r.stdout)
    assert d["ok"], d
    return d


def test_the_engine_is_asked_once_whose_brain_it_is(tree, engine):
    port, seen = engine
    prepare_at(tree, "what does the lexer emit?", port)
    prepare_at(tree, "what does the lexer read?", port)
    asked = [m for m, _ in seen if m == "byxin.episodic.stats"]
    assert len(asked) == 1, seen


def test_the_bench_is_told_before_the_engine_is_asked(tree, engine):
    port, seen = engine
    prepare_at(tree, "what does the lexer emit?", port)
    assert seen and all(had for _m, had in seen), "the ask mark must exist before the first engine call: %s" % seen
    mark = json.loads((tree / "data" / "answering_layer_ask.json").read_text(encoding="utf-8"))
    assert mark.get("at") and "question" not in mark, mark


# Roadmap items 4 and 7c: a web session gets the grounding through the classic hooks, and the person sees )|(.

CLASSIC = os.path.join(os.path.dirname(BRIDGE), "classic_hook.py")


def classic(event, root, session, **kw):
    env = dict(os.environ, BYXIN_RPC_PORT="9", PYTHONIOENCODING="utf-8", CLAUDE_CODE_REMOTE="true", BYXIN_HOST="cloud-box")
    r = subprocess.run([sys.executable, CLASSIC], input=json.dumps(dict(kw, hook_event_name=event, cwd=str(root),
                                                                         session_id=session)),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=300, creationflags=NO_WINDOW)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}


def test_the_web_hooks_ground_a_question_and_record_the_verdict(project, tmp_path):
    out = classic("UserPromptSubmit", project, "web-1", prompt="how does the AcmeOS tokenizer split input?")
    assert "tokenizer" in (out.get("hookSpecificOutput") or {}).get("additionalContext", ""), out
    assert out.get("systemMessage", "").startswith(")|( ByxIn: grounding"), out
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(m) for m in (
        {"type": "user", "message": {"role": "user", "content": "how does the AcmeOS tokenizer split input?"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "It splits input on whitespace and "
                                                       "punctuation (docs/tokenizer.md)."}]}})), encoding="utf-8")
    out = classic("Stop", project, "web-1", transcript_path=str(transcript))
    assert out.get("systemMessage", "").startswith(")|( ByxIn: "), out
    hub = os.path.join(str(project), ".git", "byxin-brain", "events")
    turns = [json.load(open(os.path.join(hub, n), encoding="utf-8")) for n in os.listdir(hub) if "-turn-" in n]
    mine = [t for t in turns if t.get("session") == "web-1"]
    assert mine and mine[-1].get("outcome") in ("verified", "unverified") and "whitespace" in mine[-1].get("answer", ""), mine


def test_the_web_hooks_pass_an_uncovered_question_through(project):
    out = classic("UserPromptSubmit", project, "web-2", prompt="how perfect is the AcmeOS grounded plugin?")
    assert "grounding" not in out.get("systemMessage", ""), out
    assert "ANSWER ONLY FROM" not in json.dumps(out), out


def test_the_web_hooks_ask_like_the_mod():
    import re
    ts = open(os.path.join(HERE, "hooks", "register.tsx"), encoding="utf-8").read()
    js = re.search(r"const QUESTION = /(.*)/i\n", ts).group(1)
    py = open(CLASSIC, encoding="utf-8").read()
    pat = re.search(r'QUESTION = re\.compile\(r"(.*)",', py).group(1)
    assert js == pat, (js, pat)
