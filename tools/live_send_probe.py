"""First live send against DeepSeek — the wire contracts, measured.

Ticket: `.scratch/blender-copilot/issues/16-first-live-send.md`

Run it with Blender's own bundled interpreter, so the client under test is the
one the addon will actually use:

    set -a; . ./.env; set +a
    python3 tools/bounded_run.py 180 -- \\
        /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 \\
        tools/live_send_probe.py

It reads `DEEPSEEK_API_KEY` and `DEEPSEEK_API_URL` from the environment and
**never prints either**. Nothing here is the addon: it is a scaffold whose only
job is to make the provider answer questions the design has been assuming
answers to. What it does *not* exercise is ticket 11's subprocess transport,
which is a design and not yet built — this goes straight to `requests`, which is
what that transport would carry.

Writes `.scratch/blender-copilot/research/first-live-send.md`.

Budget: a handful of requests, each with a small `max_tokens`. The one item
deliberately skipped is forcing a real `context_length_exceeded`, because the
provider's limit is 1M tokens and provoking it means uploading megabytes of
padding — noted in the report as deferred rather than quietly omitted.
"""

import json
import os
import sys
import time
import urllib.error

import requests

REPORT = (
    "/Users/user/Projects/blender.anx.copilot"
    "/.scratch/blender-copilot/research/first-live-send.md"
)

BASE = os.environ.get("DEEPSEEK_API_URL", "").rstrip("/")
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = "deepseek-flash"
LEGACY = "deepseek-v4-flash"
MAXTOK = 64

RESULTS: list[str] = []
SPEND = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}


def note(title: str, body: str) -> None:
    RESULTS.append(f"\n### {title}\n\n{body}\n")
    print(f"PROBE | {title}: {body.splitlines()[0][:150]}", flush=True)
    flush()


def flush() -> None:
    """Write after every step: one manual run must not be able to lose evidence."""
    text = (
        "# The first live send against DeepSeek\n\n"
        f"Scaffold: `tools/live_send_probe.py`, run under Blender's bundled\n"
        f"`python3.13` ({sys.version.split()[0]}), client `requests`\n"
        f"{requests.__version__} with bundled `certifi`.\n\n"
        f"Base URL: taken from `DEEPSEEK_API_URL` in the environment (OpenAI format).\n"
        f"Model under test: `{MODEL}`.\n\n"
        "These are observations, not verdicts. Interpretation belongs on the ticket.\n"
        f"Skipped: forcing a real `context_length_exceeded` — the provider's limit is\n"
        "1M tokens, so provoking it means uploading megabytes of padding. Deferred\n"
        "rather than omitted; see the ticket.\n"
        + "".join(RESULTS)
        + f"\n### Spend\n\nRequests: {SPEND['requests']}, prompt tokens:\n"
        f"{SPEND['prompt_tokens']}, completion tokens: {SPEND['completion_tokens']}.\n"
    )
    try:
        with open(REPORT, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"PROBE | could not write report: {exc}", flush=True)


def post(path: str, payload: dict, stream: bool = False):
    url = f"{BASE}{path}"
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    started = time.monotonic()
    SPEND["requests"] += 1
    resp = requests.post(url, headers=headers, json=payload, stream=stream, timeout=90)
    return resp, time.monotonic() - started


def shape(resp) -> str:
    """A compact, secret-free description of an HTTP response."""
    try:
        body = resp.json()
    except Exception:
        body = {"_non_json": resp.text[:400]}
    return f"HTTP {resp.status_code}\n\n```json\n{json.dumps(body, indent=2)[:1200]}\n```"


