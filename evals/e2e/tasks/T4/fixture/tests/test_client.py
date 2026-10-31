import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fetch import FetchError, get_json


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data.json":
            body = json.dumps({"ok": True, "items": [1, 2, 3]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # silence
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_get_json_ok(server):
    assert get_json(server + "/data.json") == {"ok": True, "items": [1, 2, 3]}


def test_get_json_404(server):
    with pytest.raises(FetchError) as ei:
        get_json(server + "/missing")
    assert ei.value.status == 404
