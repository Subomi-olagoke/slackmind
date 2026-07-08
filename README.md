# SlackMind

A Slack agent with **real long-term memory** and **real-time workspace
grounding** — built for the Slack Agent Builder Challenge, submitting to the
**"New Slack Agent"** track (deadline Jul 13 2026, 5pm PDT).

## The concept

Every AI assistant bolted onto Slack today has the same problem: it forgets
you the moment the conversation scrolls away. Ask it something on Monday,
re-explain the same context on Wednesday. SlackMind fixes that by pairing
Slack's own Bolt SDK with two things:

1. **[Cortex](../cortex)** — a sibling project, a self-pruning, multi-tenant
   memory server with a real governance model: new facts *dedupe* against
   existing ones instead of piling up, recall *reinforces* what matters
   (salience grows with use), contradictory information *supersedes* the old
   fact instead of leaving stale data around, and unused memories *decay and
   get forgotten* on a salience-scaled half-life — with every one of those
   lifecycle events written to a queryable audit trail. Cortex runs as its
   own FastAPI process; SlackMind talks to it purely over its REST API
   (`POST /remember`, `POST /recall`, `POST /chat`, `GET /memories`,
   `GET /audit`) — never by trying to spawn Cortex's MCP stdio server as a
   subprocess, which isn't practical across a Slack-bot/memory-server
   process boundary.
2. **Slack's Real-Time Search API** (`assistant.search.context`) — for the
   other half of "the bot doesn't know things": questions that need *fresh,
   in-workspace* context (what did the team just decide in #proj-gizmo?)
   rather than durable facts about the person asking. Memory answers "what
   do you know about me"; Real-Time Search answers "what's happening in this
   workspace right now."

The result: mention SlackMind or DM it, and it answers using both what it
remembers about *you* (across every channel and DM, forever, until it
naturally decays) and what's actually happening in the workspace *today*.

## How a message flows

```
Slack message (@mention or DM)
        │
        ▼
Cortex POST /recall  ──────────► relevant memories for this person
        │
        ▼
looks like it needs fresh info? ──► Slack assistant.search.context
        │                                (Real-Time Search)
        ▼
generate a reply:
  1. ANTHROPIC_API_KEY set?      → Claude (claude-opus-4-8), given memory +
                                    search context, also emits a structured
                                    memory-extraction block
  2. else: Cortex POST /chat     → Cortex's own recall+generate+store,
     (if Cortex has a model key)   using ITS model key instead
  3. else: canned reply          → still surfaces memory/search hits, and
                                    stores a coarse heuristic memory so the
                                    system keeps demonstrably working with
                                    zero LLM keys configured anywhere
        │
        ▼
Cortex POST /remember  ─────────► whatever came out of this turn worth
                                    keeping long-term
```

`/memory-audit` is the separate, always-on introspection surface: it calls
Cortex's `GET /memories` + `GET /audit` and renders a Block Kit summary —
recent memories with a salience meter, an audit-trail activity breakdown,
and a called-out "contradictions resolved" section built from `supersede`
events.

## Files

| File | Purpose |
|---|---|
| `app.py` | Bolt app (Socket Mode): `app_mention` + DM handlers, `/memory-audit` command, the message pipeline |
| `cortex_client.py` | REST client for Cortex's exact endpoints/shapes (`/remember`, `/recall`, `/chat`, `/memories`, `/audit`, `/health`) |
| `rts.py` | Slack Real-Time Search integration (`assistant.search.context` / `.info`) + the heuristic for when to trigger it |
| `llm.py` | Claude reply generation (`claude-opus-4-8`) + memory-extraction parsing + the canned no-LLM fallback |
| `blocks.py` | Block Kit rendering for `/memory-audit` |
| `manifest.yaml` | Slack app manifest with every OAuth scope the code above actually uses |
| `requirements.txt` / `.env.example` / `.gitignore` | Standard project plumbing |

## Setup

### 1. Run Cortex

SlackMind expects Cortex running as its own process:

```bash
cd "../cortex"
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 2. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Paste in `manifest.yaml` from this repo.
3. Under **Socket Mode**, enable it and generate an **app-level token** with
   the `connections:write` scope — this is your `SLACK_APP_TOKEN` (`xapp-...`).
4. **Install the app** to your workspace to get the bot token
   (`SLACK_BOT_TOKEN`, `xoxb-...`).
5. Under **OAuth & Permissions**, confirm the granular `search:read.*`
   scopes from the manifest were actually granted — Slack's Real-Time Search
   API needs them, and it's worth double-checking your dev workspace has AI
   search enabled at all (the app checks `assistant.search.info` itself at
   runtime and just skips search gracefully if not).

### 3. Configure environment

```bash
cp .env.example .env
# fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN
# CORTEX_API_URL defaults to http://localhost:8000
# ANTHROPIC_API_KEY is optional — see fallback chain above
```

### 4. Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

No public URL or ngrok needed — Socket Mode means Slack pushes events over
an outbound websocket the app opens itself.

### 5. Try it

- `@SlackMind what's my favorite editor?` (after telling it once — it should
  remember across channels and DMs)
