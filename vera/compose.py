"""The Vera message engine.

    compose(category, merchant, trigger, customer?) -> {
        body, cta, send_as, suppression_key, rationale,
        template_name, template_params, plan
    }

Design rules
------------
1. Deterministic. Same four inputs (+ `now`) -> same output. No randomness, no
   system clock, no LLM calls.
2. Grounded. Every number, date, price, quote and citation comes from the four
   contexts (or arithmetic on them). Nothing is invented.
3. One anchor per message. The trigger picks the moment; merchant state picks
   the angle; category voice picks the words; a single CTA closes it.
"""

from __future__ import annotations

import os
import re

from . import ground as G
from .util import (as_list, days_between, fmt_date, fmt_delta, fmt_time,
                   humanize, inr, months_between, nfmt, now_dt, pick, pct,
                   sentence, squeeze, stable_hash)
from .voice import (CTA_BINARY, CTA_NONE, CTA_OPEN, CTA_SLOT, biz_name,
                    code_mix_tail, customer_lang, is_hindi, sanitize,
                    salutation)

# ---------------------------------------------------------------------------
# Context bundle
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, category, merchant, trigger, customer=None, now=None):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer
        self.now = now
        self.now_dt = now_dt(now)
        self.f = G.merchant_facts(self.m)
        self.cf = G.category_facts(self.cat)
        self.tf = G.trigger_facts(self.t)
        self.k = G.customer_facts(customer) if customer else None
        self.slug = self.f["category_slug"] or self.cf["slug"]
        self.salut = self.f["salut"] or "there"
        self.biz = self.f["name"] or biz_name(self.m)
        self.customer_facing = self.tf["scope"] == "customer" or customer is not None

    @property
    def key(self):
        return f"{self.tf['kind']}:{self.f['id']}"

    def tail(self, extra=""):
        return code_mix_tail(self.m, self.key + extra, self.cat)

    def msg(self, body, cta, rationale, plan=None, template=None, params=None):
        text = sanitize(body, self.cat)
        chosen_plan = plan or {"type": "generic", "topic": self.tf["kind"]}
        return {
            "body": text,
            "cta": cta,
            "rationale": rationale,
            "plan": chosen_plan,
            "template_name": template or f"vera_{_slug(self.tf['kind'])}_v1",
            "template_params": params if params is not None else _params(self, text, chosen_plan),
        }


def _slug(kind):
    return re.sub(r"[^a-z0-9]+", "_", str(kind or "message").lower()).strip("_") or "message"


def _params(ctx, body, plan=None, limit=3):
    """Template parameters for the pre-approved first-touch WhatsApp template:
    the person, the subject of the message, then the concrete anchors it leans on."""
    plan = plan or {}
    tokens = []
    name = ctx.salut
    if name and name != "there":
        tokens.append(name)
    subject = plan.get("topic") or plan.get("name") or ""
    if not subject and plan.get("fixes"):
        subject = str(plan["fixes"][0])
    if subject:
        tokens.append(squeeze(str(subject))[:48])
    quoted = re.findall(r"'([^']{3,48})'", body)
    anchors = re.findall(r"₹[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?%|\b\d+\s(?:days|reviews|km|"
                         r"credits|profile views)\b|\b\d{1,2} [A-Z][a-z]{2}\b", body)
    for cand in quoted + anchors:
        if len(tokens) >= limit:
            break
        if cand and cand not in tokens:
            tokens.append(cand)
    for part in re.split(r"(?<=[.!?])\s+", body):
        if len(tokens) >= limit:
            break
        part = squeeze(part)
        if part and part not in tokens:
            tokens.append(part[:60])
    return tokens


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

_MONTH_ABBR = {"jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"}

_COUNT_LABEL = {
    "review_count": "reviews", "reviews": "reviews", "rating_count": "ratings",
    "followers": "followers", "members": "members", "signups": "signups",
}

_ACTION_WORDS = {
    "profile": "audit your profile and fix the top 2 gaps",
    "offers": "draft an offer from your category catalogue",
    "post": "draft this week's profile post",
    "reviews": "draft review replies",
    "campaign": "draft the campaign",
    "message": "draft the message",
}


def fix_plan(facts, cfacts) -> list:
    """Concrete, data-backed fixes derived from merchant signals (max 2)."""
    fixes = []
    if G.has_signal(facts, "unverified_gbp") or not facts["verified"]:
        fixes.append("start Google profile verification")
    if not facts["offers_active"]:
        offer, _ = G.offer_for_new_users(facts, cfacts)
        if offer:
            fixes.append(f"put a live offer on the listing ({G.offer_title(offer)})")
    if G.has_signal(facts, "stale_posts") or G.has_signal(facts, "no_recent_post"):
        fixes.append("publish a fresh profile post")
    peer = cfacts.get("peer") or {}
    ctr, avg = facts.get("ctr"), peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(avg, (int, float)) and ctr < avg * 0.9:
        fixes.append("tighten your headline photo so more searchers tap through")
    if G.has_signal(facts, "delivery_not_set_up"):
        fixes.append("turn on home delivery on your listing")
    if not fixes:
        fixes.append("publish a fresh profile post this week")
    return fixes[:2]


def anchor_line(ctx) -> str:
    """Best merchant-grounded observation available (used by fallbacks)."""
    note = G.signal_note(ctx.f, 1)
    if note:
        return sentence(f"I noticed {note}")
    line = G.peer_line(ctx.f, ctx.cf)
    if line:
        return sentence(f"For context, {line}")
    line = G.peer_views_line(ctx.f, ctx.cf)
    if line:
        return sentence(f"For context, {line}")
    trend = G.best_trend(ctx.cf)
    if trend:
        return sentence(f"Worth knowing: '{trend.get('query')}' searches are "
                        f"{fmt_delta(trend.get('delta_yoy'))} YoY in your category")
    return ""


def strength_line(ctx) -> str:
    f, cf = ctx.f, ctx.cf
    delta = (f.get("delta") or {})
    mv = delta.get("views_pct")
    if isinstance(mv, (int, float)) and mv >= 0.10:
        return sentence(f"Your views are {fmt_delta(mv)} over the last 7 days")
    return ""


# ---------------------------------------------------------------------------
# Merchant-scope handlers
# ---------------------------------------------------------------------------


def h_research_digest(ctx):
    item = G.resolve_digest(ctx.cf, ctx.tf,
                            kind_pref=["research", "trend", "tech", "seasonal",
                                       "compliance", "cde", "supply", "alert", "compete"])
    if not item:
        return _fallback_merchant(ctx, "this week's category digest")
    title = squeeze(str(item.get("title") or "a new category note"))
    source = squeeze(str(item.get("source") or ""))
    trial = item.get("trial_n")
    segment = str(item.get("patient_segment") or "")

    anchor = ""
    if segment and ctx.f["agg"].get("high_risk_adult_count"):
        count = ctx.f["agg"]["high_risk_adult_count"]
        anchor = f"your {nfmt(count)} high-risk adults are exactly the cohort it covers"
    elif segment and ctx.f["agg"].get("total_unique_ytd"):
        anchor = f"relevant to the {nfmt(ctx.f['agg']['total_unique_ytd'])} customers on your roster"
    else:
        anchor = G.peer_line(ctx.f, ctx.cf) or f"your {ctx.f['locality'] or ctx.f['city']} practice"

    lead = f"{source} — " if source else ""
    trial_txt = f" ({nfmt(trial)}-patient trial)" if isinstance(trial, (int, float)) else ""
    body = (f"{ctx.salut}, {lead}\"{title}\"{trial_txt}. {anchor[0].upper() + anchor[1:]}. "
            f"Want me to pull the 2-minute version and draft a patient-ed WhatsApp you can share?"
            f"{ctx.tail()}")
    plan = {"type": "digest_abstract", "title": title, "source": source,
            "summary": item.get("summary") or title,
            "actionable": item.get("actionable") or "",
            "content_id": item.get("id")}
    rationale = (f"Research digest trigger; anchored on the item's own citation "
                 f"({source or 'no source'}) tied to this merchant's roster "
                 f"({anchor}). Open-ended CTA offers to do the work (reciprocity + "
                 f"low friction).")
    return ctx.msg(body, CTA_OPEN, rationale, plan=plan)


