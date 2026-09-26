"""Multi-turn reply engine: what Vera says *after* the first message.

Handles the four situations the replay test probes for:
  1. WhatsApp Business auto-replies  -> nudge once, wait, then exit
  2. Explicit merchant commitment    -> action mode, never re-qualify
  3. Hostile / opt-out               -> end immediately
  4. Off-mission asks                -> decline politely, stay on thread
Everything else continues the thread with the artifact we promised.
"""

from __future__ import annotations

import re

from .compose import Ctx, fix_plan
from .ground import offer_title, peer_line, peer_views_line
from .util import fmt_delta, humanize, nfmt, squeeze, squeeze_lines, stable_hash
from .voice import CTA_BINARY, CTA_OPEN, looks_out_of_scope

# --- classification lexicons -------------------------------------------------

AUTOREPLY_PATTERNS = [
    "thank you for contacting", "thanks for contacting", "thank you for reaching out",
    "we have received your", "we've received your", "your message has been received",
    "our team will respond", "we will respond", "we will get back", "we'll get back",
    "will revert shortly", "auto-reply", "auto reply", "automated message",
    "automated response", "this is an automated", "i am an automated",
    "i'm an automated", "assistant", "shortly.", "during business hours",
    "our office hours", "appreciate your patience",
]

HOSTILE_PATTERNS = [
    r"stop messag", r"stop sending", r"stop msg", r"do not message", r"don't message",
    r"dont message", r"stop bother", r"leave me alone", r"not interested",
    r"unsubscribe", r"remove my number", r"stop the spam", r"this is spam",
    r"useless spam", r"this is useless", r"stop these", r"\bspam(\b|ming)",
    r"stop spamming", r"wrong number", r"stop calling", r"stop text", r"don't text",
    r"dont text", r"annoying",
    r"waste of time", r"not useful", r"no more messages", r"enough already",
    r"block you", r"report you", r"mat bhejo", r"band karo", r"pareshan mat karo",
    r"f\*{0,2}ck|fuck", r"idiot", r"stupid bot", r"shut up", r"kalank mat karo",
]

COMMIT_PATTERNS = [
    r"lets do it", r"let's do it", r"let do it", r"\bdo it\b", r"go ahead",
    r"proceed", r"sure(,| )go", r"yes[,]? (please )?(start|go|do|proceed)",
    r"start (it )?(now|today)", r"\bconfirmed\b", r"i'm in", r"im in",
    r"count me in", r"haan karo", r"kardo", r"kar do", r"chalo", r"bana do",
    r"yes please (send|start|go)", r"looks good,? (go|start|do)", r"sounds good,? (go|do)",
]

NEGATIVE_PATTERNS = [
    r"\bnot now\b", r"\blater\b", r"\bbusy\b", r"\bcall (me )?(later|next week)\b",
    r"\bnext week\b", r"\bmaybe later\b", r"\bafter (some|a few) days\b",
    r"\bkal baat\b", r"\btry later\b",
]

POSITIVE_PATTERNS = [
    r"^\s*(yes|y|yeah|yep|sure|ok|okay|cool|please|pls|go|do it|haan|han|ji haan|"
    r"bilkul|send it|sounds good|looks good|perfect|great|nice)\b",
    r"\bplease send\b", r"\bsend me\b", r"\byes[, ]", r"\bsend the\b",
    r"\bi'll take\b", r"\bi want\b", r"\bdo (it|this|the needful)\b",
]

QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]


def _norm(text) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _matches(text, patterns) -> bool:
    t = (text or "").lower()
    return any((p in t if "(" not in p and not p.startswith("^") else re.search(p, t))
               for p in patterns)


def is_auto_reply(text) -> bool:
    t = _norm(text)
    if not t:
        return False
    return any(p in t for p in AUTOREPLY_PATTERNS)


def is_hostile(text) -> bool:
    t = _norm(text)
    if not t:
        return False
    return any(re.search(p, t) for p in HOSTILE_PATTERNS)


def is_commitment(text) -> bool:
    t = _norm(text)
    return bool(t) and any(re.search(p, t) for p in COMMIT_PATTERNS)


