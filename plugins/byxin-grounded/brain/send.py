"""Mail another session from the command line: the model's way to answer when the session has no `send` tool listed.

    python3 send.py --session <this session's id> --root <project folder> --to <session|name|all> [--reply-to <mail id>] "text"

The text may also come on stdin (give "-" as the text). It goes through bridge.py's own `send` op, so the record, the
reply depth and the warnings are the same as the tool's. Prints one line: what happened."""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--reply-to", default="")
    ap.add_argument("text", nargs="+")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.text == ["-"] else " ".join(a.text)
    req = {"op": "send", "session": a.session, "root": a.root, "to": a.to, "text": text, "by": "session",
           "in_reply_to": a.reply_to or None}
    ran = subprocess.run([sys.executable, os.path.join(HERE, "bridge.py")], input=json.dumps(req), cwd=HERE,
                         capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        out = json.loads(ran.stdout)
    except ValueError:
        print("not sent: the bridge failed (exit %d): %s" % (ran.returncode, ran.stderr[-400:]))
        return 1
    if not out.get("ok"):
        print("not sent: %s" % out.get("error"))
        return 1
    warn = out.get("warn") or []
    print("sent to %s through ByxIn's shared brain%s" % (out.get("to"), (" -- " + "; ".join(warn)) if warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
