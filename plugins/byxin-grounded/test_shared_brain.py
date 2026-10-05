"""The shared brain: every Claude Code session in one project reads and writes one record of what the others did.

Two local sessions are two worktrees of one repository (one hub, inside the shared .git); the remote session is a
separate clone on "another machine" (BYXIN_HOST) that joins through the orphan branch .byxin/share.json names, on a
bare repository standing in for GitHub. Every case runs brain/bridge.py as the mod runs it, with the engine's port
pointed at nothing, so no live brain is asked. The red halves: a session alone hears nothing, a project without
share.json sends nothing anywhere, a remote listed under `never` is refused.
"""
import json, os, sqlite3, subprocess, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "brain", "bridge.py")
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(cwd, *args):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, env=dict(os.environ, **IDENT),
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    assert r.returncode == 0, r.stderr
    return r.stdout


def bridge(op, root, session, host="laptop", wait=True, **kw):
    env = dict(os.environ, BYXIN_RPC_PORT="9", BYXIN_HOST=host, PYTHONIOENCODING="utf-8")
    env.pop("BYXIN_ROOT", None)
    env.pop("BYXIN_HUB", None)
    p = subprocess.Popen([sys.executable, BRIDGE], cwd=os.path.dirname(BRIDGE), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
                         creationflags=NO_WINDOW)
    p.stdin.write(json.dumps(dict(kw, op=op, root=str(root), session=session)))
    p.stdin.close()
    if not wait:
        return p
    return finish(p)


def finish(p):
    out = p.stdout.read()
    p.wait(timeout=180)
    d = json.loads(out)
    assert d.get("ok"), d
    return d


def refused(op, root, session, **kw):
    """An op that must refuse: its answer, which finish() would not let through."""
    p = bridge(op, root, session, wait=False, **kw)
    d = json.loads(p.stdout.read())
    p.wait(timeout=180)
    assert d.get("ok") is False, d
    return d


