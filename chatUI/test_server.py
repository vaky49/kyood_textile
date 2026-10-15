import hashlib
import hmac
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from chatUI.server import Backend, ChatError, Config, make_handler

PHONE = "+911111110001"
REQUEST_ID = str(uuid4())


@contextmanager
def running_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class FakeBackend(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []
    failure = 0

    def log_message(self, *_args):
        return

    def reply(self, status, value):
        content = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.calls.append(
            (self.path, {key.lower(): value for key, value in self.headers.items()}, raw)
        )
        if self.failure:
            self.reply(self.failure, {"detail": "do not expose upstream internals"})
            return
        payload = json.loads(raw)
        duplicate = (
            len(
                [
                    call
                    for call in self.calls
                    if call[0] == self.path
                    and json.loads(call[2])["external_message_id"] == payload["external_message_id"]
                ]
            )
            > 1
        )
        self.reply(
            202,
            {
                "accepted": True,
                "duplicate": duplicate,
                "request_id": None if duplicate else REQUEST_ID,
            },
        )

    def do_GET(self):
        self.calls.append(
            (self.path, {key.lower(): value for key, value in self.headers.items()}, b"")
        )
        self.reply(
            200,
            {
                "id": REQUEST_ID,
                "buyer_phone": PHONE,
                "status": "AWAITING_SUPPLIER_SELECTION",
                "messages": [
                    {
                        "id": "message-1",
                        "direction": "inbound",
                        "message_type": "text",
                        "text": "Need 100 cotton ladies suits. Delivery Jaipur.",
                        "created_at": "2026-10-15T10:00:00Z",
                    },
                    {
                        "id": "message-2",
                        "direction": "outbound",
                        "message_type": "text",
                        "text": "Persisted reply",
                        "created_at": "2026-10-15T10:01:00Z",
                    },
                ],
                "requirement_versions": [{"payload": {"category": "ladies_suit", "quantity": 100}}],
                "approvals": [{"approval_type": "supplier_selection", "status": "pending"}],
            },
        )


class ChatServerTests(unittest.TestCase):
    def setUp(self):
        FakeBackend.calls = []
        FakeBackend.failure = 0
        self.backend_context = running_server(FakeBackend)
        self.backend_url = self.backend_context.__enter__()
        self.addCleanup(self.backend_context.__exit__, None, None, None)
        self.config = Config(self.backend_url, "private-operator-token", "private-app-secret")
        self.backend = Backend(self.config)

    def payload(self):
        return {
            "external_message_id": f"chatui-{uuid4()}",
            "sender_phone": PHONE,
            "profile_name": "Test Buyer",
            "text": "Need 100 cotton ladies suits. Delivery Jaipur.",
            "media_url": "",
        }

    def test_text_uses_existing_webhook_with_exact_body_signature(self):
        result = self.backend.send(self.payload())
        self.assertTrue(result["accepted"])
        self.assertFalse(result["duplicate"])
        path, headers, raw = FakeBackend.calls[0]
        self.assertEqual(path, "/webhooks/whatsapp")
        self.assertEqual(json.loads(raw)["message_type"], "text")
        self.assertIsNone(json.loads(raw)["media_url"])
        self.assertEqual(
            headers["x-hub-signature-256"],
            "sha256=" + hmac.new(b"private-app-secret", raw, hashlib.sha256).hexdigest(),
        )
        self.assertNotIn("x-operator-token", headers)

    def test_image_only_is_forwarded_without_inferred_product_facts(self):
        payload = {**self.payload(), "text": "", "media_url": "data:image/png;base64,YQ=="}
        self.backend.send(payload)
        normalized = json.loads(FakeBackend.calls[0][2])
        self.assertEqual(normalized["message_type"], "image")
        self.assertEqual(normalized["media_url"], payload["media_url"])
        self.assertNotIn("requirement", normalized)

    def test_empty_development_secret_is_signed_too(self):
        backend = Backend(Config(self.backend_url, "private-operator-token"))
        backend.send(self.payload())
        _, headers, raw = FakeBackend.calls[0]
        self.assertEqual(
            headers["x-hub-signature-256"],
            "sha256=" + hmac.new(b"", raw, hashlib.sha256).hexdigest(),
        )

    def test_retry_keeps_external_id_and_returns_duplicate(self):
        payload = self.payload()
        self.backend.send(payload)
        result = self.backend.send(payload)
        self.assertTrue(result["duplicate"])
        self.assertIsNone(result["request_id"])

    def test_history_uses_authenticated_request_api(self):
        result = self.backend.conversation(REQUEST_ID, PHONE)
        path, headers, _ = FakeBackend.calls[0]
        self.assertEqual(path, f"/api/requests/{REQUEST_ID}")
        self.assertEqual(headers["x-operator-token"], "private-operator-token")
        self.assertEqual(result["messages"][1]["text"], "Persisted reply")

    def test_different_buyer_is_not_displayed(self):
        with self.assertRaises(ChatError) as caught:
            self.backend.conversation(REQUEST_ID, "+919999999999")
        self.assertEqual(caught.exception.status, 403)

    def test_invalid_payloads_do_not_reach_backend(self):
        for changes in [
            {"sender_phone": "bad"},
            {"text": "", "media_url": ""},
            {"media_url": "file:///private/image.png"},
            {"text": "a" * 10001},
            {"selected_supplier_ids": ["bypass"]},
        ]:
            with self.subTest(changes=list(changes)), self.assertRaises(ChatError):
                self.backend.send({**self.payload(), **changes})
        self.assertEqual(FakeBackend.calls, [])

    def test_invalid_request_id_does_not_reach_backend(self):
        with self.assertRaises(ChatError):
            self.backend.conversation("../suppliers", PHONE)
        self.assertEqual(FakeBackend.calls, [])

    def test_backend_auth_error_is_explicit_and_sanitized(self):
        FakeBackend.failure = 401
        with self.assertRaises(ChatError) as caught:
            self.backend.send(self.payload())
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("credentials/signature", str(caught.exception))
        self.assertNotIn("upstream internals", str(caught.exception))

    def test_config_rejects_remote_servers_and_hides_secrets(self):
        for url in [
            "https://example.com",
            "http://127.0.0.1:8000/private",
            "http://user:secret@localhost:8000",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Config(url, "secret")
        with self.assertRaises(ValueError):
            Config(self.backend_url, "")
        self.assertNotIn("private-operator-token", repr(self.config))
        self.assertNotIn("private-app-secret", repr(self.config))

    def test_real_http_server_static_send_poll_and_origin_protection(self):
        with running_server(make_handler(self.backend)) as url:
            with urlopen(url) as response:
                self.assertIn(b"Mock WhatsApp conversation", response.read())
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            with urlopen(url + "/chat/config") as response:
                config = response.read()
                self.assertNotIn(b"private-operator-token", config)
                self.assertNotIn(b"private-app-secret", config)
            request = Request(
                url + "/chat/send",
                json.dumps(self.payload()).encode(),
                {"Content-Type": "application/json", "X-ChatUI-Request": "1", "Origin": url},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["request_id"], REQUEST_ID)
            with urlopen(
                url + f"/chat/conversation?request_id={REQUEST_ID}&phone=%2B{PHONE[1:]}"
            ) as response:
                self.assertEqual(json.load(response)["status"], "AWAITING_SUPPLIER_SELECTION")
            for headers in [
                {"Content-Type": "application/json"},
                {
                    "Content-Type": "application/json",
                    "X-ChatUI-Request": "1",
                    "Origin": "https://untrusted.example",
                },
                {
                    "Content-Type": "application/json",
                    "X-ChatUI-Request": "1",
                    "Host": "untrusted.example",
                },
            ]:
                with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url + "/chat/send", b"{}", headers))
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()


if __name__ == "__main__":
    unittest.main()
