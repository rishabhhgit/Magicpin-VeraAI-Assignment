"""Mechanical rubric lint over submission.jsonl — the five judge dimensions,
checked deterministically so copy regressions are caught before an LLM run.

    python3 tools/rubric_lint.py                # all 30 pairs
    python3 tools/rubric_lint.py --dataset DIR  # contexts to load against

Exit code 1 if any message has a hard failure (no anchor, wrong send_as,
multiple CTAs, taboos, URL, over length).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.voice import HARD_TABOOS, sanitize  # noqa: E402

CTA_KINDS = {"open_ended", "binary_yes_no", "multi_choice_slot", "link_only", "none"}

CATEGORY_WORDS = {
    "dentists": "tooth teeth dental cleaning root canal crown implant extraction "
                "orthodontic braces whitening filling x-ray radiograph fluoride "
                "gingivitis anaesthesia impression scan cad cam cde credits "
                "dentist visit treatment".split(),
    "salons": "haircut hair colour balayage highlights keratin spa facial bleach "
              "manicure pedicure bridal groom haircut beard".split(),
    "restaurants": "menu dish thali biryani pizza pasta dessert delivery dine-in "
                   "combo meal kitchen order table cover".split(),
    "gyms": "gym fitness trainer workout class weights cardio yoga zumba pilates "
            "membership squat deadlift protein retention slot regulars camp".split(),
    "pharmacies": "medicine medicines refill prescription generic strip tablet "
                  "capsule pharmacy stock delivery sanitizer ors sunscreen shelf "
                  "counter antifungal".split(),
}

WHY_NOW = re.compile(
    r"(tonight|today|tomorrow|first[- ]time|newly|just opened|still unverified|"
    r"\bdue\b|expir|renew|days? (away|left|short|since)|over the last|since your|"
    r"\bup \d|\bdown \d|launched|is here|this week|is live|live right now|"
    r"heads-?up|worth (blocking|flagging)|you asked|reminder|heads up|"
    r"circular|effective|window|season|recall|worth closing|good week|quick one|"
    r"run(s)? out|days? ago|short of|been a while|since we last|new competitor|"
    r"competitor|demand shift|yaad dilana|\d{1,2} [A-Z][a-z]{2})", re.I)

GENERIC = re.compile(
    r"\b(increase your sales|grow your business|boost your revenue|"
    r"take your business to the next level|10% off|improve your online presence)\b", re.I)

CTA_RE = re.compile(r"\b(reply|tell me|send me|tap|call us|book now)\b", re.I)

GENERIC_SERVICE = ("appointment", "visit", "session", "refill", "booking", "slot",
                   "table", "menu", "offer", "profile", "listing", "post", "trial",
                   "class", "delivery", "shelf", "counter", "recall", "treatment")


def load_contexts(dataset_dir):
    contexts = {}
    for kind, folder, id_key in (("category", "categories", "slug"),
                                 ("merchant", "merchants", "merchant_id"),
                                 ("customer", "customers", "customer_id"),
                                 ("trigger", "triggers", "id")):
        path = os.path.join(dataset_dir, folder)
        if not os.path.isdir(path):
            continue
        for fn in sorted(os.listdir(path)):
            if fn.endswith(".json"):
                item = json.load(open(os.path.join(path, fn), encoding="utf-8"))
                contexts.setdefault(kind, {})[item.get(id_key) or fn[:-5]] = item
    return contexts


def load_pairs(dataset_dir):
    path = os.path.join(dataset_dir, "test_pairs.json")
    if os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
        return data["pairs"] if isinstance(data, dict) else data
    return []


def anchors(body):
    found = []
    found += re.findall(r"₹[\d,]+(?:\.\d+)?", body)
    found += re.findall(r"\b\d+(?:\.\d+)?%", body)
    found += re.findall(r"\b\d{1,2} [A-Z][a-z]{2}(?: \d{4})?\b", body)
    found += re.findall(r"\b\d{1,2}:\d{2}(?:am|pm)?\b", body, re.I)
    found += re.findall(r"'\s*[^']{6,45}\s*'", body)
    found += re.findall(r"\b\d+(?:\.\d+)?\s?(?:km|credits?|days?|reviews|profile views"
                        r"|mSv|baseline)\b", body, re.I)
    found += re.findall(r"\b(today|tomorrow|tonight|kal)\b", body, re.I)
    found += re.findall(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]* \d{1,2} [A-Z][a-z]{2}\b", body)
    found += re.findall(r"\b[A-Z][a-z]{2}-[A-Z][a-z]{2}\b", body)
    found += re.findall(r"\b\d+ (?:x|times)\b", body, re.I)
    return found


def lint_one(row, pair, contexts):
    raw = str(row["body"] or "")
    body = raw
    issues, notes = [], []
    if sanitize(raw, {}) != raw:
        issues.append("body not pre-sanitised")
    slug = (pair or {}).get("_slug")
    merchant = (pair or {}).get("_merchant") or {}
    trigger = (pair or {}).get("_trigger") or {}
    category = (pair or {}).get("_category") or {}
    customer = (pair or {}).get("_customer")

    # --- hard contract ----------------------------------------------------
    if "http" in body.lower() or "www." in body.lower():
        issues.append("URL in body")
    for taboo in HARD_TABOOS:
        if taboo in body.lower():
            issues.append(f"taboo: {taboo}")
    if GENERIC.search(body):
        issues.append("generic framing")
    if len(body) > 700:
        issues.append(f"too long ({len(body)})")
    if "  " in body or re.search(r"\.\s*\.", body):
        issues.append("spacing artifact")
    if row.get("cta") not in CTA_KINDS:
        issues.append(f"bad cta: {row.get('cta')}")
    if row.get("send_as") not in ("vera", "merchant_on_behalf"):
        issues.append(f"bad send_as: {row.get('send_as')}")
    if not row.get("suppression_key"):
        issues.append("missing suppression_key")
    if len(row.get("rationale") or "") < 40:
        issues.append("thin rationale")
    if (trigger or {}).get("scope") == "customer" and row.get("send_as") != "merchant_on_behalf":
        issues.append("customer trigger not sent merchant_on_behalf")
    if (trigger or {}).get("scope") == "merchant" and row.get("send_as") != "vera":
        issues.append("merchant trigger not sent as vera")

    # --- specificity ------------------------------------------------------
    found = anchors(body)
    if not found:
        issues.append("no concrete anchor")
    else:
        notes.append(f"anchors={len(found)}")

    # --- category fit -----------------------------------------------------
    words = CATEGORY_WORDS.get(slug, [])
    hits = [w for w in words if re.search(rf"\b{re.escape(w)}", body, re.I)]
    generic = [w for w in GENERIC_SERVICE if re.search(rf"\b{re.escape(w)}", body, re.I)]
    if words and not hits and not generic:
        issues.append("no category vocabulary")
    emoji = re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", body)
    expected = {"dentists": "🦷", "gyms": "💪", "salons": "💇", "pharmacies": "💊",
                "restaurants": "🍽️"}
    want = expected.get(slug)
    for em in emoji:
        if want and em != want and em not in ("📅", "👋", "✅", "😊"):
            issues.append(f"emoji {em} off-category for {slug}")

    # --- merchant fit -----------------------------------------------------
    ident = (merchant or {}).get("identity") or {}
    owner = str(ident.get("owner_first_name") or "").strip()
    biz = str(((merchant or {}).get("profile") or {}).get("name")
              or (merchant or {}).get("name") or "")
    if biz and biz.split()[0].lower() not in body.lower() and \
            (owner and owner.lower() not in body.lower()):
        issues.append("no merchant identity in body")
    langs = [str(x).lower() for x in (ident.get("languages") or [])]
    has_hi = any(x.startswith("hi") for x in langs)
    if has_hi and row.get("send_as") == "vera" and not re.search(
            r"\b(chalega|bataiye|karoon|karo|hai|ho gaya)\b", body, re.I):
        issues.append("hi merchant without Hindi tail")
    if customer:
        pref = str(((customer or {}).get("identity") or {}).get("language_pref") or "en")
        if pref.startswith("hi") and not re.search(
                r"\b(ho gaya|karein|dijiye|hai|aapka|kar loon)\b", body, re.I):
            issues.append("hi customer without Hindi body")

    # --- trigger relevance ------------------------------------------------
    if not WHY_NOW.search(body):
        issues.append("no why-now signal")

    # --- engagement compulsion -------------------------------------------
    ask_sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if CTA_RE.search(s)]
    if len(ask_sentences) > 1:
        issues.append(f"multiple ask sentences ({len(ask_sentences)})")
    if not re.search(r"\b(reply|tell me|send me)\b", body, re.I):
        issues.append("no reply hook")

    return issues, notes, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(ROOT, "expanded"))
    ap.add_argument("--submission", default=os.path.join(ROOT, "submission.jsonl"))
    args = ap.parse_args()

    contexts = load_contexts(args.dataset)
    pairs = {p["test_id"]: p for p in load_pairs(args.dataset)}
    rows = [json.loads(l) for l in open(args.submission, encoding="utf-8") if l.strip()]

    hard = 0
    counts = {"anchors": [], "clean": 0}
    for row in rows:
        pair = dict(pairs.get(row["test_id"], {}))
        merchants = contexts.get("merchant", {})
        triggers = contexts.get("trigger", {})
        pair["_merchant"] = merchants.get(pair.get("merchant_id"), {})
        pair["_trigger"] = triggers.get(pair.get("trigger_id"), {})
        pair["_category"] = contexts.get("category", {}).get(
            (pair["_merchant"] or {}).get("category_slug"), {})
        pair["_customer"] = contexts.get("customer", {}).get(pair.get("customer_id"))
        pair["_slug"] = (pair["_category"] or {}).get("slug") or \
            (pair["_merchant"] or {}).get("category_slug")

        issues, notes, found = lint_one(row, pair, contexts)
        counts["anchors"].append(len(found))
        status = "OK  " if not issues else "FAIL"
        if issues:
            hard += 1
        print(f"{status} {row['test_id']} [{row['send_as']}|{row['cta']}] "
              f"{'; '.join(notes)}")
        for issue in issues:
            print(f"       - {issue}")

    total = len(rows)
    print(f"\n{total - hard}/{total} clean | anchors avg "
          f"{sum(counts['anchors']) / max(total, 1):.1f} per message")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
