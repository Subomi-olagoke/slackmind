# SlackMind — Demo Script

Target: ~2 minutes, tight. Every beat below exists specifically to make one
of the genuinely differentiated features *visible*, since a casual viewer
can't see salience decay, contradiction detection, or namespace scoping just
by watching a normal reply — this script is deliberately staged to surface
the internals, not to have an organic conversation.

**Recording prerequisite — do this before hitting record, not after:**
Contradiction resolution (Beat 3) only produces a real, non-empty
"Contradictions Resolved" section in `/memory-audit` if Cortex has a real
`DASHSCOPE_API_KEY` configured — without one, the underlying `remember()`
call falls back to cosine-merge and the supersede event won't exist to show.
Confirm `curl http://localhost:8000/health` reports `"qwen_key": true`
before recording. If it's still `false` when you need to record, cut Beat 3
rather than fake it — an honest 4-beat demo beats a hollow 5-beat one where
the headline claim visibly doesn't work on screen.

---

## Beat 1 — Personal memory, the baseline (0:00–0:20)

DM SlackMind:

> "I prefer terse code reviews, no long explanations."

Reply lands (real LLM, not canned — confirm the key is set beforehand).
Then, in the same DM:

> "@SlackMind what do you know about how I like reviews?"

It recalls and states the preference back. **Say on camera:** "That's the
baseline every memory bot does. Here's what's actually different."

## Beat 2 — Team memory, not just personal (0:20–0:50)

Switch to a real channel (not the DM). Post:

> "we decided to ship the v2 release next Friday"

Then, **from a second account/user** if possible (or narrate "as a
teammate would ask"):

> "@SlackMind when are we shipping v2?"

It answers with the team-shared fact — **explicitly call out**: "I said
that from account A, this is account B asking, and it still knows — because
that was tagged as team knowledge, not locked to me personally." This is
the single most important 10 seconds of the whole video; it's the one
feature a viewer cannot mistake for a generic wrapper bot.

## Beat 3 — Reconciliation: memory vs. live reality (0:50–1:20)

*(Only if the DashScope key is live — see prerequisite above.)*

In the same channel, post an update that contradicts Beat 2's stated fact:

> "correction — v2 is actually shipping Monday now, not Friday"

Then ask again:

> "@SlackMind when's v2 shipping?"

The reply should explicitly flag the discrepancy it found via Real-Time
Search against what was stored, and give the corrected date. Immediately
run `/memory-audit` and scroll to **"♻️ Contradictions Resolved"** — show
the real, non-empty entry with the old value struck through. **Say on
camera:** "It didn't just answer with whichever fact it saw first — it
noticed the conflict, and the audit trail proves it, it's not just words in
a chat reply."

## Beat 4 — Correcting it back, conversationally, safely (1:20–1:50)

DM SlackMind:

> "actually, forget that I said I prefer terse reviews"

It replies acknowledging the request and posts a card with the matching
memory and a **Forget** button — **do not click confirm yet, show the
native Slack confirm dialog first** ("Forget this memory? / Forget it /
Cancel"). Click through it. **Say on camera:** "Even when it understood a
plain-language delete request, it never just deletes — a human click is
still what makes it real. That's the same UI whether you type a sentence or
run `/memory-forget` directly."

## Beat 5 — Close (1:50–2:00)

One line, on the architecture diagram or README open on screen: "Two of the
three technologies this hackathon names — a real MCP-pattern memory server,
and Slack's own Real-Time Search — not one. Provider-agnostic on the LLM
side. Fully auditable, fully reversible, and it scopes what it remembers to
who should actually see it."

---

## If Beat 3 has to be cut (DashScope key not ready in time)

Renumber to a 4-beat script (1, 2, 4, 5 above) and adjust the close line to
drop the "noticed the conflict" claim — replace with: "and everything it
does write is logged to an audit trail you can inspect with `/memory-audit`,
not a black box." Do not claim contradiction-resolution is live if it isn't
at recording time — a judge testing it after the fact and finding an empty
section is worse than not claiming it in the first place.