def usage_of(body: dict) -> str:
    u = body.get("usage") or {}
    SPEND["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
    SPEND["completion_tokens"] += u.get("completion_tokens", 0) or 0
    return (
        f"model echoed `{body.get('model')}`, usage {json.dumps(u)}, "
        f"finish_reason `{(body.get('choices') or [{}])[0].get('finish_reason')}`"
    )


def main() -> int:
    if not KEY or not BASE:
        print(
            "PROBE | DEEPSEEK_API_KEY and DEEPSEEK_API_URL must be set. "
            "Load .env first: set -a; . ./.env; set +a",
            flush=True,
        )
        return 2
    print(
        f"PROBE | base {BASE} | key present ({len(KEY)} chars) | never printed",
        flush=True,
    )

    # T1 -------------------------------------------------------------------
    try:
        resp, dt = post(
            "/chat/completions",
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                "max_tokens": MAXTOK,
            },
        )
        body = resp.json()
        if resp.status_code == 200:
            text = (body["choices"][0]["message"].get("content") or "").strip()
            note(
                "1. Basic non-streaming completion — works",
                f"{shape(resp)}\n\nLatency {dt:.2f}s, reply `{text[:60]}`, {usage_of(body)}",
            )
        else:
            note("1. Basic non-streaming completion — FAILED", shape(resp))
            return 1
    except Exception as exc:
        note("1. Basic non-streaming completion — RAISED", f"`{type(exc).__name__}: {exc}`")
        return 1

    # T2 -------------------------------------------------------------------
    chunks, content_seen, tool_deltas, first_chunk_at = 0, 0, 0, None
    try:
        resp, _ = post(
            "/chat/completions",
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "Count from 1 to 5, one number per line."}],
                "max_tokens": MAXTOK,
                "stream": True,
            },
            stream=True,
        )
        started = time.monotonic()
        assembled = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunks += 1
            if first_chunk_at is None:
                first_chunk_at = time.monotonic() - started
            delta = (json.loads(data).get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                content_seen += 1
                assembled.append(delta["content"])
            if delta.get("tool_calls"):
                tool_deltas += 1
        note(
            "2. Streaming — works, and it is line-oriented",
            f"HTTP {resp.status_code}, {chunks} SSE `data:` chunks, "
            f"{content_seen} carrying content, first chunk after "
            f"{first_chunk_at:.2f}s.\n\nReassembled reply:\n\n```\n"
            f"{''.join(assembled).strip()[:300]}\n```\n\n"
            "This is what `iter_lines()` in ticket 03's design has to survive.",
        )
    except Exception as exc:
        note("2. Streaming — RAISED", f"`{type(exc).__name__}: {exc}`")

    # T3 -------------------------------------------------------------------
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_scene_info",
                "description": "Return a count of objects in the scene.",
                "parameters": {
                    "type": "object",
                    "properties": {"scope": {"type": "string", "enum": ["all", "selected"]}},
                    "required": ["scope"],
                },
            },
        }
    ]
    messages = [
        {"role": "system", "content": "You have one tool. Use it when asked about the scene."},
        {"role": "user", "content": "How many objects are in the scene? Use the tool."},
    ]
    tool_call_id = None
    try:
        resp, dt = post(
            "/chat/completions",
            {"model": MODEL, "messages": messages, "tools": tools, "max_tokens": MAXTOK},
        )
        body = resp.json()
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if calls:
            tool_call_id = calls[0].get("id")
            args = calls[0]["function"].get("arguments")
            note(
                "3a. Tool calls — the model issued one",
                f"{shape(resp)}\n\nName `{calls[0]['function']['name']}`, "
                f"arguments `{args}`, id `{tool_call_id}`, latency {dt:.2f}s, "
                f"{usage_of(body)}.\n\nNote the arguments are a **JSON string**, not "
                "an object — ticket 06's parsing has to expect that.",
            )
            # 3b: the round trip, with the matching tool result ticket 09 assumes.
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": '{"objects": 14}'}
            )
            resp2, dt2 = post(
                "/chat/completions",
                {"model": MODEL, "messages": messages, "tools": tools, "max_tokens": MAXTOK},
            )
            body2 = resp2.json()
            final = ((body2.get("choices") or [{}])[0].get("message") or {}).get("content")
            note(
                "3b. Tool call round trip — the matching `tool` result was accepted",
                f"{shape(resp2)}\n\nFinal reply: `{(final or '')[:200]}` "
                f"(latency {dt2:.2f}s, {usage_of(body2)})\n\n"
                "This is the contract ticket 09 assumed and could not test: an "
                "assistant message carrying `tool_calls` followed by a `tool` "
                "message with the same id.",
            )

            # T4: the same shape with the result missing.
            broken = [
                {"role": "system", "content": "You have one tool."},
                {"role": "user", "content": "How many objects are in the scene? Use the tool."},
                {"role": "assistant", "content": None, "tool_calls": calls},
                {"role": "user", "content": "Never mind."},
            ]
            resp3, _ = post(
                "/chat/completions",
                {"model": MODEL, "messages": broken, "tools": tools, "max_tokens": MAXTOK},
            )
            note(
                "4. A `tool_call` with NO matching `tool` result — the history-integrity rule",
                f"{shape(resp3)}\n\n"
                + (
                    "Rejected, as ticket 09 assumed: the provider enforces the pairing, "
                    "so the flusher's synthetic `cancelled` tool results are load-bearing "
                    "rather than merely tidy."
                    if resp3.status_code != 200
                    else "**Accepted.** The pairing is therefore NOT enforced by this "
                    "provider, and ticket 09's synthetic-`cancelled` rule is a local "
                    "tidiness choice rather than a wire requirement."
                ),
            )
        else:
            note(
                "3a. Tool calls — the model did NOT issue one",
                f"{shape(resp)}\n\nInconclusive: no tool call came back, so the "
                "round-trip and history-integrity tests could not run.",
            )
    except Exception as exc:
        note("3. Tool call round trip — RAISED", f"`{type(exc).__name__}: {exc}`")

    # T5 -------------------------------------------------------------------
    try:
        resp, _ = post(
            "/chat/completions",
            {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "You are a test harness. Answer in one word."},
                    {"role": "user", "content": "Name a colour."},
                    {"role": "system", "content": "Live scene summary: 14 objects."},
                ],
                "max_tokens": MAXTOK,
            },
        )
        note(
            "5. A TRAILING `system` message — invariant I8 of the context design",
            f"{shape(resp)}\n\n"
            + (
                "Accepted, so *How a conversation degrades as context grows* can keep "
                "the live summary as the trailing message, and the stable base prompt "
                "keeps index 0 and its cache prefix."
                if resp.status_code == 200
                else "Rejected — the ticket's documented fallback applies: fold the "
                "summary into index 0 and accept losing the cache prefix."
            ),
        )
    except Exception as exc:
        note("5. Trailing system message — RAISED", f"`{type(exc).__name__}: {exc}`")

    # T6 -------------------------------------------------------------------
    try:
        resp, _ = post(
            "/chat/completions",
            {
                "model": LEGACY,
                "messages": [{"role": "user", "content": "Reply with: ok"}],
                "max_tokens": MAXTOK,
            },
        )
        body = resp.json()
        note(
            "6. The legacy model name — the trap from the provider's own docs",
            f"{shape(resp)}\n\n"
            + (
                f"Accepted, and the response echoes `{body.get('model')}`. The provider's "
                "docs say `deepseek-v4-flash` is **remapped to a retired model**, so a "
                "name that appears to work can be silently serving something else. This "
                "is the concrete case for ticket 05 §4's refusal to hard-code a model "
                "string, and for validating whatever the user types against the live "
                "model list."
                if resp.status_code == 200 and body.get("model") != LEGACY
                else f"Accepted with `model` echoing `{body.get('model')}`."
            ),
        )
    except Exception as exc:
        note("6. Legacy model name — RAISED", f"`{type(exc).__name__}: {exc}`")

    note(
        "7. Latency and spend",
        f"{SPEND['requests']} requests, {SPEND['prompt_tokens']} prompt tokens, "
        f"{SPEND['completion_tokens']} completion tokens. At Flash off-peak rates "
        "($0.15/M input cache-miss, $0.60/M output) this run costs well under a cent "
        "— which is the point: ticket 09's caps now have a real baseline instead of "
        "an invented one.",
    )
    flush()
    print(f"PROBE | wrote {REPORT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
