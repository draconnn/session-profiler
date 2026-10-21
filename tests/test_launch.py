"""The report server answers only requests addressed to 127.0.0.1 or localhost."""
from __future__ import annotations

import http.client
import sys
import threading
from http.server import ThreadingHTTPServer

from support import REPO, IsolatedTest

sys.path.insert(0, str(REPO / "assets/profiler"))
import launch  # noqa: E402


class HostCheckTest(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), launch.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def status(self, method: str, host: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, "/index.html", headers={"Host": host})
        return connection.getresponse().status

    def test_get_and_head_check_the_host(self) -> None:
        port = self.server.server_port
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(self.status(method, f"127.0.0.1:{port}"), 200)
                self.assertEqual(self.status(method, f"localhost:{port}"), 200)
                self.assertEqual(self.status(method, "attacker.example"), 403)
