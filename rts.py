"""Slack Real-Time Search (RTS) API integration.

Wraps `assistant.search.context` (and its capability-check sibling
`assistant.search.info`) — Slack's Web API method for pulling fresh,
in-workspace context into an agent. This is a plain Web API call, not a
distinct REST product, so it goes through the same bot WebClient Bolt
already gives every handler; there is no dedicated `slack_sdk` typed
wrapper for it yet, so we use the generic `client.api_call()` escape hatch.

Key facts baked in here from Slack's own docs:
  - Bot-token calls need an `action_token`, sourced from a message/mention
    event payload (we only get called from handlers that have one).
  - `search:read.public` is the minimum bot-token scope; add
    `search:read.im` / `.private` / `.mpim` for those channel types.
  - Errors of note: `missing_scope`, `invalid_action_token`,
    `feature_not_enabled` (AI search off for the workspace — check with
    `assistant.search.info` first), `rate_limited`.
  - Per Slack's usage policy: query-then-discard. We never persist RTS
    results anywhere — they're used once to build a reply and dropped.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from slack_sdk.errors import SlackApiError

log = logging.getLogger("slackmind.rts")

# Heuristic trigger words for "this needs fresh, real-world info that a
# personal memory store can't possibly hold" — recency / current-events cues.
_RECENCY_PAT = re.compile(
    r"\b(latest|current(ly)?|today|this week|this month|right now|"
    r"just (announced|released|happened)|breaking|recent(ly)?|"
    r"what'?s new|update on|news about)\b",
    re.IGNORECASE,
)

_rts_enabled_cache: dict[str, bool] = {}

# Stopwords for the history-fallback keyword match — kept local to avoid a
# dependency, deliberately small (just the highest-frequency noise words).
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "it", "its",
    "and", "or", "not", "what", "when", "where", "who", "why", "how", "do",
    "does", "did", "for", "with", "on", "in", "at", "be", "this", "that",
    "i", "you", "we", "they", "me", "my", "our", "your", "about", "can",
    "will", "would", "should", "could", "have", "has", "had", "am", "get",
}


def should_search_realtime(text: str, had_memory_hits: bool) -> bool:
    """Decide whether a message warrants a live Slack search.

    Triggers when the text has a recency/current-events cue, OR when memory
    recall came back empty for what reads like a genuine question — memory
    alone couldn't answer it, so try grounding in live workspace content.
    """
    text = text or ""
    if _RECENCY_PAT.search(text):
        return True
    looks_like_question = text.strip().endswith("?") or text.strip().lower().startswith(
        ("what", "who", "when", "where", "why", "how", "is ", "are ", "did ", "does ")
    )
    return looks_like_question and not had_memory_hits


def is_rts_enabled(client, team_id: Optional[str] = None) -> bool:
    """Check assistant.search.info once per team and cache the result.

    Slack's docs are explicit: check this before relying on
    assistant.search.context, since AI search can simply be off for a
    given workspace (feature_not_enabled).
    """
    cache_key = team_id or "_default"
    if cache_key in _rts_enabled_cache:
        return _rts_enabled_cache[cache_key]
    enabled = False
    try:
        resp = client.api_call(api_method="assistant.search.info")
        enabled = bool(resp.get("is_ai_search_enabled", False))
    except SlackApiError as e:
        log.info("assistant.search.info unavailable (%s) — treating RTS as disabled", e.response.get("error"))
    except Exception as e:  # pragma: no cover - defensive
        log.info("assistant.search.info check failed: %s", e)
    _rts_enabled_cache[cache_key] = enabled
    return enabled


def search_realtime(
    client,
    query: str,
    action_token: Optional[str],
    channel_types: Optional[list[str]] = None,
    limit: int = 5,
) -> list[dict]:
    """Call assistant.search.context and return the message hits (or []).

    Fails soft: any SlackApiError (missing scope, bad/missing action_token,
    feature not enabled, rate limited, etc.) is logged and swallowed so a
    Slack-side hiccup never breaks the reply pipeline — RTS is a grounding
    enhancement, not a hard dependency.
    """
    if not action_token:
        log.info("No action_token on this event — skipping Real-Time Search")
        return []
    try:
        resp = client.api_call(
            api_method="assistant.search.context",
            json={
                "query": query,
                "action_token": action_token,
                "content_types": ["messages"],
                "channel_types": channel_types or ["public_channel", "private_channel"],
                "include_context_messages": False,
                "include_bots": False,
                "limit": limit,
            },
        )
        return resp.get("results", {}).get("messages", [])
    except SlackApiError as e:
        log.info("Real-Time Search failed (%s) — continuing without it", e.response.get("error"))
        return []
    except Exception as e:  # pragma: no cover - defensive
        log.warning("Real-Time Search unexpected error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Plan-independent fallback: conversations.history over the channels the bot
# can see. assistant.search.context is a Slack-AI feature gated to Business+/
# Enterprise+ plans (assistant.search.info reports is_ai_search_enabled=False
# elsewhere). This keeps the "live workspace grounding" half of SlackMind
# working on ANY plan — Free/Pro included — using standard Web API methods
# (channels:history / groups:history / im:history, all already granted), so
# the feature degrades instead of vanishing. Lower-fidelity than semantic
# search (keyword + recency, not embeddings), and honestly labelled as such.
# ---------------------------------------------------------------------------

def _keywords(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP and len(t) > 2}


def _member_channels(client, current_channel: Optional[str], max_channels: int) -> list[dict]:
    """The current channel first, then other channels the bot is a member of.
    Each item is {id, name}. Fails soft to just the current channel."""
    seen: dict[str, dict] = {}
    if current_channel:
        name = "this-channel"
        try:
            info = client.conversations_info(channel=current_channel)
            name = info.get("channel", {}).get("name") or name
        except Exception:
            pass
        seen[current_channel] = {"id": current_channel, "name": name}
    try:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True, limit=200,
        )
        for ch in resp.get("channels", []):
            if not ch.get("is_member"):
                continue
            if ch["id"] not in seen:
                seen[ch["id"]] = {"id": ch["id"], "name": ch.get("name", "channel")}
            if len(seen) >= max_channels:
                break
    except SlackApiError as e:
        log.info("conversations.list unavailable (%s) — scanning current channel only", e.response.get("error"))
    except Exception as e:  # pragma: no cover - defensive
        log.info("conversations.list failed (%s) — scanning current channel only", e)
    return list(seen.values())


def search_history_fallback(
    client,
    query: str,
    current_channel: Optional[str],
    limit: int = 5,
    max_channels: int = 6,
    per_channel: int = 60,
) -> list[dict]:
    """Approximate live workspace grounding without Slack AI: scan recent
    messages across the bot's channels, rank by query-keyword overlap
    (recency as tie-break), and return the top hits in the SAME shape RTS
    produces (content / author_name / channel_name / permalink) so the reply
    pipeline and canned fallback render them identically. Requires at least
    one keyword hit to include a message — no keyword match means no grounding
    injected, rather than dumping unrelated recent chatter into the reply.
    """
    kw = _keywords(query)
    if not kw:
        return []
    channels = _member_channels(client, current_channel, max_channels)
    scored: list[tuple[int, float, dict]] = []
    for ch in channels:
        try:
            hist = client.conversations_history(channel=ch["id"], limit=per_channel)
        except SlackApiError as e:
            log.info("conversations.history(%s) failed (%s) — skipping", ch["id"], e.response.get("error"))
            continue
        except Exception:  # pragma: no cover - defensive
            continue
        for msg in hist.get("messages", []):
            if msg.get("subtype") or msg.get("bot_id"):
                continue  # skip joins/leaves/edits and other bots
            text = msg.get("text", "")
            hits = len(kw & _keywords(text))
            if hits == 0:
                continue
            try:
                ts = float(msg.get("ts", 0))
            except (TypeError, ValueError):
                ts = 0.0
            scored.append((hits, ts, {
                "content": text,
                "user": msg.get("user", ""),
                "ts": msg.get("ts", ""),
                "channel_id": ch["id"],
                "channel_name": ch["name"],
            }))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [item for _, _, item in scored[:limit]]
    _hydrate(client, top)
    return top


_user_name_cache: dict[str, str] = {}


def _hydrate(client, results: list[dict]) -> None:
    """Best-effort author name + permalink for just the final results (≤limit
    calls each), so the fallback reads like the real thing without paying a
    lookup per scanned message."""
    for r in results:
        uid = r.get("user")
        if uid:
            if uid not in _user_name_cache:
                try:
                    info = client.users_info(user=uid)
                    prof = info.get("user", {}).get("profile", {})
                    _user_name_cache[uid] = (
                        prof.get("display_name") or prof.get("real_name") or uid
                    )
                except Exception:
                    _user_name_cache[uid] = uid
            r["author_name"] = _user_name_cache[uid]
        else:
            r["author_name"] = "someone"
        try:
            link = client.chat_getPermalink(channel=r["channel_id"], message_ts=r["ts"])
            r["permalink"] = link.get("permalink", "#")
        except Exception:
            r["permalink"] = "#"
