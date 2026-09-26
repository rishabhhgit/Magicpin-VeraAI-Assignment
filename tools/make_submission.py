"""Build submission.jsonl from the 30 canonical test pairs.

    python3 tools/make_submission.py                # uses expanded/ if present
    python3 tools/make_submission.py --dataset DIR --out submission.jsonl

Reads categories / merchants / customers / triggers (either the expanded
per-entity layout or the seed bundle layout) plus test_pairs.json, runs
`vera.compose.compose()` for each pair and writes one JSON object per line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.compose import compose  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_dataset(dataset_dir):
    """Returns (categories, merchants, customers, triggers) keyed by id."""
    categories, merchants, customers, triggers = {}, {}, {}, {}

    def load_dir(name, bucket, id_key):
        folder = os.path.join(dataset_dir, name)
        if not os.path.isdir(folder):
            return False
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".json"):
                continue
            item = _read(os.path.join(folder, fn))
            bucket[item.get(id_key) or item.get("slug") or fn[:-5]] = item
        return True

    load_dir("categories", categories, "slug")
    if not load_dir("merchants", merchants, "merchant_id"):
        for m in _read(os.path.join(dataset_dir, "merchants_seed.json")).get("merchants", []):
            merchants[m["merchant_id"]] = m
    if not load_dir("customers", customers, "customer_id"):
        for c in _read(os.path.join(dataset_dir, "customers_seed.json")).get("customers", []):
            customers[c["customer_id"]] = c
    if not load_dir("triggers", triggers, "id"):
        for t in _read(os.path.join(dataset_dir, "triggers_seed.json")).get("triggers", []):
            triggers[t["id"]] = t
    return categories, merchants, customers, triggers


def load_pairs(dataset_dir):
    path = os.path.join(dataset_dir, "test_pairs.json")
    if os.path.exists(path):
        data = _read(path)
        return data["pairs"] if isinstance(data, dict) else data
    # fall back: first 30 seed triggers in id order
    triggers = _read(os.path.join(dataset_dir, "triggers_seed.json")).get("triggers", [])
    pairs = []
    for i, t in enumerate(sorted(triggers, key=lambda x: x["id"])[:30], start=1):
        pairs.append({"test_id": f"T{i:02d}", "trigger_id": t["id"],
                      "merchant_id": t.get("merchant_id"),
                      "customer_id": t.get("customer_id")})
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(ROOT, "expanded"))
    ap.add_argument("--out", default=os.path.join(ROOT, "submission.jsonl"))
    ap.add_argument("--now", default=None, help="reference clock (default dataset today)")
    args = ap.parse_args()

    dataset_dir = args.dataset
    if not os.path.exists(os.path.join(dataset_dir, "test_pairs.json")) and \
            os.path.exists(os.path.join(ROOT, "expanded", "test_pairs.json")):
        dataset_dir = os.path.join(ROOT, "expanded")

    categories, merchants, customers, triggers = load_dataset(dataset_dir)
    pairs = load_pairs(dataset_dir)
    print(f"dataset: {dataset_dir} | categories={len(categories)} merchants={len(merchants)} "
          f"customers={len(customers)} triggers={len(triggers)} pairs={len(pairs)}")

    lines, skipped = [], []
    for pair in pairs:
        trigger = triggers.get(pair["trigger_id"])
        if trigger is None:
            skipped.append((pair["test_id"], "missing trigger"))
            continue
        merchant = merchants.get(pair.get("merchant_id") or trigger.get("merchant_id")) or {}
        customer = customers.get(pair.get("customer_id")) if pair.get("customer_id") else None
        category = categories.get(merchant.get("category_slug")) or \
            categories.get((trigger.get("payload") or {}).get("category")) or {}
        message = compose(category, merchant, trigger, customer, now=args.now)
        if message.get("skipped") or not message.get("body"):
            skipped.append((pair["test_id"], message.get("rationale", "empty body")))
            continue
        lines.append({
            "test_id": pair["test_id"],
            "body": message["body"],
            "cta": message["cta"],
            "send_as": message["send_as"],
            "suppression_key": message["suppression_key"],
            "rationale": message["rationale"],
        })

    with open(args.out, "w", encoding="utf-8") as fh:
        for row in lines:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"wrote {len(lines)} lines -> {args.out}")
    if skipped:
        print("SKIPPED:")
        for tid, why in skipped:
            print(f"  {tid}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
