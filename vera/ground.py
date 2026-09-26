"""Fact extraction: turn the four contexts into usable, non-fabricated anchors.

Rule enforced across this module: every number we surface must come from one of
the four contexts (or arithmetic on them). Nothing is invented.
"""

from __future__ import annotations

import re

from .util import (as_list, beat_matches, days_between, fmt_delta, fmt_date,
                   humanize, inr, month_of, months_between, nfmt, now_dt,
                   parse_dt, pct, squeeze)
from .voice import biz_name, first_name, locality, salutation

SKIP_PAYLOAD_KEYS = {
    "placeholder", "metric_or_topic", "merchant_id", "customer_id", "category",
    "ask_template", "last_ask_at", "is_expected_seasonal", "verification_path",
}


# --- merchant ---------------------------------------------------------------

def merchant_facts(merchant) -> dict:
    m = merchant or {}
    ident = m.get("identity") or {}
    sub = m.get("subscription") or {}
    perf = m.get("performance") or {}
    delta = perf.get("delta_7d") or {}
    offers = as_list(m.get("offers"))
    history = as_list(m.get("conversation_history"))
    agg = m.get("customer_aggregate") or {}
    signals = []
    for raw in as_list(m.get("signals")):
        text = str(raw)
        if ":" in text:
            name, _, value = text.partition(":")
            signals.append({"name": name.strip(), "value": value.strip(), "raw": text})
        else:
            signals.append({"name": text.strip(), "value": "", "raw": text})
    return {
        "id": str(m.get("merchant_id") or ""),
        "name": squeeze(str(ident.get("name") or "")),
        "owner": first_name(m),
        "salut": salutation(m),
        "city": squeeze(str(ident.get("city") or "")),
        "locality": locality(m),
        "verified": bool(ident.get("verified")),
        "languages": [str(x).lower() for x in as_list(ident.get("languages"))],
        "sub_status": str(sub.get("status") or ""),
        "plan": str(sub.get("plan") or ""),
        "days_remaining": sub.get("days_remaining"),
        "days_since_expiry": sub.get("days_since_expiry"),
        "renewal_amount": sub.get("renewal_amount"),
        "views": perf.get("views"),
        "calls": perf.get("calls"),
        "directions": perf.get("directions"),
        "leads": perf.get("leads"),
        "ctr": perf.get("ctr"),
        "delta": delta,
        "offers_active": [o for o in offers if str(o.get("status", "")).lower() == "active"],
        "offers_other": [o for o in offers if str(o.get("status", "")).lower() != "active"],
        "history": history,
        "agg": agg,
        "signals": signals,
        "themes": as_list(m.get("review_themes")),
        "category_slug": str(m.get("category_slug") or ""),
    }


def signal_value(facts, name):
    for s in facts["signals"]:
        if s["name"] == name:
            return s["value"] or True
    return None


def has_signal(facts, name) -> bool:
    return any(s["name"] == name for s in facts["signals"])


def signal_note(facts, count=1, exclude=()) -> str:
    """A short human phrase for the most actionable merchant signal."""
    order = ["perf_dip_severe", "dormant_with_vera", "no_active_offers",
             "unverified_gbp", "stale_posts", "ctr_below_peer_median",
             "no_recent_post", "winback_eligible", "trial_ending_soon",
             "renewal_due_soon", "new_merchant", "delivery_not_set_up",
             "seasonal_dip_apr_may", "high_risk_adult_cohort"]
    notes = {
        "stale_posts": "your Google posts are stale (last one {v} ago)",
        "ctr_below_peer_median": "your click-through rate sits below the peer median",
        "no_active_offers": "you have no live offer on your profile",
        "unverified_gbp": "your Google profile is still unverified",
        "perf_dip_severe": "your profile performance has dropped sharply",
        "no_recent_post": "you haven't posted to your profile recently",
        "dormant_with_vera": "we haven't spoken in {v} days",
        "winback_eligible": "your plan lapsed {v} days ago",
        "trial_ending_soon": "your trial ends in {v} days",
        "renewal_due_soon": "your plan renews in {v} days",
        "new_merchant": "you're new on magicpin",
        "delivery_not_set_up": "home delivery isn't set up on your listing",
        "high_risk_adult_cohort": "a large high-risk adult patient cohort",
        "seasonal_dip_apr_may": "the Apr-Jun acquisition lull",
    }
    skip = set(exclude or ())
    chosen = [s for s in facts["signals"]
              if s["name"] in order and s["name"] not in skip]
    chosen.sort(key=lambda s: order.index(s["name"]))
    out = []
    for s in chosen[:count]:
        tpl = notes.get(s["name"])
        if not tpl:
            continue
        if "{v}" in tpl and not s["value"]:
            continue          # never emit "lapsed  days ago"
        val = str(s["value"]).strip()
        if "{v} days" in tpl and val.endswith("d") and val[:-1].isdigit():
            val = val[:-1]    # signal value "12d" -> "12 days"
        out.append(tpl.format(v=val))
    return " and ".join(out)


