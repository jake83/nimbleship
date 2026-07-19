"""App-wide request-body byte ceiling: per-field caps bound shapes, not bytes, so an
oversized payload is refused here, once, before parsing - always as a clean 413. A
declared Content-Length over the cap is refused before any body is read; every other
body is buffered up to the cap ahead of the app, so the cap holds without trusting
the declaration and its tripping cannot reach the framework's body-parsing layer
(which would misreport it as a 400)."""

from collections.abc import Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Strictly above the legacy WMS edge's own 5 MB cap (legacy/router.py) - a
# relationship test pins the ordering.
MAX_BODY_BYTES = 6 * 1024 * 1024


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
        await self._buffered(scope, receive, send)

    async def _buffered(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Counts every body for real - a declared length is never trusted. Every
        route reads its body whole anyway, so buffering adds no new cost."""
        chunks: list[bytes] = []
        received = 0
        interrupted: Message | None = None
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                # A disconnect ends the body incomplete: replay only the disconnect,
                # never the truncated chunks dressed up as a whole body.
                interrupted = message
                break
            body = message.get("body", b"")
            if isinstance(body, bytes):
                received += len(body)
                if received > self.max_bytes:
                    await _too_large(send)
                    return
                chunks.append(body)
            if not message.get("more_body"):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if interrupted is not None:
                return interrupted
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)

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
