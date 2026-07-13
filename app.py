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

from blocks import build_audit_blocks, build_forget_blocks
from cortex_client import CortexClient
from llm import GeneratedReply, canned_reply, generate_reply
from rts import (
    is_rts_enabled,
    search_history_fallback,
    search_realtime,
    should_search_realtime,
)

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


def _team_namespace_for(channel_id: str) -> str:
    """A second, shared namespace per channel — reuses Cortex's existing
    (already tested) multi-tenant namespace isolation to give a channel its
    own pool of memory that any member can recall from, not just the person
    who stated it. Only meaningful in channels, not DMs (no "team" in a 1:1)."""
    return f"slack:team:{channel_id}"


def _forget_candidates(namespace: str, query: str, k: int = 8) -> list[dict]:
    """Find memories to forget by BOTH semantic recall AND case-insensitive
    substring match. Recall alone silently misses short/exact keywords — e.g.
    `/memory-forget terse` never clears the embedding-similarity threshold
    against "...terse code reviews...", so the substring pass catches what a
    user literally typed. Semantic hits come first (best match), then any
    substring-only hits, deduped by id."""
    matches = cortex.recall(namespace, query, k=k)
    seen = {m.get("id") for m in matches if m.get("id")}
    ql = query.lower()
    for m in cortex.list_memories(namespace):
        if m.get("id") in seen:
            continue
        if ql in (m.get("content", "") or "").lower():
            matches.append(m)
            seen.add(m.get("id"))
    return matches


def process_message(client, say, *, user_id: str, text: str, thread_ts: str | None,
                     action_token: str | None, channel_types: list[str] | None,
                     channel_id: str | None, is_dm: bool) -> None:
    """The shared pipeline for both @mentions and DMs:

      1. Cortex /recall  — pull relevant prior context: this person's own
         memory, plus (in a channel, not a DM) the channel's shared memory
      2. Real-Time Search — only if the question smells like it needs
         fresh/current workspace info that memory can't supply
      3. Generate a reply — Claude if configured, else Cortex /chat if
         *it* has a model key, else an honest canned reply
      4. Cortex /remember — store anything durable that came out of this
         turn, personal vs. team as the reply generator tagged it
    """
    text = text.strip()
    if not text:
        say(text="I didn't catch a message there — try again?", thread_ts=thread_ts)
        return

    namespace = _namespace_for(user_id)
    team_namespace = None if is_dm or not channel_id else _team_namespace_for(channel_id)

    memories = cortex.recall(namespace, text, k=5)
    for m in memories:
        m["_scope"] = "personal"
    if team_namespace:
        team_memories = cortex.recall(team_namespace, text, k=5)
        for m in team_memories:
            m["_scope"] = "team"
        memories = memories + team_memories
    log.info("recall() -> %d memories (namespace=%s, team_namespace=%s)", len(memories), namespace, team_namespace)

    search_results: list[dict] = []
    if should_search_realtime(text, had_memory_hits=bool(memories)):
        # Prefer Slack's semantic Real-Time Search when the workspace has AI
        # search (Business+/Enterprise+); otherwise fall back to a keyword+
        # recency scan of conversations.history so live grounding still works
        # on any plan. RTS returning nothing also drops through to the fallback.
        if is_rts_enabled(client):
            search_results = search_realtime(client, text, action_token, channel_types=channel_types)
            log.info("Real-Time Search -> %d hits", len(search_results))
        if not search_results:
            search_results = search_history_fallback(client, text, channel_id)
            if search_results:
                log.info("History fallback -> %d hits", len(search_results))

    reply = _generate(namespace, team_namespace, user_id, text, memories, search_results)

    if reply.forget_query:
        # Conversational forget-intent, detected by the same LLM that just
        # replied — search now (the LLM only flagged intent, it doesn't have
        # recall results for the *forget* query itself) and hand back the
        # same confirm-button UI /memory-forget uses, so a "yes, delete" is
        # still a deliberate click, not something the model did on its own.
        forget_matches = _forget_candidates(namespace, reply.forget_query, k=5)
        for m in forget_matches:
            m["_namespace"] = namespace
            m["_scope"] = "personal"
        if team_namespace:
            team_forget_matches = _forget_candidates(team_namespace, reply.forget_query, k=5)
            for m in team_forget_matches:
                m["_namespace"] = team_namespace
                m["_scope"] = "team"
            forget_matches = forget_matches + team_forget_matches
        log.info("Conversational forget-request '%s' -> %d candidate matches", reply.forget_query, len(forget_matches))
        blocks = build_forget_blocks(reply.forget_query, forget_matches, _FORGET_VALUE_SEP)
        say(text=reply.text, blocks=blocks, thread_ts=thread_ts)
        return

    say(text=reply.text, thread_ts=thread_ts)