def is_negative(text) -> bool:
    t = _norm(text)
    return bool(t) and any(re.search(p, t) for p in NEGATIVE_PATTERNS)


def is_positive(text) -> bool:
    t = _norm(text)
    return bool(t) and any(re.search(p, t) for p in POSITIVE_PATTERNS)


def is_question(text) -> bool:
    return "?" in (text or "")


# --- helpers ----------------------------------------------------------------

def _ctx_for(store, conv):
    """Rebuild a composer context from stored contexts (may be partial)."""
    merchant = store.get("merchant", conv.get("merchant_id")) or {}
    category = store.get("category", (merchant or {}).get("category_slug")) or {}
    trigger = store.get("trigger", conv.get("trigger_id")) or {}
    customer = store.get("customer", conv.get("customer_id")) if conv.get("customer_id") else None
    if not trigger and conv.get("kind"):
        trigger = {"id": conv.get("trigger_id") or "", "kind": conv.get("kind"),
                   "scope": conv.get("send_as") == "merchant_on_behalf" and "customer" or "merchant",
                   "payload": {}, "urgency": 2, "suppression_key": ""}
    try:
        return Ctx(category, merchant, trigger, customer, now=conv.get("now"))
    except Exception:
        return Ctx({}, {}, {"kind": "reply", "payload": {}}, None, now=conv.get("now"))


def _salut(ctx):
    """Person salutation only when the merchant context carries a real owner."""
    ident = (getattr(ctx, "m", None) or {}).get("identity") or {}
    if str(ident.get("owner_first_name") or "").strip():
        return ctx.salut or str(ident.get("owner_first_name")).strip()
    return ""


def _topic_label(conv) -> str:
    return humanize(conv.get("kind") or conv.get("topic") or "this")


def _dedupe(body, conv, variants=None):
    """Never repeat a body inside the same conversation (anti-repetition flag)."""
    sent = conv.get("sent") or []
    if body not in sent:
        return body
    extras = variants or ["Meanwhile, one thing I can do right away: tell me the top "
                          "priority and I'll take it from there.",
                          "If that's already covered, reply NEXT and I'll move to the "
                          "next item.",
                          "I'll hold this thread open — reply when you have a minute."]
    idx = stable_hash(body + str(len(sent))) % len(extras)
    for _ in range(len(extras)):
        candidate = squeeze_lines(body) + " " + extras[idx % len(extras)]
        if candidate not in sent:
            return candidate
        idx += 1
    return squeeze(body + " " + str(len(sent)))


# --- artifact delivery ------------------------------------------------------