@pytest.fixture
def world(tmp_path):
    """A bare 'GitHub', a project pushed to it with sharing on, a second worktree of it, and a clone elsewhere."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(remote))
    a = tmp_path / "a"
    git(tmp_path, "init", "-q", "-b", "main", str(a))
    (a / "README.md").write_text("# a project\nThe parser lives in parse.py.\n", encoding="utf-8")
    (a / ".byxin").mkdir()
    (a / ".byxin" / "share.json").write_text(json.dumps({"remote": "origin", "branch": "byxin-brain"}), encoding="utf-8")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "start")
    git(a, "remote", "add", "origin", str(remote))
    git(a, "push", "-q", "origin", "main")
    wt = tmp_path / "a-wt"
    git(a, "worktree", "add", "-q", "-b", "side", str(wt))
    b = tmp_path / "b"
    git(tmp_path, "clone", "-q", str(remote), str(b))
    return {"remote": remote, "a": a, "wt": wt, "b": b, "tmp": tmp_path}


def test_two_local_sessions_in_two_worktrees_share_one_hub_and_see_each_other(world):
    w1 = bridge("where", world["a"], "s1-local")
    w2 = bridge("where", world["wt"], "s2-local")
    assert w1["hub"] == w2["hub"], "two worktrees of one repository must share one hub"
    assert w1["hub"].replace("\\", "/").endswith(".git/byxin-brain")
    bridge("start", world["a"], "s1-local")
    bridge("turn", world["a"], "s1-local", ask="refactor the parser", files=["parse.py"], answer="done")
    bridge("note", world["a"], "s1-local", text="parse.py is mid-refactor; do not touch the tokenizer")
    bridge("beat", world["a"], "s1-local", files=["parse.py"])
    got = bridge("start", world["wt"], "s2-local")
    assert "refactor the parser" in got["text"]
    assert "parse.py is mid-refactor" in got["text"]
    assert "parse.py" in got["text"]
    live = {s["session"]: s for s in got["live"]}
    assert "s1-local" in live and live["s1-local"]["files"] == ["parse.py"] and live["s1-local"]["where"] == "this machine"


def test_a_session_alone_hears_nothing(world):
    got = bridge("start", world["a"], "only-one")
    assert got["text"] == "" and got["live"] == []


def test_a_remote_session_joins_through_the_shared_branch_both_ways(world):
    bridge("start", world["a"], "s1-local")
    bridge("note", world["a"], "s1-local", text="the local session owns parse.py today")
    bridge("turn", world["a"], "s1-local", ask="why does the parser drop comments?", outcome="verified", files=[])
    branch = git(world["remote"], "ls-tree", "--name-only", "byxin-brain").split()
    assert len(branch) >= 3, branch
    got = bridge("start", world["b"], "s3-cloud", host="cloud-box")
    assert got["sync"]["pulled"] >= 3
    assert "the local session owns parse.py today" in got["text"]
    assert "why does the parser drop comments?" in got["text"]
    bridge("note", world["b"], "s3-cloud", host="cloud-box", text="cloud session is writing the docs")
    back = bridge("resume", world["a"], "s1-local")
    assert "cloud session is writing the docs" in back["text"]
    live = {s["session"]: s for s in back["live"]}
    assert "s3-cloud" in live and live["s3-cloud"]["where"].startswith("another machine")


def test_two_sessions_pushing_at_once_both_land(world):
    p1 = bridge("note", world["a"], "s1-local", wait=False, text="note from this machine")
    p2 = bridge("note", world["b"], "s3-cloud", host="cloud-box", wait=False, text="note from the cloud")
    finish(p1)
    finish(p2)
    bridge("resume", world["a"], "s1-local")
    bridge("resume", world["b"], "s3-cloud", host="cloud-box")
    texts = [json.loads(git(world["remote"], "show", "byxin-brain:" + n)).get("text")
             for n in git(world["remote"], "ls-tree", "--name-only", "byxin-brain").split()]
    assert "note from this machine" in texts and "note from the cloud" in texts, texts


def test_without_share_json_nothing_leaves_the_machine(world, tmp_path):
    c = tmp_path / "c"
    git(tmp_path, "clone", "-q", str(world["remote"]), str(c))
    os.remove(c / ".byxin" / "share.json")
    got = bridge("note", c, "s5", text="stays here")
    assert got["sync"] is None
    assert git(world["remote"], "branch", "--list", "byxin-brain").strip() == ""


def test_a_remote_listed_under_never_is_refused(world):
    (world["a"] / ".byxin" / "share.json").write_text(
        json.dumps({"remote": "origin", "branch": "byxin-brain", "never": ["origin"]}), encoding="utf-8")
    got = bridge("note", world["a"], "s1-local", text="must not leave")
    assert "refusing" in got["sync"]["error"]
    assert git(world["remote"], "branch", "--list", "byxin-brain").strip() == ""


def test_an_answer_and_a_note_are_episodes_in_the_brains_record_with_their_origin(world):
    w = bridge("where", world["a"], "s1-local")
    assert w["tree"] == os.path.join(HERE, "brain"), "a plain project is served by the vendored brain"
    t = bridge("turn", world["a"], "s1-local", ask="what does parse.py export?", outcome="verified", answer="parse()")
    n = bridge("note", world["a"], "s1-local", text="release on friday")
    assert t["stored"].startswith("the brain's record") and n["stored"].startswith("the brain's record"), (t, n)
    rows = sqlite3.connect(w["record"]).execute(
        "select source, origin, skill, outcome, intent from episodes order by id").fetchall()
    assert ("answering_layer", "perceived", "answer_from_knowledge", "verified", "what does parse.py export?") in rows
    assert ("answering_layer", "told", "note", "noted", "release on friday") in rows
    assert not w["record"].replace("\\", "/").startswith(HERE.replace("\\", "/")), "the record must not live in the mod's watched folder"


def test_a_byxin_tree_is_served_by_its_own_brain(tmp_path):
    t = tmp_path / "brain-tree"
    for parts in (("tools", "byxin_rerank.py"), ("tools", "byxin_history.py")):
        (t.joinpath(*parts[:-1])).mkdir(parents=True, exist_ok=True)
        t.joinpath(*parts).write_text("", encoding="utf-8")
    (t / "config" / "skills" / "answer_from_knowledge").mkdir(parents=True)
    import shutil
    shutil.copy(os.path.join(HERE, "brain", "config", "skills", "answer_from_knowledge", "handler.py"),
                t / "config" / "skills" / "answer_from_knowledge" / "handler.py")
    git(tmp_path, "init", "-q", str(t))
    w = bridge("where", t, "s1")
    assert w["tree"] == str(t) and "ByxIn tree" in w["tree_why"]


CLASSIC = os.path.join(HERE, "brain", "classic_hook.py")


def classic(event, cwd, session, remote=True, **kw):
    env = dict(os.environ, BYXIN_RPC_PORT="9", BYXIN_HOST="cloud-box", PYTHONIOENCODING="utf-8")
    env.pop("BYXIN_CLASSIC_HOOKS", None)
    if remote:
        env["CLAUDE_CODE_REMOTE"] = "true"
    else:
        env.pop("CLAUDE_CODE_REMOTE", None)
    r = subprocess.run([sys.executable, CLASSIC], input=json.dumps(dict(kw, hook_event_name=event, cwd=str(cwd), session_id=session)),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=180, creationflags=NO_WINDOW)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_a_cloud_session_shares_the_brain_through_classic_hooks(world):
    bridge("note", world["a"], "s1-local", text="laptop note for the cloud")
    out = classic("SessionStart", world["b"], "s3-cloud")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "laptop note for the cloud" in ctx
    classic("UserPromptSubmit", world["b"], "s3-cloud", prompt="write the docs page")
    classic("PostToolUse", world["b"], "s3-cloud", tool_name="Write", tool_input={"file_path": str(world["b"] / "DOCS.md")})
    classic("Stop", world["b"], "s3-cloud")
    back = bridge("resume", world["a"], "s1-local")
    assert "write the docs page" in back["text"] and "DOCS.md" in back["text"], back["text"]


def test_classic_hooks_stay_silent_where_the_mod_runs(world):
    hub = bridge("where", world["b"], "probe")["hub"]
    before = sorted(os.listdir(os.path.join(hub, "events"))) if os.path.isdir(os.path.join(hub, "events")) else []
    assert classic("SessionStart", world["b"], "s9-local", remote=False) is None
    classic("UserPromptSubmit", world["b"], "s9-local", remote=False, prompt="nothing should be written")
    after = sorted(os.listdir(os.path.join(hub, "events"))) if os.path.isdir(os.path.join(hub, "events")) else []
    assert after == before


def test_byxin_share_zero_keeps_a_run_on_its_machine(world):
    env_note = subprocess.run([sys.executable, BRIDGE], input=json.dumps({"op": "note", "root": str(world["a"]), "session": "s0", "text": "local only"}),
                              capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW, timeout=180,
                              env=dict(os.environ, BYXIN_RPC_PORT="9", BYXIN_SHARE="0", PYTHONIOENCODING="utf-8"))
    assert json.loads(env_note.stdout)["sync"] is None
    assert git(world["remote"], "branch", "--list", "byxin-brain").strip() == ""


def test_a_wrong_record_is_retracted_kept_and_marked_and_a_turn_says_what_it_edited(world):
    bridge("turn", world["a"], "s1-local", ask="create notes.md", files=["notes.md"], answer="I could not write it")
    bridge("turn", world["a"], "s1-local", ask="explain the parser", answer="It reads tokens.")
    ev = [e for e in bridge("events", world["a"], "s1-local")["events"] if e["kind"] == "turn" and e.get("files")][0]
    r = bridge("retract", world["a"], "s2-local", event=ev["id"], why="the write was refused; notes.md was never created")
    assert r["retracts"] == ev["id"]
    text = bridge("resume", world["wt"], "s3-local")["text"]
    assert "the write was refused; notes.md was never created" in text
    assert "edited notes.md" not in text, text
    assert "explain the parser; edited nothing; its answer began: It reads tokens." in text, text
    hub = bridge("where", world["a"], "s1-local")["hub"]
    assert os.path.exists(os.path.join(hub, "events", ev["id"] + ".json")), "a retraction never deletes the record it corrects"


def test_an_edit_never_falls_out_of_the_block_however_many_sessions_followed(world):
    bridge("turn", world["a"], "old-0", ask="fix the lexer", files=["lexer.py"], answer="fixed")
    for i in range(1, 8):
        bridge("turn", world["a"], "new-%d" % i, ask="question %d" % i, answer="answer")
    text = bridge("resume", world["wt"], "reader")["text"]
    assert "older session(s) not shown in full; between them they edited lexer.py" in text, text


NOTIFICATION = ('<task-notification>\n<task-id>b3n2k56p4</task-id>\n<summary>Monitor event: "nightly build"</summary>\n'
                '<event>build 412 finished: 3 targets, 0 failed</event>\n</task-notification>')


def old_turn(root, session, ask, answer, at):
    """A turn event as a mod older than 0.4.1 wrote it: the notification itself recorded as the ask."""
    hub = bridge("where", root, session)["hub"]
    os.makedirs(os.path.join(hub, "events"), exist_ok=True)
    eid = "%s-%s-turn-old%03d" % (at.replace("-", "").replace(":", ""), session[:8], len(os.listdir(os.path.join(hub, "events"))))
    with open(os.path.join(hub, "events", eid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": eid, "at": at, "kind": "turn", "origin": "perceived", "by": "claude-code", "session": session,
                   "host": "laptop", "branch": "main", "ask": ask, "answer": answer}, fh)


def test_a_notification_is_not_someone_asking(world):
    """A long-running job's notifications are not questions, or they fill the block every other session reads. A turn a
    notification started is no one asking:
    the mod records it only when it edited something, and says what started it; a record written before the fix (its
    ask the notification itself) is read the same way."""
    bridge("turn", world["a"], "mon-1", trigger="task-notification", files=["watch.sh"], answer="noted")
    old_turn(world["a"], "mon-1", NOTIFICATION, "still waiting", "2026-10-04T10:00:01Z")
    bridge("turn", world["a"], "mon-1", ask="is the run done?", trigger="composer", answer="not yet")
    old_turn(world["a"], "mon-2", NOTIFICATION, "waiting", "2026-10-04T10:00:02Z")
    old_turn(world["a"], "mon-2", NOTIFICATION, "waiting", "2026-10-04T10:00:03Z")
    text = bridge("resume", world["wt"], "reader")["text"]
    assert "task-notification>" not in text and "build 412 finished" not in text, text
    assert "after a task-notification (no one asked): edited watch.sh" in text, text
    assert "asked: is the run done?; edited nothing" in text, text
    assert "2 turn(s) no one asked (task-notification); edited nothing" in text, text


def test_the_bridge_refuses_a_notification_whatever_mod_sends_it(world):
    """The bridge is started afresh for every call, so a session still running an older module gets this at once: a
    notification is not looked up in the record (no engine call), and a turn it started is written only for what it
    edited, as no one asking."""
    p = bridge("prepare", world["a"], "old-mod", question=NOTIFICATION)
    assert p["answerable"] is False and p["anchored"] is False and p["chunks"] == 0, p
    assert "no one asked" in (p.get("retrieval_note") or ""), p
    t = bridge("turn", world["a"], "old-mod", ask=NOTIFICATION, answer="still waiting")
    assert t.get("event") is None and "no one asked" in t.get("skipped", ""), t
    bridge("turn", world["a"], "old-mod", ask=NOTIFICATION, files=["LOG.md"], answer="logged")
    turns = [e for e in bridge("events", world["a"], "probe", n=50)["events"] if e["kind"] == "turn" and e["session"] == "old-mod"]
    assert len(turns) == 1 and turns[0]["files"] == ["LOG.md"] and turns[0]["trigger"] == "task-notification", turns
    hub = bridge("where", world["a"], "probe")["hub"]
    with open(os.path.join(hub, "events", turns[0]["id"] + ".json"), encoding="utf-8") as fh:
        assert "ask" not in json.load(fh), "a turn no one asked carries no ask"


def test_a_cloud_notification_is_not_someone_asking(world):
    classic("UserPromptSubmit", world["b"], "s4-cloud", prompt=NOTIFICATION)
    classic("Stop", world["b"], "s4-cloud")
    classic("UserPromptSubmit", world["b"], "s4-cloud", prompt=NOTIFICATION)
    classic("PostToolUse", world["b"], "s4-cloud", tool_name="Write", tool_input={"file_path": str(world["b"] / "LOG.md")})
    classic("Stop", world["b"], "s4-cloud")
    turns = [e for e in bridge("events", world["b"], "probe", n=50)["events"] if e["kind"] == "turn" and e["session"] == "s4-cloud"]
    assert len(turns) == 1, turns
    assert turns[0]["files"] == ["LOG.md"] and turns[0]["trigger"] == "task-notification", turns


def test_the_mod_and_the_bridge_know_the_same_envelopes():
    """One list, two languages: the mod decides before the model speaks, the bridge when it reads an older record."""
    import importlib.util, re
    spec = importlib.util.spec_from_file_location("bridge_for_test", BRIDGE)
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    ts = open(os.path.join(HERE, "hooks", "register.tsx"), encoding="utf-8").read()
    m = re.search(r"const ENVELOPES = \[([^\]]*)\]", ts)
    assert m, "register.tsx must declare const ENVELOPES = [...]"
    assert re.findall(r"'([^']+)'", m.group(1)) == list(b.ENVELOPES)
    assert b.envelope_of(NOTIFICATION) == "task-notification" and b.envelope_of("is the run done?") is None


def test_a_later_prompt_hears_only_what_is_new(world):
    """With several live sessions, repeating the whole block on every prompt drowns it. A later prompt hears what
    changed since its session last heard."""
    import time
    bridge("turn", world["a"], "s1-local", ask="first ask about the lexer", answer="ok")
    heard = bridge("start", world["wt"], "reader")
    assert "first ask about the lexer" in heard["text"]
    time.sleep(1.2)                                       # events are stamped to the second
    bridge("turn", world["a"], "s1-local", ask="second ask about the parser", files=["parse.py"], answer="done")
    bridge("note", world["a"], "s2-local", text="the release is frozen until Friday")
    news = bridge("beat", world["wt"], "reader", since=heard["at"])["news"]
    assert "second ask about the parser" in news and "edited parse.py" in news, news
    assert "the release is frozen until Friday" in news, news
    assert "first ask about the lexer" not in news, news
    again = bridge("beat", world["wt"], "reader", since=bridge("beat", world["wt"], "reader")["at"])["news"]
    assert again == "", "nothing new is no news: %r" % again


def test_only_files_in_the_project_are_named(world):
    """The shared brain is about this project; a path outside it is never named, and a turn that edited only such files says so."""
    outside = ["Z:/scratch/probe.py", "../elsewhere/notes.md", "/tmp/x.json"]
    bridge("turn", world["a"], "s1-local", ask="refactor the parser", files=["parse.py"] + outside, answer="done")
    bridge("turn", world["a"], "s1-local", ask="write a scratch probe", files=outside[:1], answer="written")
    bridge("beat", world["a"], "s1-local", files=["parse.py"] + outside)
    text = bridge("start", world["wt"], "reader")["text"]
    assert "parse.py" in text, text
    assert "probe.py" not in text and "elsewhere" not in text and "/tmp/" not in text, text
    assert "write a scratch probe; edited only files outside the project" in text, text


def test_the_block_has_a_cap(world):
    for i in range(9):
        for j in range(3):
            bridge("turn", world["a"], "busy-%d" % i, ask=("question %d.%d " % (i, j)) + "about a long topic " * 12,
                   files=["src/module_%d_%d.py" % (i, k) for k in range(12)], answer="an answer " * 30)
    text = bridge("start", world["wt"], "reader")["text"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("bridge_for_cap", BRIDGE)
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    assert len(text) <= b.BLOCK_CAP + 120, len(text)
    assert "/byxin brain" in text, text[-300:]


def test_the_first_session_in_a_project_is_told_what_byxin_is_and_no_later_one(world):
    assert bridge("start", world["a"], "first-one")["first"] is True
    assert bridge("start", world["a"], "first-one")["first"] is True, "a session's own restart is still the first"
    bridge("turn", world["a"], "first-one", ask="what is here?", answer="a parser")
    assert bridge("start", world["wt"], "second-one")["first"] is False


# Roadmap item 8: mail between sessions lives in the shared brain, addressed to one session or to all.

def test_mail_reaches_the_session_it_is_for_and_no_other(world):
    import time
    t0 = bridge("start", world["wt"], "s2-local")["at"]
    bridge("start", world["a"], "s3-other")
    time.sleep(1.2)
    sent = bridge("send", world["a"], "s1-local", to="s2-local", text="please rebase onto main before landing", by="person")
    assert sent["event"], sent
    for_s2 = bridge("beat", world["wt"], "s2-local", since=t0, mail_since=t0)
    assert [m["text"] for m in for_s2["mail"]] == ["please rebase onto main before landing"], for_s2["mail"]
    assert "please rebase onto main" in for_s2["news"], for_s2["news"]
    for_s3 = bridge("beat", world["a"], "s3-other", since=t0, mail_since=t0)
    assert for_s3["mail"] == [] and "please rebase" not in for_s3["news"], for_s3
    assert [m["text"] for m in bridge("mail", world["wt"], "s2-local")["mail"]] == ["please rebase onto main before landing"]


def test_mail_to_all_reaches_every_other_session_and_is_never_shown_as_asked(world):
    import time
    t0 = bridge("start", world["wt"], "s2-local")["at"]
    time.sleep(1.2)
    bridge("send", world["a"], "s1-local", to="all", text="the release is frozen until Friday", by="session")
    got = bridge("beat", world["wt"], "s2-local", since=t0, mail_since=t0)
    assert [m["text"] for m in got["mail"]] == ["the release is frozen until Friday"], got["mail"]
    assert "asked:" not in got["news"], got["news"]
    mine = bridge("beat", world["a"], "s1-local", since=t0, mail_since=t0)["mail"]
    assert mine == [], "a session does not receive its own mail"


def test_mail_crosses_machines_through_the_shared_branch(world):
    t0 = bridge("start", world["b"], "s4-cloud", host="cloud-box")["at"]
    bridge("send", world["a"], "s1-local", to="s4-cloud", text="your branch is behind main", by="person")
    got = bridge("resume", world["b"], "s4-cloud", host="cloud-box")
    assert "your branch is behind main" in got["text"], got["text"]
    assert [m["text"] for m in bridge("mail", world["b"], "s4-cloud", host="cloud-box")["mail"]] == ["your branch is behind main"]


def test_a_named_session_gets_the_mail_sent_to_its_name(world):
    import time
    t0 = bridge("start", world["wt"], "f00dcafe-session", host="laptop")["at"]
    bridge("name", world["wt"], "f00dcafe-session", name="reviewer")
    time.sleep(1.2)
    bridge("send", world["a"], "s1-local", to="reviewer", text="the parser change is ready for review", by="person")
    got = bridge("beat", world["wt"], "f00dcafe-session", since=t0, mail_since=t0)
    assert [m["text"] for m in got["mail"]] == ["the parser change is ready for review"], got
    other = bridge("beat", world["a"], "s9-other", since=t0, mail_since=t0)
    assert other["mail"] == [], other
    assert "reviewer" in bridge("start", world["a"], "s8-reader")["text"], "the block shows a session's name"


# The review of the mail (2026-10-05): a reply chain cannot wake sessions for ever, addresses cannot reach strangers,
# names cannot collide, and the sender hears what went wrong.

def test_a_reply_is_one_deeper_than_the_mail_it_answers(world):
    bridge("start", world["wt"], "s2-local")
    first = bridge("send", world["a"], "s1-local", to="s2-local", text="can you take the parser?", by="person")
    assert first["depth"] == 0, first
    answer = bridge("send", world["wt"], "s2-local", to="s1-local", text="yes", by="session", in_reply_to=first["event"])
    assert answer["depth"] == 1, answer
    again = bridge("send", world["a"], "s1-local", to="s2-local", text="thanks", by="session", in_reply_to=answer["event"])
    assert again["depth"] == 2, "a reply to a reply is past what wakes a session"
    got = bridge("beat", world["wt"], "s2-local", mail_recent=True)["mail"]
    assert [(m["text"], m["depth"]) for m in got] == [("can you take the parser?", 0), ("thanks", 2)], got


def test_a_short_prefix_reaches_no_one_and_eight_characters_do(world):
    bridge("start", world["wt"], "s2-local-long-id")
    bridge("send", world["a"], "s1-local", to="s2", text="too short", by="person")
    bridge("send", world["a"], "s1-local", to="s2-local", text="eight is enough", by="person")
    got = [m["text"] for m in bridge("beat", world["wt"], "s2-local-long-id", mail_recent=True)["mail"]]
    assert got == ["eight is enough"], got


def test_a_name_another_session_holds_or_that_reads_as_an_id_is_refused(world):
    assert bridge("name", world["wt"], "s2-local", name="reviewer", by="person")["ok"]
    taken = refused("name", world["a"], "s1-local", name="Reviewer", by="person")
    assert "already called" in taken["error"], taken
    hexy = refused("name", world["a"], "s1-local", name="bead", by="person")
    assert "session id" in hexy["error"], hexy
    assert bridge("name", world["wt"], "s2-local", name="reviewer")["ok"], "a session may say its own name again"


def test_the_sender_hears_of_an_unknown_address_and_of_a_cut(world):
    bridge("start", world["wt"], "s2-local")
    typo = bridge("send", world["a"], "s1-local", to="s2-lcoal", text="hello", by="person")
    assert typo["ok"] and any("no session known" in w for w in typo["warn"]), typo
    known = bridge("send", world["a"], "s1-local", to="s2-local", text="x" * 2100, by="person")
    assert known["warn"] == ["cut at 2000 characters"], known


def test_mail_already_there_at_start_is_known_and_late_mail_is_still_new(world):
    bridge("send", world["a"], "s1-local", to="s2-local", text="left while you were away", by="person")
    st = bridge("start", world["wt"], "s2-local")
    assert len(st["mail_ids"]) == 1, st["mail_ids"]
    # a mail that arrives by a later sync carries the sender's older clock: the beat still returns it, by id
    late = bridge("send", world["a"], "s1-local", to="s2-local", text="from a slow clock", by="session")
    events = os.path.join(bridge("where", world["a"], "s1-local")["hub"], "events")
    path = os.path.join(events, late["event"] + ".json")
    with open(path, encoding="utf-8") as fh:
        ev = json.load(fh)
    ev["at"] = "2000-01-01T00:00:00Z"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ev, fh)
    got = bridge("beat", world["wt"], "s2-local", mail_recent=True)["mail"]
    assert late["event"] in [m["id"] for m in got], got


def test_the_block_shows_who_wrote_a_mail_as_the_senders_claim(world):
    bridge("start", world["wt"], "s2-local")
    bridge("send", world["a"], "s1-local", to="s2-local", text="trust me", by="person")
    text = bridge("resume", world["wt"], "s2-local")["text"]
    assert "the sender says a person wrote it" in text and "(a person wrote it" not in text, text


def test_a_late_mail_is_delivered_by_arrival_even_in_a_busy_mailbox(world):
    import time
    bridge("start", world["wt"], "s2-local")
    late = bridge("send", world["a"], "s1-local", to="s2-local", text="from a slow clock", by="session")
    events = os.path.join(bridge("where", world["a"], "s1-local")["hub"], "events")
    path = os.path.join(events, late["event"] + ".json")
    with open(path, encoding="utf-8") as fh:
        ev = json.load(fh)
    ev["at"] = "2000-01-01T00:00:00Z"
    for i in range(25):
        bridge("send", world["a"], "s1-local", to="s2-local", text="busy %d" % i, by="session", wait=False).wait(timeout=180)
    time.sleep(1.1)
    with open(path, "w", encoding="utf-8") as fh:      # it arrives last, by a sync, with the oldest clock
        json.dump(ev, fh)
    got = bridge("beat", world["wt"], "s2-local", mail_recent=True)["mail"]
    assert got[-1]["id"] == late["event"], [m["text"] for m in got]


def test_a_mirrored_mailbox_is_named_as_a_mailbox_not_a_session(world):
    bridge("start", world["wt"], "s2-local")
    hub = bridge("where", world["wt"], "s2-local")["hub"]
    ev = {"id": "20261005T120000Z-mailbox-mail-abc123", "at": "2026-10-05T12:00:00Z", "kind": "mail", "origin": "perceived",
          "by": "session", "session": "team-mailbox", "host": "laptop", "to": "all", "text": "the build is red", "mail_id": "m1"}
    with open(os.path.join(hub, "events", ev["id"] + ".json"), "w", encoding="utf-8") as fh:
        json.dump(ev, fh)
    text = bridge("resume", world["wt"], "s2-local")["text"]
    assert "mailbox team-mailbox on laptop" in text and "session team-mai" not in text, text


def test_a_session_without_the_send_tool_answers_mail_by_command(world):
    """The engine may not list the plugin's send tool to the model: brain/send.py sends the same mail by command, one
    level deeper when it names the mail it answers, and says plainly when it could not send."""
    first = bridge("send", world["a"], "s1-local", to="s2-local", text="can you look at the parser?", by="person")
    send = os.path.join(os.path.dirname(BRIDGE), "send.py")
    run = lambda *args: subprocess.run([sys.executable, send, *args], capture_output=True, text=True, encoding="utf-8",
                                       creationflags=NO_WINDOW, timeout=180,
                                       env=dict(os.environ, BYXIN_RPC_PORT="9", BYXIN_HOST="laptop", PYTHONIOENCODING="utf-8"))
    ok = run("--session", "s2-local", "--root", str(world["wt"]), "--to", "s1-local", "--reply-to", first["event"], "yes, after lunch")
    assert ok.returncode == 0 and ok.stdout.startswith("sent to s1-local"), (ok.stdout, ok.stderr)
    got = [m for m in bridge("beat", world["a"], "s1-local", mail_recent=True)["mail"] if m["text"] == "yes, after lunch"]
    assert len(got) == 1 and got[0]["depth"] == 1, got
    bad = run("--session", "s2-local", "--root", str(world["wt"]), "--to", "", "nothing")
    assert bad.returncode == 1 and bad.stdout.startswith("not sent"), (bad.stdout, bad.stderr)
