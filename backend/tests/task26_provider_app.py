"""Deterministic local HTTP provider used only by the Task 26 browser gate."""

import json
import re

from fastapi import FastAPI, Request

app = FastAPI()
# The production prompt serializes evidence in more than one safe JSON layout
# (compact answer prompts and spaced staff-draft prompts).  Preserve the exact
# durable chunk identity while accepting harmless JSON whitespace.
_CHUNK_ID = re.compile(r'"chunk_id"\s*:\s*"([0-9a-f-]{36})"')


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, object]:
    payload = await request.json()
    inputs = payload.get("input", [])
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": [1.0] * 1536}
            for index, _ in enumerate(inputs)
        ],
        "model": payload.get("model", "text-embedding-3-small"),
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@app.post("/v1/messages")
async def messages(request: Request) -> dict[str, object]:
    payload = await request.json()
    system = payload.get("system")
    if isinstance(system, str) and system.startswith("Classify the untrusted customer message"):
        text = '{"sensitive_topic":null}'
    else:
        message = payload["messages"][0]["content"]
        chunk_id = _CHUNK_ID.search(message)
        assert chunk_id is not None
        answer = "Regenerated grounded reply."
        text = json.dumps(
            {"text": answer, "claims": [{"text": answer, "citation_ids": [chunk_id.group(1)]}]}
        )
    return {
        "id": "task26-local-message",
        "model": "task26-local-anthropic",
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
