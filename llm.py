"""Reply generation: a real Claude call when ANTHROPIC_API_KEY is configured,
with a clear, still-useful canned fallback when it isn't — so SlackMind runs
end-to-end without any LLM key at all (it just gets less clever about what
it chooses to remember).

Model choice: `claude-opus-4-8` is Anthropic's current default recommendation
absent an explicit request for a cheaper/faster tier — this is a low-volume
Slack Q&A bot, not a high-throughput pipeline, so there's no cost pressure
that would justify stepping down to Sonnet/Haiku on our own initiative.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("slackmind.llm")

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """You are SlackMind, a Slack workspace agent with real long-term memory.

You are given:
  - MEMORY: durable facts/preferences/events Cortex (your memory server) has
    previously stored about this person, retrieved because they're relevant
    to the current message.
  - LIVE SEARCH RESULTS: fresh messages from this Slack workspace, pulled via
    Slack's Real-Time Search API, when the question needs current
    in-workspace context memory alone can't supply.

Answer the person's message naturally and helpfully, grounding your answer in
MEMORY and LIVE SEARCH RESULTS when they're relevant. Never invent memory —
if you don't have relevant context, just answer from the conversation itself.
Keep replies conversational and Slack-appropriate (concise, no walls of text).

After your reply, on a new line, emit a memory-extraction block so Cortex can
learn from this exchange. Only include GENUINELY durable, reusable facts,
preferences, or decisions worth remembering long-term — skip small talk,
one-off logistics, and anything already present in MEMORY. It is normal and
expected for this list to be empty most of the time.

Format the block EXACTLY like this, with valid JSON (empty array if nothing
is worth storing):

<memories>{"memories": [{"content": "...", "kind": "fact|preference|event", "salience": 0.0-1.0}]}</memories>
"""

_MEMORY_BLOCK_RE = re.compile(r"<memories>(.*?)</memories>", re.DOTALL)


@dataclass
class GeneratedReply:
    text: str
    memories_to_store: list[dict] = field(default_factory=list)
    used_llm: bool = False


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed but ANTHROPIC_API_KEY is set")
        return None
    return anthropic.Anthropic(api_key=api_key)


def _build_context_block(memories: list[dict], search_results: list[dict]) -> str:
    parts = []
    if memories:
        mem_lines = "\n".join(f"- {m['content']} (salience {m.get('salience', '?')})" for m in memories)
        parts.append(f"MEMORY:\n{mem_lines}")
    else:
        parts.append("MEMORY:\n(nothing relevant stored yet)")
    if search_results:
        search_lines = "\n".join(
            f"- [#{r.get('channel_name', '?')}] {r.get('author_name', 'someone')}: {r.get('content', '')}"
            for r in search_results
        )
        parts.append(f"LIVE SEARCH RESULTS:\n{search_lines}")
    return "\n\n".join(parts)


def _parse_reply(raw_text: str) -> tuple[str, list[dict]]:
    match = _MEMORY_BLOCK_RE.search(raw_text)
    if not match:
        return raw_text.strip(), []
    visible = raw_text[: match.start()].strip()
    try:
        payload = json.loads(match.group(1))
        memories = payload.get("memories", [])
        memories = [m for m in memories if isinstance(m, dict) and m.get("content")]
    except (json.JSONDecodeError, AttributeError):
        log.info("Could not parse memory-extraction block; storing nothing this turn")
        memories = []
    return visible or "…", memories


def generate_reply(
    user_text: str,
    memories: list[dict],
    search_results: list[dict],
) -> GeneratedReply:
    """The primary generation path: a real Claude call.

    Only invoked when ANTHROPIC_API_KEY is set — callers should fall back to
    `canned_reply()` (or Cortex's own /chat) otherwise.
    """
    client = _client()
    if client is None:
        return canned_reply(memories, search_results)

    context_block = _build_context_block(memories, search_results)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"{context_block}\n\n---\n\nMessage: {user_text}",
                }
            ],
        )
        raw_text = "".join(block.text for block in response.content if block.type == "text")
        visible_text, extracted = _parse_reply(raw_text)
        return GeneratedReply(text=visible_text, memories_to_store=extracted, used_llm=True)
    except Exception as e:
        log.warning("Claude call failed (%s) — falling back to canned reply", e)
        return canned_reply(memories, search_results)


def canned_reply(memories: list[dict], search_results: list[dict]) -> GeneratedReply:
    """A clear, honest fallback that still surfaces what memory/search found,
    so the bot stays useful (and demonstrably running) with zero LLM key
    configured anywhere in the stack.
    """
    lines = ["I don't have a language model configured right now, so I can't reason freely — "
             "but here's what I found for you:"]
    if memories:
        lines.append("\n*From what I remember about you:*")
        for m in memories[:3]:
            lines.append(f"• {m['content']}")
    else:
        lines.append("\n_I don't have any relevant memory stored yet._")
    if search_results:
        lines.append("\n*From the workspace right now:*")
        for r in search_results[:3]:
            lines.append(f"• <{r.get('permalink', '#')}|{r.get('author_name', 'someone')} in #{r.get('channel_name', '?')}>: {r.get('content', '')[:160]}")
    return GeneratedReply(text="\n".join(lines), memories_to_store=[], used_llm=False)
