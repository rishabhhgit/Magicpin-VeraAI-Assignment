# Vera — message engine (magicpin AI Challenge)

Pure-Python, zero-dependency, deterministic. No LLM calls at runtime, no randomness,
no wall-clock reads (the dataset's `today` is the reference clock unless `now` is given).

## Run

```bash
python3 bot.py                      # serves on $PORT (default 8080)
curl localhost:8080/v1/healthz
python3 tests/test_engine.py        # 22 tests: composer, reply engine, HTTP contract
python3 tests/test_deploy.py        # 8 tests: WSGI adapter + KV store cold-start
python3 tools/make_submission.py    # regenerates submission.jsonl (30 canonical pairs)
python3 tools/rubric_lint.py        # deterministic lint of all 5 judge dimensions
python3 tools/run_judge.py          # official LLM judge (key via env or .groq_key)
```

Endpoints: `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`,
`GET /v1/metadata`, `POST /v1/teardown`. `bot.compose(category, merchant, trigger,
customer)` is the brief's entry point; `conversation_handlers.respond(state, msg)`
demonstrates multi-turn handling on top of the same engine.

## Deploy (Vercel)

Import the repo with framework preset **Other** (no build command). `api/*.py` +
`vercel.json` expose the same six endpoints; `vera/wsgi.py` adapts them to the
runtime, and `vera/persistent.py` moves state into KV.

| Env var | Needed | Purpose |
|---|---|---|
| `KV_REST_API_URL` + `KV_REST_API_TOKEN` | **yes, for state** | Vercel KV (or Upstash `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN`) — persists contexts, suppression keys and conversations across cold starts. Create the store in *Storage*, attach it to the project and Vercel injects both automatically. Without them the bot still runs, but a fresh instance won't see what an earlier one stored. |
| `VERA_TEAM_NAME`, `VERA_TEAM_MEMBERS`, `VERA_MODEL`, `VERA_CONTACT_EMAIL` | no | identity returned by `GET /v1/metadata` |
| `VERA_QUIET=1` | no | silence request logs |
| `HOST`, `PORT` | no | provided by Vercel |

Judge keys (`GROQ_API_KEY`, `JUDGE_*`) stay local — the judge runs on your machine
against `BOT_URL=https://<app>.vercel.app` (or `https://<app>.vercel.app/api` if you
call the functions directly).

## Approach

**Compose is a lookup, not a generation.** Each of the 24 trigger kinds has a handler
that builds the message as: *grounded anchor → category-voice framing → exactly one CTA*.
Every number, date, price, offer title, peer stat and headline is read from the four
contexts (or derived from them by stated arithmetic) — nothing is invented, no URLs are
emitted, and output passes a taboo/URL/length gate before it leaves the function.
Thin or placeholder payloads fall back to the merchant's own signals (CTR vs peer,
reviews vs 150, live offers) instead of going generic.

**Category fit is a template choice, not a style switch.** Dentists get clinical-peer
framing (compliance circulars, CDE credits, treatment names); gyms get retention/trial
language; pharmacies get stock/shelf/delivery vocabulary; salons get trend + look copy.
Hindi is used only where the context says so — `identity.languages` contains `hi` for
merchants, `language_pref` for customers — and only as a single natural tail
(`Chalega?` / `Bataiye?`), with full Hinglish bodies for `hi` customers.

**State lives in `Store`; the server is a shell.** Contexts are versioned (idempotent on
re-push, 409 on stale version), triggers are deduped by `suppression_key`, ticks sort by
urgency and cap at 12 sends, and consent is gated at send time. The reply engine ranks
incoming text (hostile → end, auto-reply → staged exit, commitment → deliver, out of
scope → decline + redirect, question → grounded answer) and never fabricates an answer
it can't source from the contexts.

## Tradeoffs

- **Determinism over fluency.** A rule engine guarantees the same input always yields the
  same message and never hallucinates, but copy is assembled from templates, so long-tail
  phrasing is less varied than an LLM's would be.
- **Tight numeric grounding over flexibility.** Derived figures (e.g. tiered draft prices)
  are shown as proposals and labelled as drafts, not facts, to stay inside the "no
  fabricated numbers" rule.
- **In-memory state only.** Suppression/conversation state dies with the process; a restart
  is clean and reproducible, which favours judging over production durability.
- **Single message per trigger.** We optimise the one send the rubric scores rather than
  splitting context across a sequence.

## What additional context would have helped most

1. **A real message history per merchant** (send/open/reply stats) — "why now" would be
   grounded in behaviour instead of inferred from `conversation_history`.
2. **Offer redemption/conversion rates** — lets the CTA pick the lever with evidence
   rather than by category priors.
3. **Explicit customer consent scope for service messages** (reminders/recalls vs
   promotional) so gating can be tighter than opt-in-or-skip.
4. **Merchant-authored voice samples** (2–3 past posts) to match tone beyond category.

## Files

`bot.py` (stdlib HTTP server) · `vera/api.py` (shared dispatch + tick/reply logic) ·
`vera/compose.py` (per-kind handlers) · `vera/reply_engine.py` (multi-turn) ·
`vera/store.py` (state) + `vera/persistent.py`/`vera/kv.py` (KV persistence) ·
`vera/wsgi.py` + `api/` (Vercel functions) · `vera/ground.py`, `vera/voice.py`,
`vera/util.py` (facts, sanitisation, formatting) · `tests/` · `tools/` ·
`submission.jsonl` · `conversation_handlers.py` (optional multi-turn demo).