- DM it directly — DMs work the same as mentions, no `@` needed
- `/memory-audit` — see everything it currently remembers about you, with
  the full audit trail

## Judging rubric mapping

**Technological Implementation** — SlackMind uses *two* of the hackathon's
three named technologies, not one: a real memory server integrated over its
actual REST contract (not a mocked API — `cortex_client.py`'s request/
response shapes are read directly from Cortex's `main.py`), plus Slack's own
Real-Time Search API (`assistant.search.context`) for live grounding,
wired with the correct granular `search:read.*` scopes, the `action_token`
sourced from real event payloads, and a capability check
(`assistant.search.info`) before ever relying on it. Both Bolt (Python,
Socket Mode) and the Slack CLI-manifest workflow are used as intended.

**Design** — `/memory-audit` is the concrete UX surface for this criterion:
a Block Kit layout (header, salience meters, an audit-activity breakdown,
a dedicated "contradictions resolved" section) instead of a raw JSON or
plain-text dump of what the bot knows.

**Potential Impact** — "I have to re-explain my context every conversation"
is a universal pain in any busy Slack workspace, not a novelty demo. Memory
that follows a person across channels/DMs and decays what's no longer
relevant is directly useful in any team, immediately, with no bespoke setup
per use case.

**Quality of Idea** — most "AI + memory" bots are a thin wrapper around a
vector DB: embed everything, cosine-similarity it back, done, no notion of
what should be forgotten or what happens when two stored facts disagree.
Cortex's approach is more rigorous: dedupe-on-write, reinforcement-on-recall,
explicit contradiction resolution via `supersede`, salience-scaled decay,
and a compliance-grade audit trail for every one of those lifecycle events —
exactly the kind of governance a real, long-running workspace agent needs
and a plain vector store doesn't give you for free.

## Notes on the fallback chain

Every piece of this app is designed to run and be demoable with zero
external LLM keys:

- No `ANTHROPIC_API_KEY` and Cortex has no `DASHSCOPE_API_KEY` either → the
  canned-reply path still calls `/recall`, surfaces what it finds, and
  exercises `/remember` with a coarse heuristic ("this looks like a
  statement worth keeping"), so the whole memory loop is still visibly
  running.
- No `ANTHROPIC_API_KEY` but Cortex *does* have a model key → SlackMind
  transparently defers reply generation to Cortex's own `POST /chat`, which
  does recall + generate + store in a single call.
- `ANTHROPIC_API_KEY` set → the full pipeline: Claude sees retrieved memory
  and any Real-Time Search hits, answers, and emits a structured
  memory-extraction block that SlackMind parses and writes back via
  `POST /remember`.