def _store_extracted_memories(reply: GeneratedReply, namespace: str, team_namespace: str | None, user_id: str) -> None:
    for m in reply.memories_to_store:
        scope = m.get("scope", "personal")
        target_ns = team_namespace if (scope == "team" and team_namespace) else namespace
        cortex.remember(
            target_ns,
            m["content"],
            kind=m.get("kind", "fact"),
            salience=float(m.get("salience", 0.5)),
            source=f"slack-dm:{user_id}",
        )


def _generate(namespace: str, team_namespace: str | None, user_id: str, text: str,
              memories: list[dict], search_results: list[dict]) -> GeneratedReply:
    has_compat = os.environ.get("OPENAI_COMPAT_BASE_URL") and os.environ.get("OPENAI_COMPAT_API_KEY")
    if has_compat or os.environ.get("ANTHROPIC_API_KEY"):
        reply = generate_reply(text, memories, search_results)
        _store_extracted_memories(reply, namespace, team_namespace, user_id)
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
        channel_id=event.get("channel"),
        is_dm=False,
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
        channel_id=event.get("channel"),
        is_dm=True,
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
# /memory-forget slash command — the correction/deletion half of the audit
# story. /memory-audit shows what's remembered; this is how you actually do
# something about it, closing the "you can see it but not touch it" gap.
# ---------------------------------------------------------------------------

# Slack button values are plain strings — encode both fields SlackMind needs
# to call Cortex's DELETE (namespace + memory_id) into one, since a button
# has no separate metadata field of its own to carry them in.
# Separator for packing namespace|memory_id|content into a button value.
# Must be PRINTABLE: Slack strips non-printable control chars (e.g. \x1f) from
# button values on the round-trip, which silently breaks the unpack. Neither the
# namespace (slack:...) nor the hex memory_id contains "|"; content is unpacked
# last with maxsplit=2, so a "|" inside content is harmless.
_FORGET_VALUE_SEP = "|"


@app.command("/memory-forget")
def handle_memory_forget(ack, respond, command):
    ack()
    user_id = command["user_id"]
    namespace = _namespace_for(user_id)
    query = command.get("text", "").strip()

    if not query:
        respond(
            response_type="ephemeral",
            text="Usage: `/memory-forget <keyword>` — e.g. `/memory-forget terse code reviews`. "
            "I'll show you matching memories with a button to forget each one.",
        )
        return

    matches = _forget_candidates(namespace, query)
    for m in matches:
        m["_namespace"] = namespace
        m["_scope"] = "personal"
    blocks = build_forget_blocks(query, matches, _FORGET_VALUE_SEP)
    respond(
        response_type="ephemeral",  # only the requester sees this — deletion is personal
        text=f"Found {len(matches)} memories matching '{query}'",
        blocks=blocks,
    )


@app.action("forget_memory")
def handle_forget_button(ack, body, respond):
    ack()
    value = body["actions"][0]["value"]
    namespace, memory_id, content = value.split(_FORGET_VALUE_SEP, 2)

    result = cortex.delete_memory(namespace, memory_id)
    if result is not None:
        respond(
            response_type="ephemeral",
            replace_original=False,
            text=f"✅ Forgotten: _{content}_",
        )
    else:
        respond(
            response_type="ephemeral",
            replace_original=False,
            text=f"⚠️ Couldn't forget that one — it may already be gone. (`{memory_id}`)",
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
    if os.environ.get("OPENAI_COMPAT_BASE_URL") and os.environ.get("OPENAI_COMPAT_API_KEY"):
        log.info(
            "Reply generation: OpenAI-compatible endpoint at %s (model=%s)",
            os.environ["OPENAI_COMPAT_BASE_URL"],
            os.environ.get("OPENAI_COMPAT_MODEL", "gpt-4o-mini"),
        )
    elif os.environ.get("ANTHROPIC_API_KEY"):
        log.info("Reply generation: direct Claude call (ANTHROPIC_API_KEY set)")
    else:
        log.info("Reply generation: no LLM key configured — will try Cortex /chat, then fall back to canned replies")

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
