# The first live send against DeepSeek

Scaffold: `tools/live_send_probe.py`, run under Blender's bundled
`python3.13` (3.13.13), client `requests`
2.32.3 with bundled `certifi`.

Base URL: taken from `DEEPSEEK_API_URL` in the environment (OpenAI format).
Model under test: `deepseek-flash`.

These are observations, not verdicts. Interpretation belongs on the ticket.
Skipped: forcing a real `context_length_exceeded` — the provider's limit is
1M tokens, so provoking it means uploading megabytes of padding. Deferred
rather than omitted; see the ticket.

### 1. Basic non-streaming completion — works

HTTP 200

```json
{
  "id": "40471823-825e-42ce-981b-37ed13527f06",
  "object": "chat.completion",
  "created": 1790362167,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "",
        "reasoning_content": "The user wants me to reply with the single word \"pong\". This is a simple request. I should just reply \"pong\".\n\nBut wait, I need to consider if there's any hidden instruction. The system prompt says I'm an AI assistant accessed via an API. The user says \"Reply with the single"
      },
      "logprobs": null,
      "finish_reason": "length"
    }
  ],
  "usage": {
    "prompt_tokens": 38,
    "completion_tokens": 64,
    "total_tokens": 102,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 64
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 38
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

Latency 1.50s, reply ``, model echoed `deepseek-flash`, usage {"prompt_tokens": 38, "completion_tokens": 64, "total_tokens": 102, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 64}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 38}, finish_reason `length`

### 2. Streaming — works, and it is line-oriented

HTTP 200, 41 SSE `data:` chunks, 9 carrying content, first chunk after 0.04s.

Reassembled reply:

```
1
2
3
4
5
```

This is what `iter_lines()` in ticket 03's design has to survive.

### 3a. Tool calls — the model issued one

HTTP 200

```json
{
  "id": "5f29507e-5b11-4339-aca1-846056bef953",
  "object": "chat.completion",
  "created": 1790362170,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "",
        "reasoning_content": "The user wants to know how many objects are in the scene. I should call get_scene_info with scope \"all\".",
        "tool_calls": [
          {
            "index": 0,
            "id": "call_00_wUBDLyK1hSA7nNuPQA4M1293",
            "type": "function",
            "function": {
              "name": "get_scene_info",
              "arguments": "{\"scope\": \"all\"}"
            }
          }
        ]
      },
      "logprobs": null,
      "finish_reason": "length"
    }
  ],
  "usage": {
    "prompt_tokens": 326,
    "completion_tokens": 64,
    "total_tokens": 390,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 25
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 326
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

Name `get_scene_info`, arguments `{"scope": "all"}`, id `call_00_wUBDLyK1hSA7nNuPQA4M1293`, latency 1.04s, model echoed `deepseek-flash`, usage {"prompt_tokens": 326, "completion_tokens": 64, "total_tokens": 390, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 25}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 326}, finish_reason `length`.

Note the arguments are a **JSON string**, not an object — ticket 06's parsing has to expect that.

### 3b. Tool call round trip — the matching `tool` result was accepted

HTTP 200

```json
{
  "id": "9439dc1f-55ab-4adc-9a1b-8215c66f3ffc",
  "object": "chat.completion",
  "created": 1790362171,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "There are **14 objects** in the scene.",
        "reasoning_content": ""
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 408,
    "completion_tokens": 11,
    "total_tokens": 419,
    "prompt_tokens_details": {
      "cached_tokens": 256
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0
    },
    "prompt_cache_hit_tokens": 256,
    "prompt_cache_miss_tokens": 152
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

Final reply: `There are **14 objects** in the scene.` (latency 0.98s, model echoed `deepseek-flash`, usage {"prompt_tokens": 408, "completion_tokens": 11, "total_tokens": 419, "prompt_tokens_details": {"cached_tokens": 256}, "completion_tokens_details": {"reasoning_tokens": 0}, "prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 152}, finish_reason `stop`)

This is the contract ticket 09 assumed and could not test: an assistant message carrying `tool_calls` followed by a `tool` message with the same id.

### 4. A `tool_call` with NO matching `tool` result — the history-integrity rule

HTTP 400

```json
{
  "error": {
    "message": "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message) (request_id: bbe4c94c-27d7-4a39-91d2-979f3cb59667)",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_request_error"
  }
}
```

Rejected, as ticket 09 assumed: the provider enforces the pairing, so the flusher's synthetic `cancelled` tool results are load-bearing rather than merely tidy.

### 5. A TRAILING `system` message — invariant I8 of the context design

HTTP 200

```json
{
  "id": "086a4168-d15c-4c60-a226-95251b4ebfe4",
  "object": "chat.completion",
  "created": 1790362176,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Blue",
        "reasoning_content": "We need answer one word. User asks \"Name a colour.\" Need comply one word. Any color. But also \"Live scene summary: 14 objects.\" Maybe irrelevant. We should answer one word. Need final only one word. Choose \"Blue\". Ensure no punctuation? \"Blue\" is one word."
      },
      "logprobs": null,
      "finish_reason": "length"
    }
  ],
  "usage": {
    "prompt_tokens": 54,
    "completion_tokens": 64,
    "total_tokens": 118,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 62
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 54
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

Accepted, so *How a conversation degrades as context grows* can keep the live summary as the trailing message, and the stable base prompt keeps index 0 and its cache prefix.

### 6. The legacy model name — the trap from the provider's own docs

HTTP 200

```json
{
  "id": "137d2868-0367-400f-b9b9-9b5341eb0d89",
  "object": "chat.completion",
  "created": 1790362178,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "ok",
        "reasoning_content": "The user just wants me to reply with \"ok\". Simple."
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 34,
    "completion_tokens": 15,
    "total_tokens": 49,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 13
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 34
  },
  "system_fingerprint": "aeb56401ca74e127821c4f9126dcb669"
}
```

Accepted, and the response echoes `deepseek-flash`. The provider's docs say `deepseek-v4-flash` is **remapped to a retired model**, so a name that appears to work can be silently serving something else. This is the concrete case for ticket 05 §4's refusal to hard-code a model string, and for validating whatever the user types against the live model list.

### 7. Latency and spend

7 requests, 772 prompt tokens, 139 completion tokens. At Flash off-peak rates ($0.15/M input cache-miss, $0.60/M output) this run costs well under a cent — which is the point: ticket 09's caps now have a real baseline instead of an invented one.

### Spend

Requests: 7, prompt tokens:
772, completion tokens: 139.
