"""SlackMind — a Slack agent with real long-term memory (via Cortex) and
real-time workspace grounding (via Slack's Real-Time Search API).

Architecture:
  Slack (Bolt, Socket Mode)
    -> Cortex REST API (POST /remember, POST /recall, POST /chat,
       GET /memories, GET /audit) over HTTP, at CORTEX_API_URL
    -> Slack assistant.search.context (Real-Time Search) for current-info
       questions memory can't answer
    -> Claude (claude-opus-4-8) for reply generation when ANTHROPIC_API_KEY
       is set; otherwise Cortex's own /chat, then a canned fallback — the
       app always runs, it just gets progressively less clever without keys.

Run with Socket Mode (no public URL needed):
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...
    export CORTEX_API_URL=http://localhost:8000
    python3 app.py
"""
from __future__ import annotations

import logging
import os
import re

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from blocks import build_audit_blocks
from cortex_client import CortexClient
from llm import GeneratedReply, canned_reply, generate_reply
from rts import is_rts_enabled, search_realtime, should_search_realtime

load_dotenv()  # picks up a local .env if present; real env vars still win

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("slackmind")

CORTEX_API_URL = os.environ.get("CORTEX_API_URL", "http://localhost:8000")
AGENT_NAME = "slackmind"

cortex = CortexClient(CORTEX_API_URL, agent=AGENT_NAME)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

_MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def _namespace_for(user_id: str) -> str:
    """One Cortex namespace per Slack user — memory follows the person
    across channels and DMs, which is the whole point of "long-term"."""
    return f"slack:{user_id}"


def process_message(client, say, *, user_id: str, text: str, thread_ts: str | None,
                     action_token: str | None, channel_types: list[str] | None) -> None:
    """The shared pipeline for both @mentions and DMs:

      1. Cortex /recall  — pull relevant prior context for this person
      2. Real-Time Search — only if the question smells like it needs
         fresh/current workspace info that memory can't supply
      3. Generate a reply — Claude if configured, else Cortex /chat if
         *it* has a model key, else an honest canned reply
      4. Cortex /remember — store anything durable that came out of this turn
    """
    text = text.strip()
    if not text:
        say(text="I didn't catch a message there — try again?", thread_ts=thread_ts)
        return

    namespace = _namespace_for(user_id)

    memories = cortex.recall(namespace, text, k=5)
    log.info("recall() -> %d memories for %s", len(memories), namespace)

    search_results: list[dict] = []
    if is_rts_enabled(client) and should_search_realtime(text, had_memory_hits=bool(memories)):
        search_results = search_realtime(client, text, action_token, channel_types=channel_types)
        log.info("Real-Time Search -> %d hits", len(search_results))

    reply = _generate(namespace, user_id, text, memories, search_results)

    say(text=reply.text, thread_ts=thread_ts)


def _generate(namespace: str, user_id: str, text: str, memories: list[dict],
              search_results: list[dict]) -> GeneratedReply:
    if os.environ.get("ANTHROPIC_API_KEY"):
        reply = generate_reply(text, memories, search_results)
        for m in reply.memories_to_store:
            cortex.remember(
                namespace,
                m["content"],
                kind=m.get("kind", "fact"),
                salience=float(m.get("salience", 0.5)),
                source=f"slack-dm:{user_id}",
            )
        return reply

    # No Claude key on the Slack side — try Cortex's own /chat, which does
    # recall + generate + store in one call if Cortex has its own model key.
    search_note = ""
    if search_results:
        lines = "\n".join(f"- {r.get('content', '')}" for r in search_results[:5])
        search_note = f"\n\nRelevant live workspace messages just found via search:\n{lines}"
    chat_system = (
        "You are SlackMind, a Slack agent with long-term memory. Use the user's "
        "remembered context when relevant; never invent it." + search_note
    )
    chat_resp = cortex.chat(namespace, text, system=chat_system)
    if chat_resp is not None:
        return GeneratedReply(text=chat_resp["answer"], memories_to_store=[], used_llm=True)

    # No LLM available anywhere in the stack — canned fallback, but still
    # exercise /remember with a simple heuristic so memory keeps growing.
    reply = canned_reply(memories, search_results)
    looks_like_statement = len(text) > 12 and not text.rstrip().endswith("?")
    if looks_like_statement:
        cortex.remember(
            namespace, text, kind="event", salience=0.4, source=f"slack-dm:{user_id}:canned"
        )
    return reply


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_app_mention(event, say, client):
    text = _strip_mention(event.get("text", ""))
    process_message(
        client,
        say,
        user_id=event["user"],
        text=text,
        thread_ts=event.get("thread_ts") or event.get("ts"),
        action_token=event.get("action_token"),
        channel_types=["public_channel", "private_channel"],
    )


@app.event("message")
def handle_dm(event, say, client):
    # app_mention doesn't fire for DMs — catch the generic "message" event
    # and filter to actual DM channels, ignoring bot/edit/delete noise.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    process_message(
        client,
        say,
        user_id=event["user"],
        text=event.get("text", ""),
        thread_ts=None,
        action_token=event.get("action_token"),
        channel_types=["im", "public_channel", "private_channel"],
    )


# ---------------------------------------------------------------------------
# /memory-audit slash command
# ---------------------------------------------------------------------------

@app.command("/memory-audit")
def handle_memory_audit(ack, respond, command):
    ack()
    user_id = command["user_id"]
    namespace = _namespace_for(user_id)
    memory_id_filter = command.get("text", "").strip() or None

    memories = cortex.list_memories(namespace)
    audit_log = cortex.audit(namespace, memory_id=memory_id_filter)

    blocks = build_audit_blocks(namespace, memories, audit_log, memory_id_filter)
    respond(
        response_type="in_channel",
        text=f"Memory audit for <@{user_id}> — {len(memories)} memories, {len(audit_log)} audit events",
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    health = cortex.health()
    if health:
        log.info("Cortex reachable at %s (qwen_key=%s)", CORTEX_API_URL, health.get("qwen_key"))
    else:
        log.warning(
            "Cortex not reachable at %s — memory recall/storage will silently no-op "
            "until it's running (start it with: uvicorn main:app --port 8000)",
            CORTEX_API_URL,
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY not set — will try Cortex /chat, then fall back to canned replies")

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
