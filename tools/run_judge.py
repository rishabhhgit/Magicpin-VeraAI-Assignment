"""Run the official judge_simulator.py against a local bot, credentials from env.

    GROQ_API_KEY=gsk_... python3 tools/run_judge.py                      # full run
    MISTRAL_API_KEY=...  python3 tools/run_judge.py --provider mistral \
                         --scenario phase2_short

Options:
  --scenario  warmup | phase2_short | hostile | intent_transition |
              auto_reply_hell | all (default) | full_evaluation
  --provider  groq (default) | mistral | openrouter | openai | anthropic |
              gemini | deepseek | ollama
  --model     override the provider's default model
  --bot-url   default http://localhost:$PORT (starts `bot.py` for you if it's down)

Nothing is written to judge_simulator.py — configuration is patched at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib import error as urlerror

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import judge_simulator as js  # noqa: E402

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "mistral": "mistral-large-latest",
}


class OpenAICompatProvider(js.LLMProvider):
    """Minimal OpenAI-compatible chat provider (used for mistral)."""

    def __init__(self, api_key, model, api_url):
        self.api_key, self.model, self.api_url = api_key, model, api_url

    def name(self):
        return f"{self.model} ({self.api_url.split('//')[1].split('/')[0]})"

    def complete(self, prompt, system=None):
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        req = urllib.request.Request(
            self.api_url, data=json.dumps({
                "model": self.model, "messages": messages,
                "temperature": 0.2, "max_tokens": 1500}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=js.TIMEOUT_LLM) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def pick_key(provider):
    env_name = {"groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY"}.get(
        provider, f"{provider.upper()}_API_KEY")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get(env_name, "")
    if key:
        return key
    # Convenience: key staged in a local file (never committed, delete after use)
    for name in (f".{provider}_key", ".judge_key"):
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
    return ""


def healthz(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=os.environ.get("JUDGE_SCENARIO", "all"))
    ap.add_argument("--provider", default=os.environ.get("JUDGE_PROVIDER", "groq"))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL", ""))
    ap.add_argument("--bot-url", default=os.environ.get(
        "BOT_URL", f"http://localhost:{os.environ.get('PORT', '8080')}"))
    args = ap.parse_args()

    js.BOT_URL = args.bot_url
    js.TEST_SCENARIO = args.scenario
    js.LLM_PROVIDER = args.provider

    key = pick_key(args.provider)
    if args.provider != "ollama" and not key:
        print(f"Missing API key for provider '{args.provider}'. "
              f"Export {'GROQ_API_KEY' if args.provider == 'groq' else args.provider.upper() + '_API_KEY'} "
              f"(or JUDGE_API_KEY) and retry.")
        return 2
    js.LLM_API_KEY = key
    js.LLM_MODEL = args.model or DEFAULT_MODELS.get(args.provider, "")

    if args.provider == "mistral":
        js.create_provider = lambda: OpenAICompatProvider(
            key, js.LLM_MODEL or DEFAULT_MODELS["mistral"],
            "https://api.mistral.ai/v1/chat/completions")
    elif js.LLM_PROVIDER != "groq":
        js.LLM_MODEL = args.model or ""

    bot = None
    if not healthz(args.bot_url):
        port = args.bot_url.rsplit(":", 1)[-1]
        print(f"bot not reachable at {args.bot_url} — starting `bot.py` on port {port}")
        bot = subprocess.Popen([sys.executable, os.path.join(ROOT, "bot.py")],
                               env={**os.environ, "PORT": port, "VERA_QUIET": "1"},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if healthz(args.bot_url):
                break
            time.sleep(0.25)
        else:
            bot.terminate()
            print("bot failed to start")
            return 3

    try:
        js.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        if bot:
            bot.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