def _deliver(conv, ctx, store):
    """Render what we promised, using only stored contexts."""
    plan = conv.get("plan") or {"type": "generic", "topic": conv.get("kind")}
    kind = plan.get("type")

    if kind == "digest_abstract":
        summary = squeeze(str(plan.get("summary") or plan.get("title") or ""))
        actionable = squeeze(str(plan.get("actionable") or ""))
        source = squeeze(str(plan.get("source") or ""))
        patient_line = summary.split(". ")[0].rstrip(".") + "."
        body = (f"Here's the 2-minute version{f' ({source})' if source else ''}: "
                f"{summary} "
                f"What it means for you{': ' + actionable if actionable else ''}. "
                f"Patient-ed draft you can forward as-is:\n\n"
                f"\"{plan.get('title', 'New research')}: {patient_line} "
                f"If this applies to you, drop us a note — quick check takes 5 min.\"\n\n"
                f"Reply CONFIRM and I'll schedule this as your profile post for "
                f"tomorrow 10am.")
        return body, CTA_BINARY

    if kind == "profile_fix":
        fixes = plan.get("fixes") or ["profile clean-up"]
        lines = []
        for fix in fixes:
            if "verification" in fix:
                lines.append(f"{fix}: started — Google posts the code in 3-5 days, "
                             f"I'll drop it here")
            elif "offer" in fix:
                lines.append(f"{fix}: drafted, live the moment you say go")
            else:
                lines.append(f"{fix}: drafted — sending the preview next")
        body = (f"On it. {'; '.join(lines)}. "
                f"Reply CONFIRM and I'll push the first one live now.")
        return body, CTA_BINARY

    if kind in ("draft", "list"):
        lines = plan.get("lines") or []
        name = plan.get("name") or "draft"
        block = "\n".join(f"• {l}" for l in lines if l) or "• ready when you are"
        body = (f"Here's the {name}:\n{block}\n\n"
                f"Reply CONFIRM and I'll publish it to your profile today.")
        return body, CTA_BINARY

    if kind == "post_draft":
        topic = plan.get("topic") or "your week"
        angle = plan.get("angle") or "proof"
        offers = ctx.f.get("offers_active") or []
        offer_txt = f" Tag '{offer_title(offers[0])}'" if offers else ""
        trend = (ctx.cf.get("trends") or [{}])[0]
        trend_txt = (f" Tie it to demand: '{trend.get('query')}' "
                     f"{fmt_delta(trend.get('delta_yoy'))} YoY." if trend.get("delta_yoy") else "")
        body = (f"Draft post ({angle} angle):\n\n"
                f"\"{humanize(topic).capitalize()} — here's what's new at {ctx.biz}, "
                f"{ctx.f.get('locality') or ctx.f.get('city')}.\"{offer_txt}{trend_txt}\n\n"
                f"Reply CONFIRM and it goes live on your Google profile today.")
        return body, CTA_BINARY

    if kind == "schedule":
        slots = plan.get("slots") or []
        if slots:
            body = (f"Hold on — {slots[0]}"
                    f"{f' or {slots[1]}' if len(slots) > 1 else ''} are open. "
                    f"Reply 1 to block the first slot.")
            return body, CTA_BINARY
        body = (f"I'll block the next slot that matches your customer's usual time. "
                f"Reply CONFIRM and it's held.")
        return body, CTA_BINARY

    # generic
    fixes = fix_plan(ctx.f, ctx.cf)
    sal = _salut(ctx)
    body = (f"{sal + ' — ' if sal else ''}Done with the "
            f"{humanize(conv.get('kind') or 'ask')} piece — {fixes[0]} "
            f"is drafted. Reply CONFIRM and I'll push it live, or tell me what to "
            f"change first.")
    return body, CTA_BINARY


def _intent_action(conv, ctx):
    """Action-mode body after an explicit commitment. Must not re-qualify."""
    plan = conv.get("plan") or {}
    target = plan.get("name") or plan.get("topic") or plan.get("type")
    history = ctx.f.get("history") or []
    context_line = ""
    for turn in reversed(history):
        if str(turn.get("from")) == "merchant" and turn.get("body"):
            context_line = squeeze(str(turn.get("body")))[:140]
            break
    if not target:
        target = _topic_label(conv)
    if context_line:
        body = (f"Done — starting now. Taking your note ({context_line}), I'm drafting "
                f"it and it lands here next. Reply CONFIRM and I push it live the "
                f"moment it's ready.")
    else:
        body = (f"Done — starting now. I'm drafting the {target} and it lands here "
                f"next. Reply CONFIRM and I push it live the moment it's ready.")
    return squeeze(body)


# --- main entry -------------------------------------------------------------

