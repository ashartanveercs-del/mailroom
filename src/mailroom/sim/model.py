"""A scripted stand-in for an OpenAI-compatible chat-completions endpoint.

Each script entry says: when the document sent to the model contains `needle` (or, for an
image, has this content hash), answer with `reply`. That lets a corpus describe the mistakes a
real model makes (confidently wrong on a look-alike, a hallucinated number, malformed JSON, an
outage) and check that the pipeline catches every one of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..http import Request, Response

DEFAULT_REPLY = {"doc_type": "other", "confidence": 0.2, "fields": {}}


@dataclass(frozen=True)
class Scripted:
    needle: str
    reply: Mapping[str, Any] | str
    status: int = 200


class SimulatedModel:
    def __init__(self, script: Sequence[Scripted] = (),
                 default: Mapping[str, Any] | None = None) -> None:
        self.script = list(script)
        self.default = dict(default or DEFAULT_REPLY)
        self.requests: list[dict[str, Any]] = []

    @staticmethod
    def image_key(data: bytes) -> str:
        return "image:" + hashlib.sha256(data).hexdigest()

    def send(self, request: Request) -> Response:
        body = json.loads(request.body or b"{}")
        self.requests.append({"url": request.url, "headers": dict(request.headers),
                              "body": body})
        content = body["messages"][-1]["content"]
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        texts, images = [], set()
        for part in parts:
            if part.get("type") == "text":
                texts.append(part["text"])
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                images.add(self.image_key(base64.b64decode(url.split(",", 1)[1])))
        haystack = "\n".join(texts)
        for entry in self.script:
            hit = (entry.needle in images if entry.needle.startswith("image:")
                   else entry.needle in haystack)
            if hit:
                if entry.status != 200:
                    return Response(entry.status, {}, b'{"error": {"message": "unavailable"}}')
                return self._answer(entry.reply)
        return self._answer(self.default)

    @staticmethod
    def _answer(reply: Mapping[str, Any] | str) -> Response:
        content = reply if isinstance(reply, str) else json.dumps(reply)
        payload = {"choices": [{"index": 0, "message": {"role": "assistant",
                                                        "content": content}}]}
        return Response(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())
