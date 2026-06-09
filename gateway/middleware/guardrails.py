from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from config.settings import get_settings
from gateway.middleware.span_extractor import JsonRpcSpanExtractor, TextSpanExtractor
from services.guardrails import GuardrailService
from services.metrics import observe_guardrail_event

logger = logging.getLogger(__name__)


class GuardrailsMiddleware:
    def __init__(self, app, span_extractor: TextSpanExtractor | None = None):
        self.app = app
        self.settings = get_settings()
        self.guardrails = GuardrailService()
        # Defaults to JSON-RPC extraction; a different transport (e.g. REST)
        # can inject its own extractor without touching the guardrail logic.
        self.span_extractor: TextSpanExtractor = span_extractor or JsonRpcSpanExtractor()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        # Guard both custom JSON-RPC and mounted MCP transport surfaces.
        if not (request.url.path.startswith("/rpc") or request.url.path.startswith("/mcp")):
            return await self.app(scope, receive, send)

        # Reject on the declared Content-Length before buffering the body, so an
        # oversized payload is turned away at the door instead of being read into
        # memory in full first. The post-read length check below still backstops
        # clients that lie about (or omit) Content-Length.
        if self._declared_length_exceeds_limit(request):
            observe_guardrail_event("request_size", "blocked")
            response = JSONResponse(status_code=413, content={"detail": "Request body too large."})
            return await response(scope, receive, send)

        body = await request.body()
        if len(body) > self.settings.request_max_bytes:
            observe_guardrail_event("request_size", "blocked")
            response = JSONResponse(status_code=413, content={"detail": "Request body too large."})
            return await response(scope, request.receive, send)

        body_text = body.decode("utf-8", errors="ignore")
        check = await self.guardrails.check_inbound(self.span_extractor.extract(body_text))
        if check.blocked:
            observe_guardrail_event("inbound", "blocked")
            response = JSONResponse(
                status_code=400,
                content={"detail": "Input rejected by guardrails.", "reasons": check.reasons},
            )
            return await response(scope, request.receive, send)
        observe_guardrail_event("inbound", "allowed")

        async def receive_with_cached_body():
            return {"type": "http.request", "body": body, "more_body": False}

        async def scrub_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                payload = message["body"]
                try:
                    text = payload.decode("utf-8")
                    redacted = self.guardrails.redact_outbound(text)
                    if redacted != text:
                        observe_guardrail_event("outbound", "redacted")
                    message["body"] = redacted.encode("utf-8")
                except Exception as exc:
                    observe_guardrail_event("outbound", "error")
                    logger.warning(
                        "Outbound guardrail redaction failed; leaving body unchanged: %s",
                        exc,
                    )
            await send(message)

        await self.app(scope, receive_with_cached_body, scrub_send)

    def _declared_length_exceeds_limit(self, request: Request) -> bool:
        raw = request.headers.get("content-length")
        if not raw:
            return False
        try:
            return int(raw) > self.settings.request_max_bytes
        except ValueError:
            # A malformed Content-Length is not trustworthy; let the post-read
            # length check decide rather than rejecting (or trusting) it here.
            return False