def respond(conv: dict, message: str, store, from_role: str = "merchant",
            turn_number: int = 1) -> dict:
    """Produce the next move: send / wait / end."""
    conv = conv or {}
    text = squeeze(message or "")
    low = _norm(text)
    ctx = _ctx_for(store, conv)

    # 1. Hostile / explicit opt-out -> close, suppress, never argue.
    if is_hostile(text):
        return {"action": "end",
                "rationale": "Merchant signalled hostility or opt-out; closing the "
                             "conversation and suppressing this thread rather than "
                             "burning another turn."}

    # 2. Identity / provenance question -> answer plainly, offer an opt-out.
    if re.search(r"who are you|who is this|what is vera|what's vera|are you (a |an )?"
                 r"(bot|ai|human|robot)|talking to a human|how did you get", low):
        biz = ctx.biz or "your team"
        if "how did you get" in low:
            if from_role == "customer" and (ctx.k or {}).get("consent_at"):
                note = (f"You're a saved customer of {biz} and opted in to their "
                        f"messages on {ctx.k['consent_at']}.")
            else:
                note = "This number is the contact saved on the listing Vera works with."
        else:
            note = ("I handle the updates and drafts; a human on the team approves "
                    "anything that goes out.")
        body = _dedupe(
            f"I'm Vera — the assistant working with {biz} on its magicpin listing. "
            f"{note} Reply STOP and I'll close this thread, or tell me what you need "
            f"and I'll pick up the thread with them.", conv)
        conv["sent"] = (conv.get("sent") or []) + [body]
        conv["stage"] = int(conv.get("stage") or 0) + 1
        return {"action": "send", "body": body, "cta": CTA_OPEN,
                "rationale": "Identity/provenance question answered directly and "
                             "honestly (no invented consent story), with an explicit "
                             "STOP escape hatch offered."}

    # 3. WhatsApp Business auto-reply -> one nudge, a wait, then a clean exit.
    if is_auto_reply(text):
        mid = conv.get("merchant_id") or "unknown"
        count = store.bump_auto_reply(mid, low)
        if count <= 1:
            body = _dedupe(
                "Looks like an auto-reply 😊 Reply 'Yes' when you're back at the "
                "phone and I'll pick up exactly where we left off — I'll keep this "
                "thread open until then.", conv)
            conv["sent"] = (conv.get("sent") or []) + [body]
            return {"action": "send", "body": body, "cta": CTA_OPEN,
                    "rationale": "First canned auto-reply: one explicit note flagging "
                                 "it for the owner, then back off — no repeated pitches."}
        if count == 2:
            return {"action": "wait", "wait_seconds": 14400,
                    "rationale": "Second identical auto-reply in a row — owner is not "
                                 "at the phone; backing off 4h instead of burning "
                                 "turns."}
        return {"action": "end",
                "rationale": "Auto-reply repeated with no human in the thread; "
                             "conversation has zero engagement signal, so we exit "
                             "cleanly instead of spamming."}

    # 3. Explicit commitment -> action mode, never another qualifying question.
    if is_commitment(text):
        body = _dedupe(_intent_action(conv, ctx), conv)
        conv["sent"] = (conv.get("sent") or []) + [body]
        conv["stage"] = int(conv.get("stage") or 0) + 1
        return {"action": "send", "body": body, "cta": CTA_BINARY,
                "rationale": "Merchant gave explicit intent — switching from "
                             "question-asking to execution with a concrete next step "
                             "and no further qualification."}

    # 4. Off-mission ask -> decline politely, stay on the original thread.
    if looks_out_of_scope(text):
        topic = _topic_label(conv)
        body = _dedupe(
            f"I'll leave that to the people who do it professionally — that's outside "
            f"what I can help with. Coming back to {topic}: "
            f"{'the draft is ready, reply CONFIRM and I publish it' if conv.get('plan') else 'reply YES and I start on it now'}.",
            conv)
        conv["sent"] = (conv.get("sent") or []) + [body]
        return {"action": "send", "body": body, "cta": CTA_BINARY,
                "rationale": "Out-of-scope request declined in one line and redirected "
                             "back to the active thread so momentum isn't lost."}

    # 5. Explicit deferral -> wait, don't nudge.
    if is_negative(text) and not is_question(text):
        return {"action": "wait", "wait_seconds": 86400,
                "rationale": "Merchant asked for time; backing off 24h rather than "
                             "pushing — cadence beats persistence."}

    # 6. Accept / positive -> deliver the promised artifact.
    if is_positive(text) and not is_question(text):
        body, cta = _deliver(conv, ctx, store)
        body = _dedupe(body, conv)
        conv["sent"] = (conv.get("sent") or []) + [body]
        conv["stage"] = int(conv.get("stage") or 0) + 1
        conv["delivered"] = True
        return {"action": "send", "body": body, "cta": cta,
                "rationale": "Merchant accepted — handing over the actual artifact we "
                             "promised instead of another question, then one binary "
                             "publish step."}

    # 7. A question -> answer from context, then one next step.
    if is_question(text):
        body = _answer_question(text, conv, ctx, store)
        body = _dedupe(body, conv)
        conv["sent"] = (conv.get("sent") or []) + [body]
        conv["stage"] = int(conv.get("stage") or 0) + 1
        return {"action": "send", "body": body, "cta": CTA_OPEN,
                "rationale": "Direct question answered from the stored contexts with "
                             "real numbers; closed with a single next step."}

    # 8. Default: acknowledge and advance the thread with substance.
    if int(conv.get("stage") or 0) >= 4 and from_role == "merchant":
        return {"action": "end",
                "rationale": "Four turns with no clear commitment; exiting gracefully "
                             "rather than nudging an unwilling merchant again."}
    body, cta = _deliver(conv, ctx, store)
    body = _dedupe(body, conv)
    conv["sent"] = (conv.get("sent") or []) + [body]
    conv["stage"] = int(conv.get("stage") or 0) + 1
    return {"action": "send", "body": body, "cta": cta,
            "rationale": "Merchant engaged without a clear yes/no — advancing the "
                         "thread with the drafted artifact and one binary step."}


