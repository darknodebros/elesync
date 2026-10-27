"""Resource bounds exercised with real slow and concurrent HTTP clients."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from elesync import oauth, web
from elesync.store import MemoryStore


class TestHTTPResourceLimits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = web._ServerState()
        state.vault_dir = self.temp.name
        state.store = MemoryStore(self.temp.name)
        self.addCleanup(state.store.close)
        state_patch = mock.patch.object(web, "_state", state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.reading = threading.Event()
        self.entered = threading.Event()
        self.completed = threading.Event()

    def start_server(self, *, workers=2, header_timeout=1.0, body_timeout=0.25, tls=False):
        for name, value in (
            ("MAX_HTTP_WORKERS", workers),
            ("HEADER_TIMEOUT_SECONDS", header_timeout),
            ("BODY_TIMEOUT_SECONDS", body_timeout),
        ):
            patcher = mock.patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        owner = self

        class Handler(web._Handler):
            def setup(self):
                super().setup()
                owner.entered.set()

            def _read_request_bytes(self, **kwargs):
                owner.reading.set()
                return super()._read_request_bytes(**kwargs)

            def do_POST(self):
                if self.path != "/body":
                    return super().do_POST()
                try:
                    body = self._read_request_bytes(max_bytes=web.MAX_IMPORT_REQUEST_BODY_BYTES)
                    self._send_json({"bytes": len(body)})
                except Exception as error:
                    self._fail(error)

        class Server(web.BoundedHTTPServer):
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    owner.completed.set()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        if tls:
            from elesync.auth import setup_mtls

            paths = setup_mtls(Path(self.temp.name) / "certs")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(paths["server_cert"], paths["server_key"])
            self.server.socket = context.wrap_socket(
                self.server.socket, server_side=True, do_handshake_on_connect=False
            )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.address = self.server.server_address

    def request(self, path, body):
        with closing(http.client.HTTPConnection(*self.address, timeout=3)) as connection:
            connection.request("POST", path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())

    def trickle(self, sock):
        stop = threading.Event()

        def write():
            while not stop.wait(0.03):
                try:
                    sock.sendall(b"x")
                except OSError:
                    return

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(stop.set)

    def test_header_trickle_has_a_total_deadline(self):
        self.start_server(header_timeout=0.25)
        with socket.create_connection(self.address, timeout=3) as sock:
            sock.sendall(b"POST /body HTTP/1.1\r\nHost: localhost\r\nX-Slow: ")
            self.trickle(sock)
            self.assertTrue(self.completed.wait(2), "header trickle held a worker open")
            self.assertFalse(self.reading.is_set(), "timed-out headers reached a route")

    def test_body_trickle_returns_408_without_reaching_mcp(self):
        self.start_server()
        with (
            mock.patch.object(web._Handler, "_proxy_mcp") as proxy,
            socket.create_connection(self.address, timeout=3) as sock,
        ):
            sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\n{")
            self.assertTrue(self.reading.wait(1))
            self.trickle(sock)
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 408)
            response.read()
            proxy.assert_not_called()

    def test_saturation_closes_excess_connections_and_recovers(self):
        self.start_server(workers=1, body_timeout=0.5)
        with socket.create_connection(self.address, timeout=3) as slow:
            slow.sendall(b"POST /body HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\nx")
            self.assertTrue(self.reading.wait(1))
            with socket.create_connection(self.address, timeout=3) as excess:
                try:
                    excess.sendall(b"GET /login HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    self.assertEqual(excess.recv(100), b"")
                except ConnectionResetError:
                    pass
            self.assertTrue(self.completed.wait(2), "worker slot was not returned")
        self.assertEqual(self.request("/body", b"complete")[0], 200)

    def test_normal_import_sized_body_is_not_limited_to_small_api_cap(self):
        self.start_server(body_timeout=5)
        payload = b"x" * (web.MAX_REQUEST_BODY_BYTES + 1)
        status, result = self.request("/body", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["bytes"], len(payload))

    def test_silent_tls_client_does_not_block_other_clients(self):
        self.start_server(tls=True, header_timeout=0.5)
        with socket.create_connection(self.address, timeout=3):
            self.assertTrue(self.entered.wait(1))
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with closing(
                http.client.HTTPSConnection(*self.address, context=context, timeout=3)
            ) as client:
                client.request("GET", "/login")
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()

    def test_public_oauth_registration_returns_429_at_rate_limit(self):
        self.start_server()
        body = json.dumps({"redirect_uris": ["https://client.example/cb"]}).encode()
        with mock.patch.object(oauth, "MAX_REGISTRATIONS_PER_MINUTE", 1):
            self.assertEqual(self.request("/oauth/register", body)[0], 201)
            self.assertEqual(self.request("/oauth/register", body)[0], 429)


class TestOAuthRegistrationLimits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)

    def register(self):
        return oauth.register_client(self.vault, "Client", ["https://client.example/cb"])

    def test_client_capacity_prevents_further_file_growth(self):
        with mock.patch.object(oauth, "MAX_REGISTERED_CLIENTS", 2):
            self.register()
            self.register()
            path = self.vault / oauth.OAUTH_FILE
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "capacity"):
                self.register()
            self.assertEqual(path.read_bytes(), before)

    def test_rate_limit_is_atomic_and_recovers_after_window(self):
        def attempt(_):
            try:
                self.register()
                return True
            except oauth.ClientRegistrationRateLimit:
                return False

        with (
            mock.patch.object(oauth, "MAX_REGISTRATIONS_PER_MINUTE", 2),
            mock.patch("elesync.oauth.time.time", return_value=1000),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            self.assertEqual(sum(executor.map(attempt, range(8))), 2)
        with mock.patch("elesync.oauth.time.time", return_value=1061):
            self.register()
        self.assertEqual(len(oauth._load(self.vault)["clients"]), 3)

    def test_redirect_metadata_is_bounded_before_any_write(self):
        invalid = (
            [],
            ["https://client.example/cb"] * (oauth.MAX_REDIRECT_URIS + 1),
            ["x" * (oauth.MAX_REDIRECT_URI_CHARS + 1)],
            [None],
            ["https://client.example/cb#fragment"],
        )
        for uris in invalid:
            with self.subTest(uris=uris[:1]), self.assertRaises(ValueError):
                oauth.register_client(self.vault, "Client", uris)
            self.assertFalse((self.vault / oauth.OAUTH_FILE).exists())


if __name__ == "__main__":
    unittest.main()
