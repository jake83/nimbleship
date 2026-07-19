"""Request-body size limiting for the whole app. Per-field caps (services lists, the
onboarding packet) bound shapes, not bytes - a deeply nested JSON blob passes them all
while costing full parse time, so the byte ceiling lives here, once, ahead of parsing.

A declared Content-Length over the cap is refused with a clean 413 before any body is
read (uvicorn's h11 layer rejects a body exceeding its declaration, so the declaration
is trustworthy). A body without a declaration (chunked) is counted as it streams and
cut off at the cap - that surfaces as an aborted request rather than a tidy 413, which
is acceptable for the only senders that hit it (no browser or httpx JSON call does)."""

from collections.abc import Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Generous headroom over the largest legitimate payload (the ~200k-char onboarding
# packet plus a working definition, far under 1 MB as JSON).
MAX_BODY_BYTES = 2 * 1024 * 1024


class BodyTooLarge(RuntimeError):
    """An undeclared (chunked) body exceeded the cap mid-stream."""


class BodySizeLimitMiddleware:
    def __init__(self, app: AsgiApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = self._content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _too_large(send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body) if isinstance(body, bytes) else 0
                if received > self.max_bytes:
                    raise BodyTooLarge(f"request body exceeded {self.max_bytes} bytes")
            return message

        await self.app(scope, counting_receive, send)

    def _content_length(self, scope: Scope) -> int | None:
        headers = scope.get("headers")
        if not isinstance(headers, list):
            return None
        for name, value in headers:
            if bytes(name).lower() == b"content-length":
                try:
                    return int(bytes(value))
                except ValueError:
                    return None
        return None


async def _too_large(send: Send) -> None:
    body = b'{"detail": "request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