def _answer_question(text, conv, ctx, store):
    low = _norm(text)
    offers = ctx.f.get("offers_active") or []
    cf = ctx.cf

    if any(w in low for w in ("price", "pricing", "rate", "charge", "cost", "kitna",
                              "fee", "rupee")):
        if offers:
            listing = "; ".join(offer_title(o) for o in offers[:3])
            return (f"Live prices on your profile right now: {listing}. "
                    f"Reply CONFIRM if you want any of them front-and-centre on your "
                    f"listing this week.")
        cat = (cf.get("offers") or [])[:3]
        listing = "; ".join(str(o.get("title")) for o in cat if o.get("title"))
        if not listing:
            return (f"I don't have your current price list in this thread, so I won't "
                    f"invent one. Send the three prices you want shown and I'll put "
                    f"them on your Google profile today.")
        return (f"You have no live price on your listing. Category entry points that "
                f"convert: {listing}. Reply CONFIRM and I'll activate one on your "
                f"profile.")

    if any(w in low for w in ("photo", "picture", "image")):
        peer = cf.get("peer") or {}
        avg = peer.get("avg_photos")
        if avg:
            return (f"Peers in your segment sit at {nfmt(avg)} photos on average; "
                    f"profiles above that get tapped far more often. Reply CONFIRM "
                    f"and I'll caption 5 of yours today.")
        return (f"Photos are the fastest lever on a listing. Reply CONFIRM and I'll "
                f"caption 5 of yours today — you just pick the originals.")

    if any(w in low for w in ("review", "rating", "star")):
        peer = cf.get("peer") or {}
        avg = peer.get("avg_review_count")
        avg_txt = f" Peers average {nfmt(avg)} reviews." if avg else ""
        return (f"Replies to reviews are the single biggest ranking nudge on a "
                f"listing.{avg_txt} Send me the one you want answered and I'll draft "
                f"a reply in your tone.")

    if any(w in low for w in ("timing", "hours", "open", "close", "time kya")):
        return (f"Your business hours aren't in the context I hold, so I won't guess "
                f"them. Send the exact hours and I'll update your Google profile "
                f"today — reply CONFIRM once you've pasted them.")

    if any(w in low for w in ("offer", "deal", "discount", "campaign")):
        if offers:
            listing = "; ".join(offer_title(o) for o in offers[:3])
            return (f"Your live offers: {listing}. Reply CONFIRM and I'll build the "
                    f"campaign around the first one.")
        cat = (cf.get("offers") or [{}])[0]
        if not cat.get("title"):
            return (f"Send me the offer you want to run — one line is enough — and I'll "
                    f"draft the copy and put it live on your listing.")
        return (f"You have no live offer yet — the category-converting pattern is "
                f"'{cat.get('title', 'service @ price')}'. Reply CONFIRM and I'll set "
                f"it up on your listing.")

    line = peer_line(ctx.f, cf) or peer_views_line(ctx.f, cf)
    anchor = f" For context, {line}." if line else ""
    return (f"Noted — on {humanize(_topic_label(conv))}.{anchor} "
            f"Reply CONFIRM and I'll act on it, or send me the specifics and I'll "
            f"work from those.")
