"""Bounded projections of persisted conversation evidence, without a model call.

Resolve ownership before calling these helpers. Never summarize or cut a source
sentence: a suffix keeps later corrections/withdrawals with their exact wording.
The stored messages remain the source of truth across refresh and restart.
"""

RECENT_MESSAGES = 28
RECENT_CHARACTERS = 24000
ORDER_CHARACTERS = 12000


def evidence_suffix(records, *, max_characters=ORDER_CHARACTERS):
    """Return the newest contiguous source suffix and an explicit omission flag.

    Unlike an all-or-nothing count limit, a ninth statement keeps its evidence.
    Do not skip a long newer record in order to fit an older, superseded claim.
    """
    selected, used = [], 0
    for record in reversed(records):
        size = len(record.get('content', '')) + (1 if selected else 0)
        if used + size > max_characters:
            break
        selected.append(record)
        used += size
    selected.reverse()
    return selected, len(selected) < len(records)


def recent_messages(messages):
    eligible = [m for m in messages if m.get('status') not in ('failed', 'cancelled')]
    return evidence_suffix(eligible[-RECENT_MESSAGES:], max_characters=RECENT_CHARACTERS)[0]


def intent_snapshot(intents):
    """Keep every open task but do not resend its ever-growing source transcript.

    Exact source rows remain available to order evidence retrieval. The current
    text, scope, constraints and status still describe each outstanding task.
    """
    return [{key: value for key, value in intent.items() if key != 'source_texts'}
            for intent in intents if intent.get('status') in ('pending', 'needs_info')]
