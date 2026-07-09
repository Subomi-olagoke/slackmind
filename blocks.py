"""Block Kit formatting for /memory-audit — the hackathon's "Design" surface.

Rather than dumping raw JSON from Cortex's GET /audit + GET /memories, this
renders a readable, structured summary: recent memories with a visual
salience meter, an action-count breakdown of the audit trail, and a called-
out "contradictions resolved" section built from `supersede` audit events —
Cortex's word for "new info replaced an old, now-wrong fact."
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

_SALIENCE_BAR_LEN = 5
_KIND_EMOJI = {"fact": "🧩", "preference": "⭐", "event": "🗓️"}
_ACTION_EMOJI = {
    "create": "🆕",
    "merge": "🔗",
    "access": "👁️",
    "supersede": "♻️",
    "forget": "🗑️",
}


def _salience_bar(salience: float) -> str:
    filled = round(max(0.0, min(1.0, salience)) * _SALIENCE_BAR_LEN)
    return "●" * filled + "○" * (_SALIENCE_BAR_LEN - filled)


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "unknown time"
    try:
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%b %d, %H:%M UTC")
    except (ValueError, OSError, OverflowError):
        return "unknown time"


def build_audit_blocks(
    namespace: str,
    memories: list[dict],
    audit_log: list[dict],
    memory_id_filter: Optional[str] = None,
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🧠 SlackMind Memory Audit", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Namespace: `{namespace}`"
                    + (f"  •  filtered to memory `{memory_id_filter}`" if memory_id_filter else "")
                    + f"  •  {len(memories)} active memories  •  {len(audit_log)} audit events",
                }
            ],
        },
        {"type": "divider"},
    ]

    # --- Recent memories, most-recently-touched first ---
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Recent memories*"}})
    if not memories:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing stored yet._"}})
    else:
        sorted_mems = sorted(memories, key=lambda m: m.get("last_accessed", 0), reverse=True)
        for m in sorted_mems[:8]:
            emoji = _KIND_EMOJI.get(m.get("kind", "fact"), "🧩")
            bar = _salience_bar(m.get("salience", 0.6))
            content = m.get("content", "")
            meta = (
                f"salience {bar} {m.get('salience', 0):.2f}  •  "
                f"accessed {m.get('access_count', 0)}x  •  "
                f"last touched {_fmt_ts(m.get('last_accessed'))}"
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{emoji} *{content}*\n_{meta}_"},
                }
            )

    blocks.append({"type": "divider"})

    # --- Audit trail action-count summary ---
    counts: dict[str, int] = {}
    for e in audit_log:
        counts[e.get("action", "?")] = counts.get(e.get("action", "?"), 0) + 1
    if counts:
        count_line = "   ".join(
            f"{_ACTION_EMOJI.get(action, '•')} {action}: *{n}*" for action, n in sorted(counts.items())
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Audit trail activity*\n{count_line}"},
            }
        )
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Audit trail activity*\n_No activity recorded yet._"}})

    # --- Contradictions resolved (supersede events) ---
    supersedes = [e for e in audit_log if e.get("action") == "supersede"]
    if supersedes:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*♻️ Contradictions resolved*"}})
        for e in supersedes[-5:]:
            reason = e.get("reason", "superseded")
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"~{e.get('content', '')}~\n_{reason} · {_fmt_ts(e.get('at'))}_",
                    },
                }
            )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Generated {_fmt_ts(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())} · powered by Cortex + SlackMind",
                }
            ],
        }
    )
    return blocks


def build_forget_blocks(
    query: str,
    matches: list[dict],
    value_sep: str,
) -> list[dict]:
    """Forget-flow results (from either the /memory-forget command or a
    conversational "forget that I said X"): each match gets its own section
    + a "Forget" button. Each match dict must carry its own `_namespace`
    (personal and team-scoped matches can be mixed in one list, and each
    needs its correct source namespace for the delete call to work) plus the
    usual `id`/`content`/`kind` from Cortex. The button's value packs
    namespace/memory_id/content together (Slack buttons carry a single
    string, no separate metadata slot) so the click handler can call
    Cortex's DELETE and still show a human-readable confirmation without a
    second round-trip."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🗑️ Forget a memory", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Matches for *{query}*"}],
        },
        {"type": "divider"},
    ]

    if not matches:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing matched that — nothing to forget._"}}
        )
        return blocks

    for m in matches:
        emoji = _KIND_EMOJI.get(m.get("kind", "fact"), "🧩")
        scope_tag = "team" if m.get("_scope") == "team" else "personal"
        content = m.get("content", "")
        memory_id = m.get("id", "")
        match_namespace = m.get("_namespace", "")
        # Truncate the content carried in the button value defensively — Slack
        # caps block/action text length, and a very long memory shouldn't be
        # able to break the round trip.
        packed_content = content[:150]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{emoji} {content}\n_[{scope_tag}]_"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Forget", "emoji": True},
                    "style": "danger",
                    "action_id": "forget_memory",
                    "value": f"{match_namespace}{value_sep}{memory_id}{value_sep}{packed_content}",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Forget this memory?"},
                        "text": {"type": "mrkdwn", "text": f"_{content}_"},
                        "confirm": {"type": "plain_text", "text": "Forget it"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            }
        )
    return blocks
