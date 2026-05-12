"""One-shot smoke test for the local Qwen Tier 1 backend.

Hits LM Studio's OpenAI-compatible endpoint at localhost:1234 with a
small structured-output prompt. Verifies:
  - server reachable
  - target model is loaded
  - chat completion returns non-empty content
  - latency is within expected range for Qwen 3.6-27B on RTX PRO 5000

Use this after starting LM Studio with Qwen 3.6-27B Q4_K_M loaded, before
flipping config/settings.yaml's llm.t1.backend to qwen_local.

Exits 0 on success, 1 on any failure (network, schema, latency outlier,
or empty response). Failures print a structured reason.

Run:
    python scripts/verify_qwen_local.py
    python scripts/verify_qwen_local.py --base-url http://localhost:1234/v1
    python scripts/verify_qwen_local.py --model qwen/qwen3.6-27b

The default base_url is http://localhost:1234/v1 (LM Studio default).
The default expected model id is the production target name pattern.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen/qwen3.6-27b"

TEST_PROMPT = (
    "You are a structured-output checker. "
    "Reply ONLY with the single word PONG. "
    "No punctuation, no explanation."
)
EXPECTED_OUTPUT = "PONG"


def _http_get(url: str, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, body: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body_bytes = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {body_bytes.decode('utf-8', errors='replace')[:200]}")
        return json.loads(body_bytes.decode("utf-8"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help="LM Studio OpenAI-compatible base (default http://localhost:1234/v1)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="Expected model id (default qwen/qwen3.6-27b)")
    p.add_argument("--latency-warn-s", type=float, default=10.0,
                   help="Warn if completion takes longer than this many seconds")
    args = p.parse_args()

    print(f"Base URL: {args.base_url}")
    print(f"Expected model: {args.model}")

    # Step 1: list models
    try:
        models_response = _http_get(f"{args.base_url}/models")
    except urllib.error.URLError as exc:
        print(f"\nFAIL: cannot reach LM Studio at {args.base_url}: {exc.reason}",
              file=sys.stderr)
        print("Is the server running? In LM Studio > Developer/Local Server > Start Server.",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    loaded_ids = [m.get("id") for m in models_response.get("data") or []]
    print(f"Loaded models: {loaded_ids}")
    if args.model not in loaded_ids:
        print(f"\nFAIL: expected model {args.model!r} not in loaded list "
              f"{loaded_ids}", file=sys.stderr)
        print("In LM Studio > My Models, click Load on the Qwen3.6-27B-Q4_K_M entry.",
              file=sys.stderr)
        return 1
    print(f"  OK: {args.model} is loaded")

    # Step 2: chat completion smoke
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 512,  # Qwen 3.6 is a thinking model; allow room for <think>...</think> + answer
        "temperature": 0.0,
    }
    print(f"\nCalling chat completions with prompt: {TEST_PROMPT[:60]}...")
    t0 = time.monotonic()
    try:
        resp = _http_post_json(f"{args.base_url}/chat/completions", body, timeout=60)
    except Exception as exc:
        print(f"\nFAIL: chat call raised: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    latency = time.monotonic() - t0

    choices = resp.get("choices") or []
    if not choices:
        print(f"\nFAIL: response had no choices: {resp}", file=sys.stderr)
        return 1
    content = (choices[0].get("message") or {}).get("content") or ""
    content_stripped = content.strip()

    usage = resp.get("usage") or {}
    print(f"\n  Response: {content_stripped!r}")
    print(f"  Latency : {latency:.2f}s")
    print(f"  Tokens  : input={usage.get('prompt_tokens')} output={usage.get('completion_tokens')}")

    if not content_stripped:
        print("\nFAIL: response content was empty", file=sys.stderr)
        msg = choices[0].get("message") or {}
        print(f"  Full message object: {json.dumps(msg, indent=2)[:1000]}", file=sys.stderr)
        return 1
    if EXPECTED_OUTPUT not in content_stripped.upper():
        print(f"\nFAIL: expected {EXPECTED_OUTPUT!r} in response, got {content_stripped!r}",
              file=sys.stderr)
        print("Model may be loaded but the prompt template is misaligned.",
              file=sys.stderr)
        return 1
    if latency > args.latency_warn_s:
        print(f"\nWARNING: latency {latency:.2f}s exceeds threshold "
              f"{args.latency_warn_s}s. Expected ~1-3s for Qwen 3.6-27B "
              f"on RTX PRO 5000 with CUDA 12 runtime.", file=sys.stderr)

    print("\nOK - Qwen 3.6-27B local Tier 1 backend verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
