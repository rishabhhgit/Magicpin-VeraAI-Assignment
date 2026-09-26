"""Optional multi-turn handlers (brief §7.4) — thin adapter over `vera.reply_engine`.

    from conversation_handlers import respond, register_context

    register_context("category", "dentists", category_dict)
    register_context("merchant", "m_001_drmeera_dentist_delhi", merchant_dict)

    out = respond(state, "not interested")      # -> {"action": ..., "body": ...}

`state` may be the dict used by the HTTP bot ({conversation_id, sent, stage, ...}),
any object with those attributes, or a conversation dict passed straight through.
Contexts may also be carried on the state itself (state["context"] = {...}) — they
are registered on arrival, so the module is self-contained for replay tests.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from vera.reply_engine import respond as _respond
from vera.store import Store

_STORE = Store()
register_context = _STORE.put_context
wipe = _STORE.wipe


def register_bundle(category: Optional[dict] = None, merchant: Optional[dict] = None,
                    customer: Optional[dict] = None) -> None:
    """Register the contexts a replay scenario will need (missing ones are skipped)."""
    if isinstance(category, dict) and category.get("slug"):
        _STORE.put_context("category", category["slug"], 1, category)
    if isinstance(merchant, dict) and merchant.get("merchant_id"):
        _STORE.put_context("merchant", merchant["merchant_id"], 1, merchant)
    if isinstance(customer, dict) and customer.get("customer_id"):
        _STORE.put_context("customer", customer["customer_id"], 1, customer)


def _as_conv(state: Any) -> Dict[str, Any]:
    if isinstance(state, dict):
        conv = dict(state)
    elif state is None:
        conv = {}
    else:
        conv = {}
        for key in ("conversation_id", "sent", "stage", "merchant_id", "customer_id",
                    "category", "merchant", "customer", "context"):
            value = getattr(state, key, None)
            if value is not None:
                conv[key] = value
    ctx = conv.get("context") or {}
    if isinstance(ctx, dict):
        register_bundle(ctx.get("category"), ctx.get("merchant"), ctx.get("customer"))
    for key in ("category", "merchant", "customer"):
        if isinstance(conv.get(key), dict):
            register_bundle(**{key: conv[key]})
    conv.setdefault("conversation_id", conv.get("id") or "conv_replay")
    conv.setdefault("sent", [])
    conv.setdefault("stage", 0)
    return conv


def respond(state: Any, merchant_message: str, from_role: str = "merchant",
            turn_number: Optional[int] = None) -> dict:
    """Given the conversation so far + the latest message, produce the next move.

    Returns {"action": "send"|"wait"|"end", ...} — the HTTP contract shape, so the
    same handler serves both replay tests and the /v1/reply endpoint.
    """
    conv = _as_conv(state)
    if turn_number is None:
        turn_number = int(conv.get("stage") or 0) + 1
    return _respond(conv, merchant_message, _STORE, from_role=from_role,
                    turn_number=turn_number)