def h_cde(ctx):
    item = G.resolve_digest(ctx.cf, ctx.tf, kind_pref=["cde"])
    payload = ctx.tf["payload"]
    title = squeeze(str((item or {}).get("title") or payload.get("title") or "a CDE session"))
    date = payload.get("date") or (item or {}).get("date")
    credits = payload.get("credits", (item or {}).get("credits"))
    fee = payload.get("fee") or (item or {}).get("actionable") or ""
    when = fmt_date(date, weekday=False)
    time = fmt_time(date)
    bits = []
    if time:
        bits.append(f"{when}, {time}" if when else time)
    elif when:
        bits.append(when)
    if isinstance(credits, (int, float)):
        bits.append(f"{credits} credits")
    if fee:
        bits.append(squeeze(str(fee)).replace("_", " ").lower())
    detail = " — " + ", ".join(bits) if bits else ""
    summary = squeeze(str((item or {}).get("summary") or ""))
    tail = f" {summary}" if summary and len(summary) < 220 else ""
    body = (f"{ctx.salut}, CDE worth blocking{detail}: \"{title}\".{tail} "
            f"Reply YES and I'll put it on your calendar.{ctx.tail()}")
    rationale = ("External CDE trigger; cites the source calendar's date/credits/fee "
                 "verbatim and converts to a single binary commitment (calendar block).")
    plan = {"type": "draft", "name": "calendar block", "lines": [title, " ".join(bits)]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_regulation(ctx):
    item = G.resolve_digest(ctx.cf, ctx.tf, kind_pref=["compliance", "regulation"])
    payload = ctx.tf["payload"]
    title = squeeze(str((item or {}).get("title") or payload.get("title") or ctx.tf["kind"]))
    source = squeeze(str((item or {}).get("source") or ""))
    deadline = payload.get("deadline_iso") or (item or {}).get("date") or payload.get("effective_from")
    summary = squeeze(str((item or {}).get("summary") or ""))
    actionable = squeeze(str((item or {}).get("actionable") or ""))
    when = ""
    if deadline:
        label = fmt_date(deadline)
        hay = f"{title} {summary}".lower()
        # the digest title often already carries the effective date — don't repeat it
        if label.lower() not in hay and str(deadline)[:10] not in hay \
                and "effective" not in hay:
            when = f"effective {label}"
    src = f" ({source})" if source else ""
    body = (f"{ctx.salut}, compliance heads-up{src}: {title}"
            f"{', ' + when if when else ''}. {summary} "
            f"Reply YES and I'll draft the audit checklist for your "
            f"{ctx.f['locality'] or ctx.f['city']} clinic.{ctx.tail()}")
    rationale = ("Regulation-change trigger with a hard deadline pulled from the "
                 "payload; summary cited from the category digest; single binary CTA "
                 "converts awareness into an owned checklist.")
    plan = {"type": "list", "name": "compliance checklist",
            "lines": [x for x in [title, actionable] if x]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_perf_dip(ctx):
    payload = ctx.tf["payload"]
    metric = G.payload_text(payload, "metric")
    if not metric:
        metric, _derived = G.delta_line(ctx.f)
        metric = metric or "views"
    metric = humanize(metric).replace(" pct", "")
    delta = G.payload_number(payload, "delta_pct", "delta", "pct")
    if delta is None:
        delta = (ctx.f.get("delta") or {}).get(f"{metric.split()[0]}_pct")
    window = G.payload_text(payload, "window") or "7d"
    baseline = G.payload_number(payload, "vs_baseline")
    window_txt = "last 7 days" if window in ("7d", "7") else f"last {humanize(window)}"

    if delta is None:
        return _fallback_merchant(ctx, metric)
    delta_txt = fmt_delta(delta)
    base_txt = f" (your baseline is {nfmt(baseline)})" if baseline else ""
    fixes = fix_plan(ctx.f, ctx.cf)
    fix_txt = f" and {fixes[-1]}" if len(fixes) > 1 else ""
    exclude = {"perf_dip_severe"}
    fix_blob = " ".join(fixes)
    if "offer" in fix_blob:
        exclude.add("no_active_offers")
    if "verification" in fix_blob:
        exclude.add("unverified_gbp")
    if "post" in fix_blob:
        exclude.update({"stale_posts", "no_recent_post"})
    note = G.signal_note(ctx.f, 1, exclude=exclude)
    if note:
        anchor = sentence(f"I noticed {note}")
    else:
        peer = G.peer_views_line(ctx.f, ctx.cf) or G.peer_line(ctx.f, ctx.cf)
        anchor = sentence(f"For context, {peer}") if peer else ""
    body = (f"{ctx.salut}, {metric} are {delta_txt} over the {window_txt}{base_txt}. "
            f"{anchor} Fix: {fixes[0]}{fix_txt}. "
            f"Reply YES and I'll start on it now.{ctx.tail()}")
    rationale = (f"Internal perf dip: leads with the metric + delta from the trigger "
                 f"payload, pairs it with a merchant signal ({fixes[0]}), and closes "
                 f"on one binary action.")
    plan = {"type": "profile_fix", "fixes": fixes}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_perf_spike(ctx):
    payload = ctx.tf["payload"]
    metric = G.payload_text(payload, "metric")
    if not metric:
        metric, _ = G.delta_line(ctx.f)
        metric = metric or "views"
    metric = humanize(metric).replace(" pct", "")
    delta = G.payload_number(payload, "delta_pct", "delta", "pct")
    if delta is None:
        delta = (ctx.f.get("delta") or {}).get(f"{metric.split()[0]}_pct")
    window = G.payload_text(payload, "window") or "7d"
    driver = G.payload_text(payload, "likely_driver")
    window_txt = "last 7 days" if window in ("7d", "7") else f"last {humanize(window)}"

    if delta is None:
        return _fallback_merchant(ctx, metric)
    delta_txt = fmt_delta(delta)
    if driver:
        body = (f"{ctx.salut}, {metric} are {delta_txt} over the {window_txt} — "
                f"likely driver: {humanize(driver)}. Reply YES and I'll pin it as a "
                f"profile post so it repeats.{ctx.tail()}")
        cta = CTA_BINARY
        rationale = ("Perf spike with a named likely driver; converts the win into a "
                     "repeatable asset instead of a congratulation.")
        plan = {"type": "post_draft", "topic": humanize(driver),
                "angle": "repeat what worked"}
    else:
        body = (f"{ctx.salut}, good week: {metric} {delta_txt} over the {window_txt}. "
                f"What moved it — a new offer, a referral, or search? Tell me and "
                f"I'll pin it as a profile post so it sticks.{ctx.tail()}")
        cta = CTA_OPEN
        rationale = ("Perf spike without a named driver; asks the merchant (highest-"
                     "signal lever) rather than guessing, with a concrete payoff for "
                     "answering.")
        plan = {"type": "post_draft", "topic": f"{metric} up", "angle": "asked the merchant"}
    return ctx.msg(body, cta, rationale, plan=plan)


def h_seasonal_dip(ctx):
    payload = ctx.tf["payload"]
    delta = G.payload_number(payload, "delta_pct", "delta")
    metric = G.payload_text(payload, "metric") or "views"
    note = G.payload_text(payload, "season_note")
    window = G.payload_text(payload, "window") or "7d"
    delta = delta if delta is not None else (ctx.f.get("delta") or {}).get("views_pct")
    if delta is None:
        return _fallback_merchant(ctx, "seasonal dip")
    members = ctx.f["agg"].get("total_active_members") or ctx.f["agg"].get("total_unique_ytd")
    member_txt = f" your {nfmt(members)} people on file" if members else " your regulars"
    season = _season_label(note) or "the seasonal acquisition lull"
    window_txt = "last 7 days" if window in ("7d", "7") else humanize(window)
    body = (f"{ctx.salut}, {humanize(metric)} are {fmt_delta(delta)} over the "
            f"{window_txt} — but this is {season}, not a problem. Skip acquisition spend "
            f"now and keep{member_txt} engaged instead. Reply YES and I'll draft a "
            f"retention push for them.{ctx.tail()}")
    rationale = ("Seasonal dip reframed as expected (season_note from payload) — "
                 "reduces merchant anxiety, converts into a retention action anchored "
                 "on their own customer count.")
    plan = {"type": "draft", "name": "retention push",
            "lines": [f"hold the line through {season}"]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def _season_label(note) -> str:
    """'post_resolution_window_apr_jun' -> 'the Apr-Jun post-resolution window'."""
    if not note:
        return ""
    tokens = str(note).replace("_", " ").split()
    months, rest = [], []
    for tok in tokens:
        (months if tok.lower()[:3] in _MONTH_ABBR else rest).append(tok)
    desc = " ".join(rest).lower().replace("post resolution", "post-resolution") \
        or "seasonal window"
    if not months:
        return f"the {desc}"
    labels = [m.capitalize() for m in months]
    span = labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"
    return f"the {span} {desc}"


def h_milestone(ctx):
    payload = ctx.tf["payload"]
    metric = G.payload_text(payload, "metric")
    value = G.payload_number(payload, "value_now", "value")
    target = G.payload_number(payload, "milestone_value", "target")
    if metric and isinstance(value, (int, float)) and isinstance(target, (int, float)):
        gap = max(int(target - value), 0)
        gap_txt = f" — {nfmt(gap)} short of {nfmt(target)}" if gap else f" — that's {nfmt(target)}"
        label = _COUNT_LABEL.get(metric) or humanize(metric).replace(" count", "s")
        body = (f"{ctx.salut}, you're at {nfmt(value)} {label}{gap_txt}. "
                f"Reply YES and I'll draft the milestone post + a one-tap nudge to your "
                f"recent regulars.{ctx.tail()}")
        plan = {"type": "draft", "name": f"{humanize(metric)} milestone",
                "lines": [f"{nfmt(value)} {label}", f"next: {nfmt(target)}"]}
        rationale = ("Milestone trigger with exact value/target from payload; gap is "
                     "computed, and the CTA converts the milestone into social proof.")
        return ctx.msg(body, CTA_BINARY, rationale, plan=plan)
    line = G.peer_views_line(ctx.f, ctx.cf) or G.peer_line(ctx.f, ctx.cf)
    if not line:
        return _fallback_merchant(ctx, "milestone")
    if "below" in line:
        body = (f"{ctx.salut}, gap worth closing: {line}. "
                f"Reply YES and I'll send the 3-line fix list — 2 minutes to read, "
                f"nothing to set up.{ctx.tail()}")
        plan = {"type": "profile_fix", "fixes": fix_plan(ctx.f, ctx.cf)}
        rationale = ("Milestone trigger with no payload value; falls back to a verifiable "
                     "peer-benchmark comparison and pairs the below-peer number with the "
                     "matching fix, not a generic post.")
    else:
        body = (f"{ctx.salut}, worth flagging: {line}. "
                f"Reply YES and I'll turn it into this week's profile post.{ctx.tail()}")
        plan = {"type": "post_draft", "topic": "peer benchmark", "angle": "proof"}
        rationale = ("Milestone trigger with no payload value; falls back to a verifiable "
                     "peer-benchmark comparison from the merchant + category contexts.")
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_dormant(ctx):
    payload = ctx.tf["payload"]
    days = G.payload_number(payload, "days_since_last_merchant_message")
    if days is None:
        for sig in ctx.f["signals"]:
            if sig["name"].startswith("dormant_with_vera") and sig["value"].endswith("d"):
                try:
                    days = int(sig["value"][:-1])
                except ValueError:
                    days = None
    topic = G.payload_text(payload, "last_topic")
    if days is None and ctx.f["history"]:
        days = days_between(ctx.f["history"][-1].get("ts"), ctx.now_dt)
    days_txt = f"{int(days)} days" if days is not None else "a while"
    topic_txt = f" (you were asking about {humanize(topic)})" if topic else ""
    perf = strength_line(ctx) or anchor_line(ctx)
    cta = ("Reply YES and I'll send the 3-line fix list for your listing — "
           "2 minutes to read, nothing to set up." + ctx.tail())
    body = f"{ctx.salut}, it's been {days_txt} since we last spoke{topic_txt}. {perf} {cta}"
    rationale = ("Dormancy trigger: re-engages with a reciprocity offer (a prepared "
                 "fix list) anchored on current numbers instead of a 'are you there?' "
                 "nudge.")
    plan = {"type": "list", "name": "3-line fix list",
            "lines": fix_plan(ctx.f, ctx.cf)}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_renewal(ctx):
    payload = ctx.tf["payload"]
    days = G.payload_number(payload, "days_remaining")
    plan_name = G.payload_text(payload, "plan") or ctx.f["plan"] or "Pro"
    amount = G.payload_number(payload, "renewal_amount") or ctx.f.get("renewal_amount")
    if days is None:
        days = ctx.f.get("days_remaining")
    if days is None:
        return _fallback_merchant(ctx, "renewal")
    amount_txt = f" ({inr(amount)} to renew)" if amount else ""
    views = ctx.f.get("views")
    calls = ctx.f.get("calls")
    value = ""
    if isinstance(views, (int, float)) and isinstance(calls, (int, float)):
        value = (f" Last 30 days it carried {nfmt(views)} profile views and "
                 f"{nfmt(calls)} calls to your listing.")
    body = (f"{ctx.salut}, your {plan_name} plan runs out in {int(days)} days"
            f"{amount_txt}.{value} Reply YES and I'll renew it now so nothing goes "
            f"dark.{ctx.tail()}")
    rationale = (f"Renewal trigger: {int(days)}-day deadline + plan value recap from "
                 f"performance data; loss-aversion lever with a single binary action.")
    plan = {"type": "draft", "name": f"{plan_name} renewal",
            "lines": [f"{int(days)} days left", inr(amount) if amount else plan_name]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_winback(ctx):
    payload = ctx.tf["payload"]
    days = G.payload_number(payload, "days_since_expiry")
    if days is None:
        days = ctx.f.get("days_since_expiry")
    dip = G.payload_number(payload, "perf_dip_pct", "delta_pct")
    if dip is None:
        dip = (ctx.f.get("delta") or {}).get("views_pct")
    lapsed = G.payload_number(payload, "lapsed_customers_added_since_expiry")
    if days is None and dip is None:
        return _fallback_merchant(ctx, "win-back")
    days_txt = f"it's been {int(days)} days since your plan expired" if days is not None \
        else "your plan has expired"
    dip_txt = f" and views are {fmt_delta(dip)}" if dip is not None else ""
    lapsed_txt = f" {int(lapsed)} customers lapsed in that window." \
        if lapsed else f" {anchor_line(ctx)}"
    offer, _owned = G.offer_for_new_users(ctx.f, ctx.cf)
    offer_txt = f" We'll relaunch with '{G.offer_title(offer)}'." if offer else ""
    body = (f"{ctx.salut}, {days_txt}{dip_txt}. {lapsed_txt} Reply YES and I'll "
            f"reactivate your listing and draft the win-back note.{offer_txt}{ctx.tail()}")
    rationale = ("Win-back trigger: expiry age + performance drop from payload, "
                 "quantified roster loss, one binary reactivation action.")
    plan = {"type": "profile_fix", "fixes": ["reactivate the listing",
                                             "draft the win-back note"]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_festival(ctx):
    payload = ctx.tf["payload"]
    festival = G.payload_text(payload, "festival") or G.payload_text(payload, "occasion")
    date = payload.get("date")
    days_until = G.payload_number(payload, "days_until")
    beat = G.beat_for(ctx.cf, date or ctx.now_dt)
    beat_note = (beat or {}).get("note")
    beat_txt = ""
    if festival:
        when = fmt_date(date) if date else ""
        days_txt = f"{int(days_until)} days away" if days_until is not None else ""
        head = f"{festival}" + (f" is {days_txt}" if days_txt else "") + \
               (f" ({when})" if when else "")
        if beat_note:
            beat_txt = f" Category timing: {beat_note}."
    else:
        season = humanize(G.payload_text(payload, "season") or "")
        if beat_note:
            window = (beat or {}).get("month_range") or season or "the season"
            head = f"the {window} calendar is here — {beat_note}"
        else:
            head = f"{season or 'the season'} calendar is here"
    if festival or "retention" not in (beat_note or ""):
        offer, owned = G.offer_for_new_users(ctx.f, ctx.cf)
    else:
        offer, owned = G.best_active_offer(ctx.f), True
    if offer:
        offer_txt = (f" Push '{G.offer_title(offer)}' as the hook" if owned
                     else f" The category-converting hook is '{G.offer_title(offer)}'")
    else:
        offer_txt = ""
    if festival:
        ask = "Reply YES and I'll draft the offer copy + the profile post."
    elif "retention" in (beat_note or ""):
        ask = "Reply YES and I'll draft the win-back push for your regulars."
    else:
        ask = "Reply YES and I'll draft the seasonal post for your listing."
    sentences = [f"{head}."]
    if beat_txt:
        sentences.append(beat_txt.strip())
    if offer_txt:
        sentences.append(offer_txt.strip() + ".")
    body = f"{ctx.salut}, {' '.join(sentences)} {ask}{ctx.tail()}"
    rationale = ("Festival trigger anchored on date/days-out from payload and the "
                 "category's own seasonal beat; offer line prefers the merchant's live "
                 "offer over the catalogue.")
    plan = {"type": "post_draft", "topic": festival or head,
            "angle": "seasonal offer"}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_competitor(ctx):
    payload = ctx.tf["payload"]
    name = G.payload_text(payload, "competitor_name") or G.payload_text(payload, "name")
    if not name:
        return _fallback_merchant(ctx, "a new competitor")
    distance = G.payload_number(payload, "distance_km")
    their_offer = G.payload_text(payload, "their_offer", "offer")
    opened = payload.get("opened_date") or payload.get("opened_at")
    dist_txt = f"{distance} km away" if distance is not None else "nearby"
    when_txt = f" on {fmt_date(opened)}" if opened else ""
    their_txt = f" listing '{their_offer}'" if their_offer else ""
    mine, owned = G.offer_for_new_users(ctx.f, ctx.cf)
    mine_txt = ""
    if mine:
        mine_txt = (f" Your live comparable: '{G.offer_title(mine)}'."
                    if owned else f" The category counter is '{G.offer_title(mine)}'.")
    body = (f"{ctx.salut}, heads-up: {name} opened {dist_txt}{when_txt}{their_txt}."
            f"{mine_txt} Reply YES and I'll pull a side-by-side of both listings.{ctx.tail()}")
    rationale = ("Competitor-opened trigger: uses only payload facts (name, distance, "
                 "their offer, date) plus this merchant's own offer; curiosity + loss "
                 "aversion with a single binary ask.")
    plan = {"type": "list", "name": "listing side-by-side",
            "lines": [f"them: {name}{their_txt}", f"you: {mine_txt.strip() or ctx.biz}"]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_review_theme(ctx):
    payload = ctx.tf["payload"]
    theme = G.payload_text(payload, "theme")
    occurrences = G.payload_number(payload, "occurrences_30d", "occurrences")
    quote = G.payload_text(payload, "common_quote")
    if not theme:
        neg = [t for t in ctx.f["themes"] if str(t.get("sentiment")) == "neg"]
        if neg:
            neg.sort(key=lambda t: -(t.get("occurrences_30d") or 0))
            top = neg[0]
            theme, occurrences = top.get("theme"), top.get("occurrences_30d")
            quote = top.get("common_quote")
    if not theme:
        return _fallback_merchant(ctx, "review themes")
    label = {"wait_time": "waiting time", "delivery_late": "delivery time",
             "saturday_wait": "Saturday waiting times", "doctor_manner": "how the doctor explains",
             "stylist_skill": "stylist skill", "pizza_quality": "pizza quality",
             "thali_quality": "the thali", "weekend_busy": "weekend rush",
             "equipment_quality": "the equipment", "morning_crowd": "morning crowds",
             "instructor_quality": "the instructors", "small_classes": "small classes",
             "delivery_speed": "delivery speed", "medicine_availability": "medicine availability",
             }.get(theme, humanize(theme))
    count_txt = f"{int(occurrences)} reviews in the last 30 days" if occurrences else "Reviews"
    quote_txt = f" Top line: \"{squeeze(quote)}\"." if quote else ""
    neg = any(t.get("theme") == theme and t.get("sentiment") == "neg" for t in ctx.f["themes"]) \
        or str(payload.get("trend", "")) == "rising" or theme in ("wait_time", "delivery_late",
                                                                  "saturday_wait", "weekend_busy",
                                                                  "morning_crowd")
    action = ("Reply YES and I'll draft replies to the recent ones plus a one-line "
              "fix note you can pin on your profile."
              if neg else
              "Reply YES and I'll turn it into a profile post so new customers see it.")
    body = f"{ctx.salut}, {count_txt} mention {label}.{quote_txt} {action}{ctx.tail()}"
    rationale = ("Review-theme trigger: quotes the merchant's own review text with the "
                 "occurrence count, then offers a drafted response (effort "
                 "externalisation) as a single binary action.")
    plan = {"type": "draft", "name": f"review replies: {label}",
            "lines": [quote or label]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_curious_ask(ctx):
    payload = ctx.tf["payload"]
    topic = G.payload_text(payload, "ask_topic")
    trend = G.best_trend(ctx.cf)
    hint = ""
    if trend:
        hint = (f"One data point: '{trend.get('query')}' searches are "
                f"{fmt_delta(trend.get('delta_yoy'))} YoY in your category"
                f"{' for the ' + str(trend.get('segment_age')) + ' band' if trend.get('segment_age') else ''}")
    if topic:
        q = f"what's been pulling the most '{humanize(topic)}' at {ctx.biz} this week?"
    else:
        svc = {"dentists": "treatment", "salons": "service", "restaurants": "dish",
               "gyms": "class", "pharmacies": "medicine"}.get(ctx.slug, "service")
        q = f"what's been the most asked-for {svc} at {ctx.biz} this week?"
    hint_txt = f" {hint} — is that showing up for you too?" if hint else ""
    cta = (" Tell me and I'll turn it into a profile post plus a 4-line WhatsApp "
           "reply you can reuse. Takes 5 min." + ctx.tail())
    body = f"{ctx.salut}, quick one — {q}{hint_txt}{cta}"
    rationale = ("Curious-ask cadence: asks the merchant (under-used engagement lever) "
                 "with a category trend as a concrete guess, and pre-loads the payoff "
                 "so the reply costs them one line.")
    plan = {"type": "post_draft", "topic": topic or q, "angle": "asked the merchant"}
    return ctx.msg(body, CTA_OPEN, rationale, plan=plan)


def _price_of(offer):
    """Price embedded in an offer title/catalogue row (₹, @ or 'value' field)."""
    if not offer:
        return None
    value = offer.get("value")
    if isinstance(value, (int, float)) and value:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
        return int(value)
    title = str(offer.get("title") or "")
    for pattern in (r"[₹]\s*([\d][\d,]*)", r"@\s*([\d][\d,]*)",
                    r"(\d[\d,]*)\s*(?:rs\.?|inr)", r"(\d[\d,]+)\b.*\b(?:per|/)\s*(?:month|visit)"):
        m = re.search(pattern, title, flags=re.I)
        if m:
            digits = m.group(1).replace(",", "")
            if digits.isdigit() and int(digits) > 0:
                return int(digits)
    return None


def h_planning(ctx):
    payload = ctx.tf["payload"]
    topic = G.payload_text(payload, "intent_topic", "topic") or humanize(ctx.tf["kind"])
    last_msg = G.payload_text(payload, "merchant_last_message")
    label = humanize(topic)
    slug = ctx.slug

    # Ground the draft in this topic's own words first, then category defaults.
    words = [w for w in str(topic).lower().replace("-", "_").split("_") if len(w) > 3]
    keywords = words + ["thali", "lunch", "meal", "combo", "family", "program",
                        "camp", "course", "pack"]
    base = _price_of(G.best_active_offer(ctx.f, keywords[:5]))
    if base is None:
        base = _price_of(G.catalog_offer(ctx.cf, keywords[:5]))
    if base is None:
        base = _price_of(G.best_active_offer(ctx.f)) or \
            _price_of(G.catalog_offer(ctx.cf))

    lines = []
    if base:
        t1, t2, t3 = round(base * 0.85), round(base * 0.78), round(base * 0.72)
        tiers = (f"10 x {inr(t1)} · 25 x {inr(t2)} · 50+ x {inr(t3)} "
                 f"(draft — tune to your margins)")
        terms = {
            "restaurants": f"base price {inr(base)}; delivery window agreed a day ahead",
            "gyms": f"base price {inr(base)}; batch size and timings locked 24h ahead",
            "salons": f"base price {inr(base)}; slots blocked a day ahead",
            "pharmacies": f"base price {inr(base)}; pack-and-dispatch a day ahead",
            "dentists": f"base price {inr(base)}; chair time blocked a day ahead",
        }.get(slug, f"base price {inr(base)}; confirmed a day ahead")
        lines = [tiers, terms]
    elif ctx.f["offers_active"]:
        lines = [f"built around your live offer '{G.offer_title(ctx.f['offers_active'][0])}'"]
    else:
        hook, _ = G.offer_for_new_users(ctx.f, ctx.cf)
        if hook:
            lines = [f"entry hook '{G.offer_title(hook)}' from the category catalogue"]
    if not lines:
        lines = ["send me your target price and headcount and I'll draft the tiers "
                 "against those"]

    place = ctx.f["locality"] or ctx.f["city"] or "your area"
    if any(w in str(topic) for w in ("kids", "school", "children", "parent")):
        audience = f"parents in {place}"
    else:
        audience = {
            "restaurants": f"{place} offices",
            "gyms": f"{place} members",
            "salons": f"{place} customers",
            "dentists": f"{place} patients",
            "pharmacies": f"{place} regulars",
        }.get(slug, f"{place} customers")

    lead = (f"you asked what it would look like — here's the {label} draft"
            if last_msg else f"here's the {label} draft")
    body = (f"{ctx.salut}, {lead}:\n" +
            "\n".join(f"• {ln}" for ln in lines) +
            f"\nReply YES and I'll write the outreach note for {audience} and send "
            f"it here.{ctx.tail()}")
    rationale = ("Explicit merchant intent (payload carries their own words) — jumps "
                 "straight to a drafted artifact instead of re-qualifying; tiers are "
                 "derived arithmetically from this topic's own catalogue price and "
                 "explicitly marked as a draft.")
    plan = {"type": "draft", "name": label, "lines": lines}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan,
                   params=[ctx.salut, label] + lines[:2])


def h_unverified(ctx):
    payload = ctx.tf["payload"]
    path = G.payload_text(payload, "verification_path") or "postcard or phone call"
    path = path.replace("_", " ").replace(" or ", " or ")
    uplift = G.payload_number(payload, "estimated_uplift_pct", "estimated_uplift")
    uplift_txt = f" (est. +{pct(uplift)} profile views)" if uplift is not None else ""
    body = (f"{ctx.salut}, your Google profile is still unverified — every edit "
            f"(hours, photos, offers) sits in Google's review queue until it clears. "
            f"Verification is by {path}{uplift_txt}. Reply YES and I'll start it now; "
            f"you'll only need to enter the code.{ctx.tail()}")
    rationale = ("Unverified-profile trigger: explains the concrete consequence first "
                 "(edits queue), cites the payload's verification path and estimated "
                 "uplift, single binary start action.")
    plan = {"type": "profile_fix", "fixes": ["start profile verification"]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_supply_alert(ctx):
    item = G.resolve_digest(ctx.cf, ctx.tf, kind_pref=["alert", "supply"])
    payload = ctx.tf["payload"]
    molecule = G.payload_text(payload, "molecule") or G.payload_text(payload, "drug")
    batches = [str(b) for b in as_list(payload.get("affected_batches"))]
    maker = G.payload_text(payload, "manufacturer", "mfr") or \
        G.payload_text(payload, "manufacturer_name")
    if not molecule:
        return _fallback_merchant(ctx, "supply alert")
    title = squeeze(str((item or {}).get("title") or f"recall on {molecule}"))
    source = squeeze(str((item or {}).get("source") or ""))
    inside = ", ".join(batches) if batches else ""
    if source:
        inside = f"{inside}; {source}" if inside else source
    batch_txt = f" ({inside})" if inside else ""
    maker_txt = ""
    if maker and maker.lower() not in title.lower() and "manufacturer" not in title.lower():
        maker_txt = f" by {maker}"
    chronic = ctx.f["agg"].get("chronic_rx_count")
    roster = f" your {nfmt(chronic)} chronic-Rx customers on file" if chronic else \
        " your repeat-prescription customers"
    body = (f"{ctx.salut}, urgent — {title}{batch_txt}{maker_txt}. I can filter{roster} "
            f"for {molecule} regulars. Reply YES and I'll send the filtered list plus a "
            f"drafted WhatsApp note for them.{ctx.tail()}")
    rationale = ("Supply alert: batch numbers + molecule from the payload, source cited "
                 "from the category digest, roster size from the merchant's own "
                 "aggregate; urgency with a complete-artifact offer.")
    plan = {"type": "list", "name": f"{molecule} recall note",
            "lines": [title, ", ".join(batches)]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_category_seasonal(ctx):
    payload = ctx.tf["payload"]
    season = G.payload_text(payload, "season") or "the season"
    trends = [t for t in as_list(payload.get("trends")) if t]
    item = G.resolve_digest(ctx.cf, ctx.tf, kind_pref=["seasonal"])
    actionable = squeeze(str((item or {}).get("actionable") or ""))
    trend_txt = ""
    parsed = []
    for t in trends[:4]:
        parts = str(t).split("_")
        m = re.search(r"([+-]?\d+)", str(t))
        if m and parts:
            pct_part = m.group(1)
            label = " ".join(p for p in parts[:-1] if p and p != "demand") or parts[0]
            label = humanize(label)
            if pct_part.startswith("-"):
                parsed.append(f"{label} down {abs(int(pct_part))}%")
            else:
                parsed.append(f"{label} +{pct_part.lstrip('+')}%")
    if parsed:
        trend_txt = ", ".join(parsed)
    head = f"{humanize(season)} demand shift in your category"
    if trend_txt:
        head += f": {trend_txt}"
    action_txt = f" {actionable}." if actionable else ""
    body = (f"{ctx.salut}, {head}.{action_txt} Reply YES and I'll draft the shelf note "
            f"and the customer WhatsApp for it.{ctx.tail()}")
    rationale = ("Seasonal category shift: serializes the payload's own trend deltas, "
                 "pairs them with the digest's actionable line, single binary CTA.")
    plan = {"type": "draft", "name": f"{humanize(season)} shelf note",
            "lines": [trend_txt or head, actionable]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_ipl(ctx):
    item = G.resolve_digest(ctx.cf, ctx.tf, kind_pref=["seasonal"])
    payload = ctx.tf["payload"]
    match = G.payload_text(payload, "match") or G.payload_text(payload, "fixture")
    venue = G.payload_text(payload, "venue")
    when = payload.get("match_time_iso")
    weeknight = payload.get("is_weeknight")
    when_txt = f"{fmt_date(when)}, {fmt_time(when)}" if when else "tonight"
    place_txt = f" at {venue}" if venue else ""
    time_txt = fmt_time(when) or when_txt
    head = f"{match}{place_txt} tonight at {time_txt}" if match else \
        f"IPL tonight ({when_txt})"
    summary = squeeze(str((item or {}).get("summary") or ""))
    actionable = squeeze(str((item or {}).get("actionable") or ""))
    saturday = bool(weeknight) is False
    if "down 12%" in summary or "+18%" in summary:
        if saturday:
            judgement = ("Saturday matches push covers down 12% vs a Saturday average "
                         "— people watch at home. Skip the match-night promo; push "
                         "delivery instead.")
        else:
            judgement = ("Weeknight matches run +18% on covers — this is a match-night "
                         "combo night.")
    else:
        judgement = actionable or summary
    offer = G.best_active_offer(ctx.f, ["match", "combo", "bogo", "pizza", "free"])
    offer_txt = f" Your live offer '{G.offer_title(offer)}' fits." if offer else ""
    body = (f"{ctx.salut}, quick heads-up — {head}. {judgement}{offer_txt} "
            f"Reply YES and I'll draft the creative.{ctx.tail()}")
    rationale = ("Match-day trigger read against the category's own IPL digest (contrarian "
                 "call on a Saturday), then routed to the merchant's live offer.")
    plan = {"type": "draft", "name": "match-night creative",
            "lines": [head, judgement]}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def _fallback_merchant(ctx, topic):
    """Unknown / thin trigger: still specific, still this merchant."""
    payload_line = G.payload_facts_line(ctx.tf["payload"])
    anchor = anchor_line(ctx)
    fix = fix_plan(ctx.f, ctx.cf)
    head = f"{ctx.salut}, quick note on {humanize(topic)}"
    if ctx.f["locality"]:
        head += f" for {ctx.biz}"
    head += "."
    detail = f" {payload_line[0].upper() + payload_line[1:]}." if payload_line else ""
    anchor_txt = f" {anchor}" if anchor else ""
    if "competitor" in str(topic):
        mine = G.best_active_offer(ctx.f)
        counter = (f" Your live offer '{G.offer_title(mine)}' is the natural counter."
                   if mine else "")
        ask = (f"Reply YES and I'll pull a side-by-side of both listings and draft your "
               f"counter.{counter}")
        plan = {"type": "list", "name": "listing side-by-side",
                "lines": [f"them: {humanize(topic)}", f"you: {ctx.biz}"]}
    else:
        ask = (f"Reply YES and I'll draft the fix and send it here — 2 minutes, "
               f"nothing to set up.")
        plan = {"type": "profile_fix", "fixes": fix}
    ask_done = ask if ask.endswith(".") else f"{ask}."
    body = f"{head}{detail}{anchor_txt} {ask_done}{ctx.tail()}"
    if "competitor" in str(topic):
        rationale = (f"Trigger '{ctx.tf['kind']}' had no usable payload facts, so the "
                     f"message is anchored on this merchant's own listing numbers and "
                     f"live offer; single binary ask with a concrete artifact delivered.")
    else:
        rationale = (f"Trigger '{ctx.tf['kind']}' had no usable payload; grounded instead "
                     f"on the merchant's own signals and a single low-effort action "
                     f"({fix[0]}).")
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


# ---------------------------------------------------------------------------
# Customer-scope handlers (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------

_CAT_EMOJI = {"dentists": "🦷", "gyms": "💪", "salons": "💇",
              "restaurants": "🍽️", "pharmacies": "💊"}

_RECALL_NOUN = {"dentists": "next visit", "salons": "next appointment",
                "gyms": "next session", "pharmacies": "next refill",
                "restaurants": "next visit"}

_HOLD_EN = {
    "gyms": "I'll hold your usual slot — no commitment.",
    "salons": "I'll hold your usual stylist at your usual time — no commitment.",
    "dentists": "I'll book your next visit at your usual time — no commitment.",
    "pharmacies": "we'll keep your regular refill ready — no commitment.",
    "restaurants": "I'll hold your usual table for your next visit — no commitment.",
}
_HOLD_HI = {
    "gyms": "main aapka usual slot hold kar loon — koi commitment nahi.",
    "salons": "main aapka usual stylist, usual time par hold kar loon — koi commitment nahi.",
    "dentists": "main aapke usual time par agli visit book kar loon — koi commitment nahi.",
    "pharmacies": "hum aapki regular refill ready rakhenge — koi commitment nahi.",
    "restaurants": "main aapki agli visit par usual table hold kar loon — koi commitment nahi.",
}


def _cat_emoji(slug):
    return _CAT_EMOJI.get(slug, "")


def _hold_phrase(slug, lang="en"):
    table = _HOLD_EN if lang == "en" else _HOLD_HI
    fallback = _HOLD_EN["gyms"] if lang == "en" else _HOLD_HI["gyms"]
    return table.get(slug, fallback)


def _customer_opening(ctx, emoji=""):
    lang = customer_lang(ctx.k)
    name = ctx.k["name"]
    biz = ctx.biz
    suffix = f" {emoji.strip()}" if str(emoji).strip() else ""
    if is_hindi(lang):
        return f"Hi {name}, {biz} yahan{suffix}", lang
    return f"Hi {name}, {biz} here{suffix}", lang


def h_recall(ctx):
    payload = ctx.tf["payload"]
    k = ctx.k
    opener, lang = _customer_opening(ctx, _cat_emoji(ctx.slug))
    service = G.payload_text(payload, "service_due")
    service_txt = humanize(service).replace("_", " ").replace("month ", "month ") if service else ""
    noun = _RECALL_NOUN.get(ctx.slug, "next visit")
    due_date = payload.get("due_date")
    months = months_between(k.get("last_visit"), ctx.now_dt)
    months = months if months and months > 0 else None
    slots = G.slot_labels(payload)

    usual = humanize(k.get("slots") or "weekday evening").replace("_", " ")
    if ctx.slug == "pharmacies":
        ask = (f"Reply YES and we'll keep it ready at your usual {usual} time."
               if lang == "en" else
               f"Reply YES, hum aapke usual {usual} time par ready rakh denge.")
    elif lang == "en":
        ask = f"Reply YES and I'll hold a slot on your usual {usual} time."
    else:
        ask = f"Reply YES, main aapke usual {usual} time par slot hold kar loon."
    if slots and len(slots) >= 2:
        s1, s2 = slots[0], slots[1]
        if lang == "en":
            body = (f"{opener} Your {service_txt or noun} is due"
                    f"{f' — {months} months since your last visit' if months else ''}. "
                    f"Two slots are open: {s1} or {s2}. "
                    f"Reply 1 for {s1}, 2 for {s2}, or tell us a time that works.")
        else:
            body = (f"{opener} Apki {service_txt or noun} ka time ho gaya"
                    f"{f' — pichhli visit se {months} months' if months else ''}. "
                    f"Apke liye 2 slots ready hain: {s1} ya {s2}. "
                    f"Reply 1 for {s1}, 2 for {s2}, ya jo time ho, bata dijiye.")
        cta = CTA_SLOT
    else:
        when_txt = f" due {fmt_date(due_date)}" if due_date else ""
        if lang == "en":
            body = (f"{opener} Your {service_txt or noun} is due"
                    f"{when_txt}{f' — {months} months since your last visit' if months else ''}. "
                    f"{ask}")
        else:
            body = (f"{opener} Aapki {service_txt or noun} ka time ho gaya"
                    f"{when_txt}{f' — pichhli visit se {months} months' if months else ''}. "
                    f"{ask}")
        cta = CTA_BINARY
    rationale = ("Customer-scoped recall sent as the merchant; due interval, service and "
                 "slot labels come verbatim from the trigger payload, and the language "
                 f"matches the customer's stated preference ({k.get('language')}).")
    plan = {"type": "schedule", "slots": slots or []}
    return ctx.msg(body, cta, rationale, plan=plan)


def h_lapsed(ctx):
    k = ctx.k
    state = k.get("state") or ctx.tf["kind"]
    payload = ctx.tf["payload"]
    opener, lang = _customer_opening(ctx, " 👋")
    days = G.payload_number(payload, "days_since_last_visit")
    if days is None:
        d = days_between(k.get("last_visit"), ctx.now_dt)
        days = d if d and d > 0 else None
    months = months_between(k.get("last_visit"), ctx.now_dt)
    gap = ""
    if days is not None and days >= 21:
        gap = (f"it's been {int(days)} days since your last visit" if lang == "en"
               else f"{int(days)} days ho gaye aapki last visit ko")
    elif months:
        plural = "" if months == 1 else "s"
        gap = (f"it's been {months} month{plural}" if lang == "en"
               else f"{months} month{plural} ho gaye")
    elif days and days > 7:
        weeks = days // 7
        plural = "" if weeks == 1 else "s"
        gap = (f"it's been about {weeks} week{plural}" if lang == "en"
               else f"kareeb {weeks} week ho gaye")
    services = k.get("services") or []
    focus = (k.get("prefs") or {}).get("training_focus") or (services[-1] if services else "")
    focus_txt = f" — your focus was {humanize(focus)}" if focus and lang == "en" else \
        (f" — {humanize(focus)} par aapka focus tha" if focus else "")
    offer = G.best_active_offer(ctx.f, ["trial", "first month", "free", "₹", "@"])
    offer_txt = ""
    if offer:
        title = G.offer_title(offer)
        offer_txt = (f" {title} is live right now." if lang == "en"
                     else f" abhi '{title}' live hai.")
    hold = _hold_phrase(ctx.slug, lang)
    if lang == "en":
        body = (f"{opener} {(gap[0].upper() + gap[1:]) if gap else 'Long time'}"
                f"{focus_txt}. Happens to everyone, no guilt.{offer_txt} "
                f"Reply YES and {hold}")
    else:
        body = (f"{opener} {gap or 'Kafi time ho gaya'}{focus_txt}. "
                f"Kisi ke saath bhi hota hai, koi baat nahi.{offer_txt} "
                f"Reply YES, {hold}")
    if state in ("lapsed_hard", "churned"):
        body += " First session back is on us." if lang == "en" and not offer_txt \
            and ctx.slug in ("gyms", "salons") else ""
    rationale = ("Customer win-back: no-shame framing, the gap is pulled from the "
                 "trigger's own days_since_last_visit (falling back to their stored "
                 "visit history), offer taken from the merchant's live catalogue, "
                 "single binary low-commitment CTA; language matches their preference.")
    plan = {"type": "schedule", "slots": []}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_appointment(ctx):
    payload = ctx.tf["payload"]
    k = ctx.k
    opener, lang = _customer_opening(ctx, " 📅")
    slot = payload.get("slot") or payload.get("appointment") or G.payload_text(payload, "time")
    slot_txt = f" at {slot}" if slot else ""
    pref = humanize(k.get("slots") or "").replace("_", " ")
    pref_txt = (f" We'll keep your usual {pref} timing in mind." if lang == "en" and pref
                else f" Aapka usual {pref} time yaad hai." if pref else "")
    if lang == "en":
        body = (f"{opener} Quick reminder: your appointment is tomorrow{slot_txt}.{pref_txt} "
                f"Reply YES to confirm, or tell us a time that suits you better.")
    else:
        body = (f"{opener} Yaad dilana hai: kal aapka appointment hai{slot_txt}.{pref_txt} "
                f"Reply YES to confirm, ya jo time sahi ho bata dijiye.")
    rationale = ("Appointment reminder: no time was present in the trigger payload, so "
                 "none was invented — the confirmation ask is grounded on their stored "
                 "slot preference and consented reminder opt-in.")
    plan = {"type": "schedule", "slots": [slot] if slot else []}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def h_refill(ctx):
    payload = ctx.tf["payload"]
    k = ctx.k
    opener, lang = _customer_opening(ctx, "")
    molecules = [str(m) for m in as_list(payload.get("molecule_list"))]
    runs_out = payload.get("stock_runs_out_iso")
    last = G.payload_text(payload, "last_refill")
    delivery = payload.get("delivery_address_saved")
    senior = bool((k.get("raw") or {}).get("identity", {}).get("senior_citizen")) or \
        any("senior" in str(s) for s in k.get("consent_scope", []))
    offer = G.best_active_offer(ctx.f, ["delivery", "senior", "discount"])
    offer_txt = ""
    if offer:
        title = G.offer_title(offer)
        offer_txt = (f" {title} applies." if lang == "en" else f" {title} lagu hai.")
    if molecules:
        mol_txt = ", ".join(molecules)
        when = f" run out on {fmt_date(runs_out)}" if runs_out else ""
        home_txt = ""
        if delivery and "delivery" not in offer_txt.lower():
            home_txt = (" Free home delivery to your saved address." if lang == "en"
                        else " Saved address par free home delivery.")
        if lang == "en":
            body = (f"{opener}. Your monthly medicines ({mol_txt}){when}. Same doses, same "
                    f"brands ready.{offer_txt}{home_txt} "
                    f"Reply CONFIRM to dispatch, or call us if the dosage has changed.")
        else:
            body = (f"{opener}. Aapki mahine ki dawaiyan ({mol_txt}){when}. Wahi dose, wahi "
                    f"brand pack ready hai.{offer_txt}{home_txt} "
                    f"Reply CONFIRM karein, ya dosage badla ho toh call kar dijiye.")
        cta = CTA_BINARY
    else:
        d = days_between(k.get("last_visit"), ctx.now_dt)
        gap = ""
        if d and d > 20:
            gap = (f" — your last visit was {d} days ago" if lang == "en"
                   else f" — aapki last visit {d} days pehle thi")
        pharmacy = ctx.slug == "pharmacies"
        if pharmacy:
            lead = "Time for your monthly refill" if lang == "en" else \
                "Mahine ki refill ka time aa gaya"
            close = (f"Reply CONFIRM and we'll have it packed for "
                     f"{'home delivery' if delivery else 'pickup'}." if lang == "en" else
                     f"Reply CONFIRM karein, hum pack kar denge"
                     f"{' — free home delivery' if delivery else ' — pickup ke liye'}.")
        else:
            lead = "Time for your follow-up" if lang == "en" else "Follow-up ka time ho gaya"
            close = ("Reply CONFIRM and we'll book you in." if lang == "en"
                     else "Reply CONFIRM karein, hum time rakh lenge.")
        body = f"{opener}. {lead}{gap}.{offer_txt} {close}"
        cta = CTA_BINARY
    rationale = ("Chronic refill: molecules + stock-out date taken verbatim from the "
                 "trigger payload, offer and delivery terms from the merchant's own "
                 "catalogue, Hindi-English matching the customer's preference.")
    plan = {"type": "draft", "name": "refill confirmation",
            "lines": molecules or ["monthly refill"]}
    return ctx.msg(body, cta, rationale, plan=plan)


def h_trial_followup(ctx):
    payload = ctx.tf["payload"]
    k = ctx.k
    opener, lang = _customer_opening(ctx, " ✅")
    trial = payload.get("trial_date")
    sessions = G.slot_labels(payload)
    trial_txt = f" on {fmt_date(trial)}" if trial else ""
    session_txt = f" Next open slot: {sessions[0]}." if sessions else ""
    if lang == "en":
        body = (f"{opener} Thanks for the trial{trial_txt}.{session_txt} "
                f"Want us to hold that spot for you? Reply YES and it's blocked — "
                f"cancel any time.")
    else:
        body = (f"{opener} Trial ke liye shukriya{trial_txt}.{session_txt} "
                f"Spot hold karein? Reply YES — cancel kabhi bhi kar sakte hain.")
    rationale = ("Trial follow-up: trial date and next session come from the payload; "
                 "single low-risk binary CTA, language matched to the customer.")
    plan = {"type": "schedule", "slots": sessions}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


def _window_label(raw) -> str:
    """'skin_prep_program_30day' -> 'the 30-day skin prep program'."""
    if not raw:
        return ""
    text = humanize(raw).replace("_", " ").strip().lower()
    text = re.sub(r"(\d)\s*([a-z])", r"\1 \2", text)
    m = re.search(r"(\d+)\s*day", text)
    if m:
        rest = (text[:m.start()] + " " + text[m.end():]).replace("  ", " ").strip()
        return f"the {m.group(1)}-day {rest}".replace("  ", " ").strip()
    return f"the {text}"


def h_bridal(ctx):
    payload = ctx.tf["payload"]
    k = ctx.k
    opener, lang = _customer_opening(ctx, " 💍")
    wedding = payload.get("wedding_date")
    trial = payload.get("trial_completed")
    days_to = G.payload_number(payload, "days_to_wedding")
    if days_to is None:
        days_to = days_between(ctx.now_dt, wedding)
    window = G.payload_text(payload, "next_step_window_open")
    window_txt = _window_label(window) or "the prep window"
    days_txt = f"{int(days_to)} days to your wedding" if days_to and days_to > 0 else \
        "your wedding is coming up"
    pref = str((k.get("prefs") or {}).get("preferred_slot") or k.get("slots") or "")
    pref = pref.replace("_", " ").strip()
    if pref:
        pref = pref[0].upper() + pref[1:]
    pref_txt = f"your usual {pref} slot" if pref else "a weekend slot"
    if lang == "en":
        body = (f"{opener} {days_txt[0].upper() + days_txt[1:]} — and now is the right "
                f"window to start {window_txt} before the main rush"
                f"{f' (bridal trial done {fmt_date(trial)})' if trial else ''}. "
                f"Shall I block {pref_txt} for the first session? Reply YES.")
    else:
        body = (f"{opener} {days_txt} — abhi {window_txt} shuru karne ka sahi time hai "
                f"before the festive rush. Reply YES and I'll block "
                f"{pref_txt.strip()} for the first session.")
    rationale = ("Bridal follow-up window: days-to-wedding and program window from the "
                 "payload, timing preference from the customer's own booking history, "
                 "one binary booking commitment.")
    plan = {"type": "schedule", "slots": []}
    return ctx.msg(body, CTA_BINARY, rationale, plan=plan)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

MERCHANT_HANDLERS = {
    "research_digest": h_research_digest,
    "research_digest_release": h_research_digest,
    "category_research_digest_release": h_research_digest,
    "cde_opportunity": h_cde,
    "regulation_change": h_regulation,
    "compliance_change": h_regulation,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "seasonal_perf_dip": h_seasonal_dip,
    "milestone_reached": h_milestone,
    "dormant_with_vera": h_dormant,
    "renewal_due": h_renewal,
    "winback_eligible": h_winback,
    "winback": h_winback,
    "festival_upcoming": h_festival,
    "festival": h_festival,
    "competitor_opened": h_competitor,
    "review_theme_emerged": h_review_theme,
    "curious_ask_due": h_curious_ask,
    "scheduled_recurring": h_curious_ask,
    "active_planning_intent": h_planning,
    "gbp_unverified": h_unverified,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "ipl_match_today": h_ipl,
}

CUSTOMER_HANDLERS = {
    "recall_due": h_recall,
    "customer_lapsed_soft": h_lapsed,
    "customer_lapsed_hard": h_lapsed,
    "customer_churned": h_lapsed,
    "appointment_tomorrow": h_appointment,
    "chronic_refill_due": h_refill,
    "refill_due": h_refill,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_bridal,
    "bridal_followup": h_bridal,
}

# kinds that are really customer-scope even when trigger.scope is wrong
CUSTOMER_KINDS = set(CUSTOMER_HANDLERS)


def compose(category: dict, merchant: dict, trigger: dict,
            customer: dict = None, now=None) -> dict:
    """Deterministic composition from the four contexts.

    Returns body / cta / send_as / suppression_key / rationale
    (+ template_name, template_params, plan for the transport layer).
    """
    ctx = Ctx(category, merchant, trigger, customer, now)
    kind = ctx.tf["kind"]

    if ctx.k is not None and not G.has_consent(ctx.k):
        return {
            "body": "",
            "cta": CTA_NONE,
            "send_as": "merchant_on_behalf",
            "suppression_key": ctx.tf["suppression"],
            "rationale": "Skipped: customer context carries no recorded opt-in for "
                         "merchant outreach, so no message is composed.",
            "template_name": "",
            "template_params": [],
            "plan": {"type": "generic", "topic": kind},
            "skipped": True,
        }

    if ctx.customer_facing and ctx.k is not None:
        handler = CUSTOMER_HANDLERS.get(kind) or h_lapsed
    elif kind in CUSTOMER_KINDS and ctx.k is not None:
        handler = CUSTOMER_HANDLERS[kind]
    else:
        handler = MERCHANT_HANDLERS.get(kind) or _fallback_merchant_ctx

    try:
        draft = handler(ctx)
    except Exception:
        if os.environ.get("VERA_STRICT") == "1":
            raise
        draft = _fallback_merchant_ctx(ctx)

    if not draft.get("body"):
        return {
            "body": "", "cta": CTA_NONE, "send_as": "vera",
            "suppression_key": ctx.tf["suppression"],
            "rationale": "Skipped: not enough grounded context to say something "
                         "useful without fabricating.",
            "template_name": "", "template_params": [],
            "plan": {"type": "generic", "topic": kind}, "skipped": True,
        }

    send_as = "merchant_on_behalf" if ctx.customer_facing else "vera"
    return {
        "body": draft["body"],
        "cta": draft["cta"],
        "send_as": send_as,
        "suppression_key": ctx.tf["suppression"] or f"{kind}:{ctx.f['id']}",
        "rationale": draft["rationale"],
        "template_name": draft.get("template_name") or f"vera_{_slug(kind)}_v1",
        "template_params": draft.get("template_params") or _params(ctx, draft["body"]),
        "plan": draft.get("plan") or {"type": "generic", "topic": kind},
        "skipped": False,
    }


def _fallback_merchant_ctx(ctx):
    return _fallback_merchant(ctx, ctx.tf["kind"] or "an update")