def delta_line(facts, prefer=None) -> tuple:
    """(metric_label, delta_value) for the most telling 7d movement."""
    delta = facts.get("delta") or {}
    if prefer and prefer in delta:
        return prefer, delta.get(prefer)
    candidates = [(k, v) for k, v in delta.items() if k.endswith("_pct") and isinstance(v, (int, float))]
    if not candidates:
        return None, None
    candidates.sort(key=lambda kv: abs(float(kv[1])), reverse=True)
    return candidates[0]


# --- category ---------------------------------------------------------------

def category_facts(category) -> dict:
    c = category or {}
    return {
        "slug": str(c.get("slug") or ""),
        "voice": c.get("voice") or {},
        "peer": c.get("peer_stats") or {},
        "digest": as_list(c.get("digest")),
        "content": as_list(c.get("patient_content_library")),
        "beats": as_list(c.get("seasonal_beats")),
        "trends": as_list(c.get("trend_signals")),
        "offers": as_list(c.get("offer_catalog")),
        "display": str(c.get("display_name") or c.get("slug") or ""),
    }


def resolve_digest(cfacts, trigger, kind_pref=None) -> dict:
    payload = (trigger or {}).get("payload") or {}
    wanted = [payload.get("top_item_id"), payload.get("digest_item_id"),
              payload.get("item_id"), payload.get("alert_id"), payload.get("id")]
    digest = cfacts.get("digest") or []
    for want in wanted:
        if not want:
            continue
        for item in digest:
            if str(item.get("id")) == str(want):
                return item
    if kind_pref:
        for pref in kind_pref:
            for item in digest:
                if str(item.get("kind", "")).lower() == pref:
                    return item
    return digest[0] if digest else None


def best_trend(cfacts) -> dict:
    trends = [t for t in (cfacts.get("trends") or []) if isinstance(t.get("delta_yoy"), (int, float))]
    if not trends:
        return None
    return sorted(trends, key=lambda t: float(t["delta_yoy"]), reverse=True)[0]


def beat_for(cfacts, when=None) -> dict:
    month = month_of(when) or now_dt(when).month
    for beat in cfacts.get("beats") or []:
        if beat_matches(beat.get("month_range"), month):
            return beat
    return None


def peer_line(facts, cfacts) -> str:
    """Compare this merchant's CTR against the category peer benchmark."""
    peer = cfacts.get("peer") or {}
    ctr, avg = facts.get("ctr"), peer.get("avg_ctr")
    if not isinstance(ctr, (int, float)) or not isinstance(avg, (int, float)) or not avg:
        return ""
    gap = (avg - ctr) / avg
    if abs(gap) < 0.05:
        return f"your CTR of {pct(ctr)} is level with the peer benchmark of {pct(avg)}"
    if gap > 0:
        return f"your CTR of {pct(ctr)} sits below the peer benchmark of {pct(avg)}"
    return f"your CTR of {pct(ctr)} is above the peer benchmark of {pct(avg)}"


def peer_views_line(facts, cfacts) -> str:
    peer = cfacts.get("peer") or {}
    views, avg = facts.get("views"), peer.get("avg_views_30d")
    if not isinstance(views, (int, float)) or not isinstance(avg, (int, float)) or not avg:
        return ""
    diff = (views - avg) / avg
    if abs(diff) < 0.05:
        return f"your {nfmt(views)} profile views/30d are level with the {nfmt(avg)} peer average"
    word = "above" if diff > 0 else "below"
    return f"your {nfmt(views)} profile views/30d are {word} the {nfmt(avg)} peer average"


def catalog_offer(cfacts, keywords=None, audience=None) -> dict:
    """A category-level canonical offer (used only as a *proposal*)."""
    for offer in cfacts.get("offers") or []:
        title = str(offer.get("title") or "")
        if audience and offer.get("audience") not in (audience, "all", None):
            continue
        if not keywords:
            return offer
        if any(k.lower() in title.lower() for k in keywords):
            return offer
    return None


def best_active_offer(facts, keywords=None) -> dict:
    for offer in facts.get("offers_active") or []:
        title = str(offer.get("title") or "")
        if not keywords or any(k.lower() in title.lower() for k in keywords):
            return offer
    return None


def offer_for_new_users(facts, cfacts, keywords=None) -> tuple:
    """(offer_dict, owned:bool) — merchant's own live offer first, else catalog."""
    mine = best_active_offer(facts, keywords)
    if mine:
        return mine, True
    cat = catalog_offer(cfacts, keywords, audience="new_user")
    if cat:
        return cat, False
    return None, False


