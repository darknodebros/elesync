"""Verify real redirect handling without sending credentials to external hosts."""

from __future__ import annotations

import ipaddress
import json
import ssl
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from elesync.web import _api_connect_verify


class TestConnectionVerifier(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.location = "/destination"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.requests.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(owner.status if self.path == "/mcp" else 200)
                if self.path == "/mcp" and 300 <= owner.status < 400:
                    self.send_header("Location", owner.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization"), b""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def verify(self, **overrides):
        return _api_connect_verify(
            {"url": self.url, "token": "test-bearer", "insecure": True, **overrides}
        )

    def test_redirects_never_send_a_second_request(self):
        for status in (301, 302, 303, 307, 308):
            for location in (
                "/destination",
                f"http://localhost:{self.server.server_port}/destination",
                f"https://localhost:{self.server.server_port}/destination",
            ):
                with self.subTest(status=status, location=location):
                    self.requests.clear()
                    self.status, self.location = status, location
                    result = self.verify()
                    self.assertFalse(result["verified"])
                    self.assertTrue(result["reachable"])
                    self.assertEqual(result["status"], "redirect_refused")
                    self.assertEqual(len(self.requests), 1)
                    self.assertEqual(self.requests[0][0], "/mcp")

    def test_direct_endpoint_receives_initialize_and_original_token(self):
        result = self.verify()
        self.assertTrue(result["verified"])
        path, authorization, body = self.requests[0]
        self.assertEqual(path, "/mcp")
        self.assertEqual(authorization, "Bearer test-bearer")
        self.assertEqual(json.loads(body)["method"], "initialize")

    def test_direct_endpoint_can_be_checked_without_a_token(self):
        self.assertTrue(self.verify(token="")["verified"])
        self.assertIsNone(self.requests[0][1])

    def test_existing_http_error_results_are_preserved(self):
        for status, expected in ((401, "unauthorized"), (405, "reachable"), (503, "unexpected")):
            with self.subTest(status=status):
                self.status = status
                result = self.verify()
                self.assertFalse(result["verified"])
                self.assertEqual(result["status"], expected)

    def test_http_still_requires_explicit_insecure_option(self):
        with self.assertRaisesRegex(ValueError, "HTTPS is required"):
            self.verify(insecure=False)
        self.assertEqual(self.requests, [])

    def test_https_to_http_redirect_does_not_contact_the_http_target(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            tls = ThreadingHTTPServer(("127.0.0.1", 0), self.server.RequestHandlerClass)
            tls.socket = context.wrap_socket(tls.socket, server_side=True)
            thread = threading.Thread(target=tls.serve_forever, daemon=True)
            thread.start()
            try:
                self.status = 302
                self.location = self.url.replace("/mcp", "/destination")
                build_opener = urllib.request.build_opener
                trusted = ssl.create_default_context(cafile=str(cert_path))

                def trusted_opener(*handlers):
                    return build_opener(*handlers, urllib.request.HTTPSHandler(context=trusted))

                with mock.patch("urllib.request.build_opener", side_effect=trusted_opener):
                    result = self.verify(
                        url=f"https://127.0.0.1:{tls.server_port}/mcp", insecure=False
                    )
                self.assertEqual(result["status"], "redirect_refused")
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.requests[0][0], "/mcp")
            finally:
                tls.shutdown()
                tls.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
