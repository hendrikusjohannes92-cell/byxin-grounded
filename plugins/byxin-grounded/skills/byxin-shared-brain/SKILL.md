---
name: byxin-shared-brain
description: Use when the user asks what other Claude Code sessions in this project did or are doing, wants to leave a note for every session, asks who is editing a file, wants to correct a wrong record of a session, or wants to share the project's brain with sessions on other machines. Covers the /byxin commands of the byxin-grounded plugin.
---

# ByxIn's shared brain

Every Claude Code session working in the same repository writes what it did into one shared record: what was
asked, which files were edited, whether the answer was verified, notes people left, and corrections. This skill
says how to read and use that record. It is a record of sessions, not a fact about the code: read a file before
relying on what another session did to it.

## Steps

1. **To see what others did:** run `/byxin brain`. It lists the sessions working right now (and what they are
   editing), the notes left for every session, each recent session's turns with what it edited or "edited
   nothing", the corrections, and answers left unverified.
2. **Before editing a shared file:** run `/byxin sessions`. If another live session is editing the same file,
   tell the user and read the file again before changing it.
3. **To leave something for every later session:** `/byxin note <text>`. A note is told by a person; write it in
   the user's words, one fact or decision per note. `/byxin notes` lists them.
4. **When a record is wrong:** `/byxin events` lists the record with ids; `/byxin retract <id> <why>` marks one
   wrong with the reason. Never ask to delete a record: the wrong one stays, marked.
5. **To share with sessions on other machines or in the cloud:** `/byxin share on`, then commit
   `.byxin/share.json`. `/byxin share status` and `/byxin share sync` check and move it by hand.
6. **To see which brain serves this project:** `/byxin where`.

## How to speak about it

- Say where a claim about another session comes from: "the shared record says session 3f2a… edited parser.py".
- "Asked" is not "done": only a turn that lists an edited file edited it.
- When the record holds nothing for a question, say so plainly; do not fill the gap.