def offer_title(offer) -> str:
    return squeeze(str((offer or {}).get("title") or ""))


# --- trigger ----------------------------------------------------------------

def trigger_facts(trigger) -> dict:
    t = trigger or {}
    payload = t.get("payload") or {}
    return {
        "id": str(t.get("id") or ""),
        "scope": str(t.get("scope") or "merchant"),
        "kind": str(t.get("kind") or ""),
        "source": str(t.get("source") or ""),
        "payload": payload,
        "urgency": t.get("urgency") or 1,
        "suppression": str(t.get("suppression_key") or t.get("id") or ""),
        "expires": t.get("expires_at"),
        "merchant_id": str(t.get("merchant_id") or payload.get("merchant_id") or ""),
        "customer_id": t.get("customer_id") or payload.get("customer_id"),
    }


def payload_number(payload, *keys):
    for k in keys:
        v = payload.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def payload_text(payload, *keys):
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip() and v.lower() not in ("true", "false"):
            return v
    return None


def payload_facts_line(payload, limit=3) -> str:
    """Serialize injected payload values into a grounded sentence fragment."""
    parts = []
    for key, value in (payload or {}).items():
        if key in SKIP_PAYLOAD_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            parts.append(f"{humanize(key)} {nfmt(value)}" if isinstance(value, int)
                         else f"{humanize(key)} {value}")
        elif isinstance(value, str) and len(value) <= 60:
            parts.append(f"{humanize(key)} {value}")
        if len(parts) >= limit:
            break
    return "; ".join(parts)


# --- customer ---------------------------------------------------------------

def customer_facts(customer) -> dict:
    c = customer or {}
    ident = c.get("identity") or {}
    rel = c.get("relationship") or {}
    prefs = c.get("preferences") or {}
    consent = c.get("consent") or {}
    return {
        "id": str(c.get("customer_id") or ""),
        "name": squeeze(str(ident.get("name") or "").split("(")[0].strip()) or "there",
        "language": str(ident.get("language_pref") or "en"),
        "state": str(c.get("state") or ""),
        "first_visit": rel.get("first_visit"),
        "last_visit": rel.get("last_visit"),
        "visits": rel.get("visits_total"),
        "services": [str(s) for s in as_list(rel.get("services_received")) if s],
        "ltv": rel.get("lifetime_value"),
        "chronic": as_list(rel.get("chronic_conditions")),
        "prefs": prefs,
        "slots": str(prefs.get("preferred_slots") or ""),
        "reminder_opt_in": prefs.get("reminder_opt_in"),
        "consent_at": consent.get("opted_in_at"),
        "consent_scope": [str(s) for s in as_list(consent.get("scope"))],
        "raw": c,
    }


def has_consent(cfacts) -> bool:
    """Only gate on *absent* consent — an explicit opt-out with no timestamp."""
    if cfacts is None:
        return True
    if not cfacts.get("consent_at"):
        return False
    if cfacts.get("reminder_opt_in") is False and not cfacts.get("consent_scope"):
        return False
    return True


def since_visit_line(cfacts, now=None) -> str:
    months = months_between(cfacts.get("last_visit"), now_dt(now))
    if months is None:
        return ""
    if months <= 0:
        return "you visited this month"
    if months == 1:
        return "it's been a month since your last visit"
    if months < 24:
        return f"it's been {months} months since your last visit"
    years = months // 12
    return f"it's been about {years} year{'s' if years > 1 else ''} since your last visit"


def slot_labels(payload) -> list:
    out = []
    for slot in as_list(payload.get("available_slots")) or as_list(payload.get("next_session_options")):
        if isinstance(slot, dict):
            label = slot.get("label") or slot.get("iso")
            if label:
                out.append(str(label))
        elif slot:
            out.append(str(slot))
    return out


def visit_count_phrase(cfacts) -> str:
    visits = cfacts.get("visits")
    if isinstance(visits, int) and visits > 0:
        return f"visit #{visits}" if visits < 10 else f"{visits} visits"
    return ""


def months_since_refill(payload, now=None) -> str:
    last = payload.get("last_refill") or payload.get("last_visit")
    months = months_between(last, now_dt(now))
    if months is None:
        return ""
    if months <= 0:
        return "this month"
    return f"{months} month{'s' if months != 1 else ''} ago"


def offer_sentence(offer, owned=True) -> str:
    title = offer_title(offer)
    if not title:
        return ""
    if owned:
        return f"{title} is live on your profile"
    return f"'{title}' is the category's best-converting entry offer"


def service_word(slug) -> str:
    return {
        "dentists": "treatment", "salons": "service", "restaurants": "dish",
        "gyms": "class", "pharmacies": "medicine",
    }.get(slug, "service")
