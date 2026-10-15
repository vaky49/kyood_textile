"""Local-only test chat. No production app imports or third-party dependencies."""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

STATIC_DIR = Path(__file__).parent
MAX_BODY_BYTES = 8 * 1024 * 1024


class ChatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    api_url: str
    operator_token: str = field(repr=False)
    app_secret: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        url = urlsplit(self.api_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1"}
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "CHATUI_API_URL must be a local HTTP origin, e.g. http://127.0.0.1:8000"
            )
        if not self.operator_token.strip():
            raise ValueError("Set CHATUI_OPERATOR_TOKEN to the backend's OPERATOR_API_TOKEN")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Backend:
    def __init__(self, config: Config) -> None:
        self.config = config

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = {"Accept": "application/json"}
        if path.startswith("/api/"):
            headers["X-Operator-Token"] = self.config.operator_token
        if body is not None:
            headers["Content-Type"] = "application/json"
            digest = hmac.new(self.config.app_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={digest}"
        request = Request(self.config.api_url.rstrip("/") + path, body, headers, method=method)
        try:
            with build_opener(NoRedirects()).open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            error.close()
            messages = {
                401: "Backend rejected credentials/signature. Check operator token and app secret.",
                403: "Backend denied access. Check the operator token.",
                404: "Request not found in the backend.",
                422: "Backend rejected the message payload.",
            }
            raise ChatError(
                error.code,
                messages.get(error.code, f"Backend returned HTTP {error.code}; inspect API logs."),
            ) from error
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            raise ChatError(
                502, "Cannot reach backend. Check API health and CHATUI_API_URL."
            ) from error
        except (ValueError, UnicodeError) as error:
            raise ChatError(502, "Backend returned an invalid JSON response.") from error

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"external_message_id", "sender_phone", "profile_name", "text", "media_url"}
        if set(payload) - allowed:
            raise ChatError(400, "Unknown message fields.")
        phone = payload.get("sender_phone")
        if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            raise ChatError(400, "Use an international phone number, e.g. +911111110001.")
        message_id = payload.get("external_message_id")
        if not isinstance(message_id, str) or not re.fullmatch(r"chatui-[0-9a-f-]{36}", message_id):
            raise ChatError(400, "A chatUI message ID is required.")
        name = payload.get("profile_name", "")
        text = payload.get("text", "")
        media = payload.get("media_url", "")
        if not isinstance(name, str) or len(name) > 200:
            raise ChatError(400, "Name must be at most 200 characters.")
        if not isinstance(text, str) or len(text) > 10000:
            raise ChatError(400, "Message must be at most 10,000 characters.")
        if not isinstance(media, str) or (
            media
            and not media.startswith(
                (
                    "https://",
                    "data:image/jpeg;base64,",
                    "data:image/png;base64,",
                    "data:image/webp;base64,",
                )
            )
        ):
            raise ChatError(400, "Use an HTTPS image URL or attach a JPEG, PNG, or WebP image.")
        if not text.strip() and not media:
            raise ChatError(400, "Enter a message or attach an image.")
        normalized = {
            **payload,
            "message_type": "image" if media else "text",
            "media_url": media or None,
        }
        return self.call("POST", "/webhooks/whatsapp", normalized)

    def conversation(self, request_id: str, phone: str) -> dict[str, Any]:
        try:
            normalized_id = str(UUID(request_id))
        except ValueError as error:
            raise ChatError(400, "Enter a valid request UUID.") from error
        detail = self.call("GET", f"/api/requests/{normalized_id}")
        if detail.get("buyer_phone") != phone:
            raise ChatError(403, "This request belongs to a different buyer phone number.")
        return detail


def make_handler(backend: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Request URLs include buyer phone numbers; do not log them.
            return

        def respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, status: int, value: Any) -> None:
            self.respond(status, json.dumps(value).encode(), "application/json")

        def check_local_request(self) -> None:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected_host:
                raise ChatError(403, f"Open http://{expected_host} instead.")
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{expected_host}":
                raise ChatError(403, "Cross-origin requests are not allowed.")
            if self.command == "POST" and self.headers.get("X-ChatUI-Request") != "1":
                raise ChatError(403, "Use the local chat UI to send messages.")

        def do_GET(self) -> None:
            try:
                self.check_local_request()
                url = urlsplit(self.path)
                static = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/style.css": ("style.css", "text/css"),
                    "/app.js": ("app.js", "text/javascript"),
                }
                if url.path in static:
                    filename, content_type = static[url.path]
                    self.respond(200, (STATIC_DIR / filename).read_bytes(), content_type)
                elif url.path == "/chat/config":
                    self.json_response(200, {"api_url": backend.config.api_url})
                elif url.path == "/chat/conversation":
                    query = parse_qs(url.query)
                    self.json_response(
                        200,
                        backend.conversation(
                            query.get("request_id", [""])[0],
                            query.get("phone", [""])[0],
                        ),
                    )
                else:
                    raise ChatError(404, "Not found.")
            except ChatError as error:
                self.json_response(error.status, {"error": str(error)})

        def do_POST(self) -> None:
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise ChatError(400, "Invalid content length.") from error
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ChatError(413, "Message body must be between 1 byte and 8 MB.")
                raw_body = self.rfile.read(length)
                self.check_local_request()
                if self.path != "/chat/send":
                    raise ChatError(404, "Not found.")
                if self.headers.get_content_type() != "application/json":
                    raise ChatError(415, "Expected JSON.")
                try:
                    payload = json.loads(raw_body)
                except (ValueError, UnicodeError) as error:
                    raise ChatError(400, "Invalid JSON.") from error
                if not isinstance(payload, dict):
                    raise ChatError(400, "Expected a message object.")
                self.json_response(202, backend.send(payload))
            except ChatError as error:
                self.json_response(error.status, {"error": str(error)})

    return Handler


def main() -> None:
    try:
        config = Config(
            api_url=os.getenv("CHATUI_API_URL", "http://127.0.0.1:8000"),
            operator_token=os.getenv("CHATUI_OPERATOR_TOKEN", os.getenv("OPERATOR_API_TOKEN", "")),
            app_secret=os.getenv(
                "CHATUI_WHATSAPP_APP_SECRET",
                os.getenv("WHATSAPP_APP_SECRET", ""),
            ),
        )
        port = int(os.getenv("CHATUI_PORT", "7860"))
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Backend(config)))
    except (ValueError, OSError) as error:
        raise SystemExit(f"chatUI startup failed: {error}") from error
    print(f"Test chat: http://127.0.0.1:{server.server_port}")
    print(f"Backend: {config.api_url}")
    print("TEST ONLY: use a separate development database and MESSAGING_PROVIDER=mock.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping test chat.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
