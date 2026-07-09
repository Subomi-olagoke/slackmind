"""Reply generation, with two possible LLM backends and a canned fallback
so SlackMind runs end-to-end even with no LLM key configured at all:

  1. OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY — any OpenAI-compatible
     endpoint (used first, if configured).
  2. ANTHROPIC_API_KEY — a direct Claude call via the anthropic SDK.
  3. canned_reply() — no LLM anywhere; still surfaces memory/search results.

Model choice for the Anthropic path: `claude-opus-4-8` is Anthropic's
current default recommendation absent an explicit request for a
cheaper/faster tier — this is a low-volume Slack Q&A bot, not a
high-throughput pipeline, so there's no cost pressure that would justify
stepping down to Sonnet/Haiku on our own initiative.
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
    previously stored, retrieved because they're relevant to the current
    message. Each is tagged [personal] (specific to this individual) or
    [team] (shared knowledge for this channel).
  - LIVE SEARCH RESULTS: fresh messages from this Slack workspace, pulled via
    Slack's Real-Time Search API, when the question needs current
    in-workspace context memory alone can't supply.

Answer the person's message naturally and helpfully, grounding your answer in
MEMORY and LIVE SEARCH RESULTS when they're relevant. Never invent memory —
if you don't have relevant context, just answer from the conversation itself.
Keep replies conversational and Slack-appropriate (concise, no walls of text).

RECONCILIATION: if a LIVE SEARCH RESULT reveals that something in MEMORY is
now outdated or contradicted (e.g. memory says a deadline is Friday, but a
recent message says it moved to Monday), say so explicitly in your reply —
don't silently pick one and ignore the conflict. Then include the corrected
fact in the memory-extraction block below with high salience; Cortex's own
contradiction-detection will supersede the stale memory automatically, the
same as it would for a correction stated directly by a person — you do not
need to (and should not) reference a memory by id, just state the corrected
fact plainly, exactly as you would for anything else worth remembering.

After your reply, on a new line, emit a memory-extraction block so Cortex can
learn from this exchange. Only include GENUINELY durable, reusable facts,
preferences, or decisions worth remembering long-term — skip small talk,
one-off logistics, and anything already present in MEMORY. It is normal and
expected for this list to be empty most of the time.

Mark each extracted item's scope:
  - "personal" — specific to this individual (their preferences, their own
    situation). Default when unsure.
  - "team" — a shared fact/decision relevant to anyone in this channel (a
    deadline, a decision, a convention the team agreed on). Only use "team"
    for things a teammate other than this person would also want recalled.

Format the block EXACTLY like this, with valid JSON (empty array if nothing
is worth storing):

<memories>{"memories": [{"content": "...", "kind": "fact|preference|event", "salience": 0.0-1.0, "scope": "personal|team"}]}</memories>

FORGETTING: if — and only if — the person explicitly asks you to forget,
delete, or stop remembering something, with no replacement fact given, emit
a second block naming what to search for:

<forget_request>{"query": "..."}</forget_request>

Do not emit this for a correction (someone giving an updated fact to replace
an old one — that's the memory-extraction block above, not this one; Cortex's
contradiction-detection handles corrections automatically). Only use this
when they want something actually removed, with nothing to replace it.
Omit this block entirely (don't emit empty tags) when it doesn't apply — it
should be rare. When you do emit it, your visible reply should acknowledge
you're checking (e.g. "Let me see what I've got on that...") rather than
claim something specific was forgotten — the actual matching memories get
looked up and shown for confirmation in a separate step after your reply,
which you have no visibility into yet.
"""

_MEMORY_BLOCK_RE = re.compile(r"<memories>(.*?)</memories>", re.DOTALL)
_FORGET_BLOCK_RE = re.compile(r"<forget_request>(.*?)</forget_request>", re.DOTALL)


@dataclass
class GeneratedReply:
    text: str
    memories_to_store: list[dict] = field(default_factory=list)
    forget_query: Optional[str] = None
    used_llm: bool = False


def _openai_compat_client():
    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL")
    api_key = os.environ.get("OPENAI_COMPAT_API_KEY")
    if not (base_url and api_key):
        return None
    try:
        import openai
    except ImportError:
        log.warning("openai package not installed but OPENAI_COMPAT_BASE_URL/API_KEY are set")
        return None
    return openai.OpenAI(base_url=base_url, api_key=api_key)


def _anthropic_client():
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
        mem_lines = "\n".join(
            f"- [{m.get('_scope', 'personal')}] {m['content']} (salience {m.get('salience', '?')})"
            for m in memories
        )
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


def _parse_reply(raw_text: str) -> tuple[str, list[dict], Optional[str]]:
    """Strip both possible tag blocks out of the visible reply and parse
    each independently — they're optional and unordered relative to each
    other, so this doesn't assume one implies or precedes the other."""
    mem_match = _MEMORY_BLOCK_RE.search(raw_text)
    forget_match = _FORGET_BLOCK_RE.search(raw_text)

    cut_points = [m.start() for m in (mem_match, forget_match) if m is not None]
    visible = raw_text[: min(cut_points)].strip() if cut_points else raw_text.strip()

    memories: list[dict] = []
    if mem_match:
        try:
            payload = json.loads(mem_match.group(1))
            memories = [m for m in payload.get("memories", []) if isinstance(m, dict) and m.get("content")]
        except (json.JSONDecodeError, AttributeError):
            log.info("Could not parse memory-extraction block; storing nothing this turn")

    forget_query: Optional[str] = None
    if forget_match:
        try:
            payload = json.loads(forget_match.group(1))
            q = payload.get("query")
            forget_query = q.strip() if isinstance(q, str) and q.strip() else None
        except (json.JSONDecodeError, AttributeError):
            log.info("Could not parse forget-request block; ignoring")

    return visible or "…", memories, forget_query


def generate_reply(
    user_text: str,
    memories: list[dict],
    search_results: list[dict],
) -> GeneratedReply:
    """Try the OpenAI-compatible endpoint first (if configured), then a
    direct Claude call, then fall back to a canned reply. Each stage is
    independent — a failure at one falls through to the next rather than
    surfacing an error to the Slack user.
    """
    context_block = _build_context_block(memories, search_results)
    user_content = f"{context_block}\n\n---\n\nMessage: {user_text}"

    compat_client = _openai_compat_client()
    if compat_client is not None:
        model = os.environ.get("OPENAI_COMPAT_MODEL", "gpt-4o-mini")
        try:
            response = compat_client.chat.completions.create(
                model=model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            raw_text = response.choices[0].message.content or ""
            visible_text, extracted, forget_query = _parse_reply(raw_text)
            return GeneratedReply(text=visible_text, memories_to_store=extracted, forget_query=forget_query, used_llm=True)
        except Exception as e:
            log.warning("OpenAI-compatible LLM call failed (%s) — trying next fallback", e)

    anthropic_client = _anthropic_client()
    if anthropic_client is not None:
        try:
            response = anthropic_client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = "".join(block.text for block in response.content if block.type == "text")
            visible_text, extracted, forget_query = _parse_reply(raw_text)
            return GeneratedReply(text=visible_text, memories_to_store=extracted, forget_query=forget_query, used_llm=True)
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
