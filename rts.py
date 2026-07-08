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
