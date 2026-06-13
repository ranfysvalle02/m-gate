from __future__ import annotations

import json
from typing import Protocol


class TextSpanExtractor(Protocol):
    """Pulls the human/agent-authored text spans out of a raw request body.

    Guardrails screen *content*, not framing — they care about the query string
    and string-valued tool arguments, not the JSON-RPC scaffolding around them.
    Isolating extraction behind this protocol keeps the security layer agnostic
    to the wire format: a REST surface would supply its own extractor while the
    GuardrailService and its detectors stay untouched.
    """

    def extract(self, body_text: str) -> list[str]:
        """Return the text spans to screen. Falls back to ``[body_text]`` when
        the payload shape is unknown, so screening never silently sees nothing.
        """
        ...


class JsonRpcSpanExtractor:
    """Extracts spans from a JSON-RPC body: ``params.query`` and the string
    values under ``params.arguments``.
    """

    def extract(self, body_text: str) -> list[str]:
        spans: list[str] = []
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            return [body_text]

        params = payload.get("params") if isinstance(payload, dict) else None
        if isinstance(params, dict):
            query = params.get("query")
            if isinstance(query, str) and query.strip():
                spans.append(query)

            arguments = params.get("arguments")
            if isinstance(arguments, dict):
                for value in arguments.values():
                    if isinstance(value, str) and value.strip():
                        spans.append(value)

        return spans or [body_text]
