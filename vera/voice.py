"""Category voice: salutation, language match, taboo enforcement, CTA tails."""

from __future__ import annotations

import re

from .util import as_list, humanize, squeeze

# Words that read as promotional/overclaim regardless of category.
HARD_TABOOS = [
    "guaranteed", "100% safe", "completely cure", "miracle", "best in city",
    "best food in city", "guaranteed packed house", "viral guarantee",
    "instant transformation", "permanent results", "guaranteed glow",
    "shred in 7 days", "fastest results", "guaranteed weight loss",
    "doctor approved", "guaranteed result", "100% effective",
]

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def category_slug(category) -> str:
    return str((category or {}).get("slug") or (category or {}).get("category_slug") or "").strip()


def merchant_slug(merchant) -> str:
    return str((merchant or {}).get("category_slug") or "").strip()


def first_name(merchant) -> str:
    ident = (merchant or {}).get("identity") or {}
    owner = squeeze(str(ident.get("owner_first_name") or ""))
    if owner:
        return owner
    name = squeeze(str(ident.get("name") or ""))
    return name or ""


def salutation(merchant, category=None, style="owner"):
    """`Dr. Meera` for dentists, `Lakshmi` elsewhere, falls back to the biz name."""
    owner = first_name(merchant)
    slug = merchant_slug(merchant) or category_slug(category)
    if not owner:
        return squeeze(str(((merchant or {}).get("identity") or {}).get("name") or "there"))
    if slug == "dentists" and not re.match(r"^(dr\.?|dr )\s*", owner, flags=re.I):
        return f"Dr. {owner}"
    if slug == "pharmacies" and style == "warm" and not owner.lower().startswith(("mr", "mrs", "ms")):
        return owner
    return owner


def biz_name(merchant) -> str:
    return squeeze(str(((merchant or {}).get("identity") or {}).get("name") or "your business"))


def locality(merchant) -> str:
    ident = (merchant or {}).get("identity") or {}
    return squeeze(str(ident.get("locality") or ident.get("city") or ""))


def languages(merchant) -> list:
    return [str(x).lower() for x in as_list(((merchant or {}).get("identity") or {}).get("languages"))]


def merchant_code_mix(merchant) -> bool:
    """True when a short Hindi connective is welcome (identity.languages has 'hi')."""
    return "hi" in languages(merchant)


def code_mix_tail(merchant, key, category=None) -> str:
    """One short Hindi connective appended to the CTA for hi-language merchants."""
    if not merchant_code_mix(merchant):
        return ""
    slug = merchant_slug(merchant) or category_slug(category)
    if slug == "pharmacies":
        options = [" Bataiye?", " Chalega?"]
    elif slug == "dentists":
        options = [" Chalega?", " Bataiye?"]
    else:
        options = [" Chalega?", " Bataiye?", " Karoon?"]
    from .util import stable_hash
    return options[stable_hash(key) % len(options)]


def customer_lang(customer) -> str:
    """'en' | 'hi_en' | 'hi'. Accepts a raw CustomerContext or customer_facts()."""
    c = customer or {}
    ident = c.get("identity") or (c.get("raw") or {}).get("identity") or {}
    pref = str(ident.get("language_pref") or c.get("language") or "").lower()
    if "hi" in pref and ("mix" in pref or "en" in pref):
        return "hi_en"
    if pref.strip() in ("hi", "hindi"):
        return "hi"
    return "en"


def is_hindi(lang: str) -> bool:
    return lang in ("hi", "hi_en")


# --- CTA taxonomy -----------------------------------------------------------
CTA_BINARY = "binary_yes_no"
CTA_OPEN = "open_ended"
CTA_SLOT = "multi_choice_slot"
CTA_NONE = "none"


def cta_sentence(kind: str, text: str) -> str:
    return squeeze(text)


# --- Sanitisation -----------------------------------------------------------

def taboos_for(category) -> list:
    voice = (category or {}).get("voice") or {}
    out = [str(t) for t in as_list(voice.get("vocab_taboo"))]
    return [t for t in out if t]


def sanitize(body: str, category=None) -> str:
    """Strip URLs (Meta rejects them) + overclaim words before send."""
    from .util import squeeze_lines
    text = squeeze_lines(str(body or ""))
    text = _URL_RE.sub("", text)
    text = squeeze_lines(text)

    banned = [t.lower() for t in HARD_TABOOS] + [t.lower() for t in taboos_for(category)]
    for phrase in sorted(set(banned), key=len, reverse=True):
        if not phrase:
            continue
        pattern = re.compile(re.escape(phrase), flags=re.I)
        if pattern.search(text):
            # Drop the whole clause containing the overclaim rather than leaving a stub.
            text = re.sub(r"[^.;!?]*" + re.escape(phrase) + r"[^.;!?]*[.;!?]?", "", text,
                          flags=re.I)
            text = squeeze_lines(text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def body_is_specific(body: str) -> bool:
    return bool(re.search(r"\d", body or ""))


def topic_keywords() -> set:
    """In-mission vocabulary — anything else is politely declined in replies."""
    return {
        "profile", "google", "gbp", "listing", "post", "posts", "photo", "photos",
        "review", "reviews", "rating", "offer", "offers", "campaign", "discount",
        "deal", "customer", "customers", "patient", "patients", "booking",
        "appointment", "recall", "message", "whatsapp", "vera", "magicpin",
        "views", "calls", "ctr", "leads", "analytics", "dashboard", "traffic",
        "search", "seo", "keywords", "competitor", "subscription", "plan", "renew",
        "renewal", "payment", "invoice for plan", "festival", "diwali", "seasonal",
        "thali", "haircut", "spa", "facial", "dental", "teeth", "implant", "aligner",
        "gym", "membership", "trial", "class", "yoga", "pharmacy", "medicine",
        "medicines", "refill", "delivery", "stock", "shelf", "menu", "order",
        "swiggy", "zomato", "bridal", "wedding", "stylist", "colour", "color",
        "post", "story", "instagram", "creative", "banner", "copy", "draft",
        "engagement", "walk-in", "footfall", "covers", "dip", "spike", "trend",
        "digest", "research", "study", "compliance", "dci", "fssai", "cde",
        "nudge", "message", "template", "send", "sms", "broadcast", "win-back",
        "winback", "lapsed", "dormant", "milestone", "report", "summary",
    }


def looks_out_of_scope(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    off_mission = [
        "gst", "tax filing", "income tax", "gst return", "loan", "emi", "credit score",
        "insurance claim", "divorce", "legal notice", "hr policy", "payroll", "salary",
        "hiring", "resume", "boyfriend", "girlfriend", "cricket score", "share price",
        "homework", "essay", "python code", "bug in my", "mobile repair", "laptop",
        "puncture", "passport", "visa", "aadhaar", "pan card", "electricity bill",
        "water bill", "rent agreement",
    ]
    if any(k in t for k in off_mission):
        return True
    # A direct ask we can't service, phrased as "can you also help with X"
    if re.search(r"\bcan you (also )?(help|do|handle)\b", t) and not any(
            k in t for k in ("profile", "offer", "post", "campaign", "listing", "review",
                             "message", "customer", "patient", "booking")):
        return True
    return False
