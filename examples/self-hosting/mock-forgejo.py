"""Small Forgejo repository-search fixture for the self-hosting smoke profile."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


OWNER = os.environ["FORGE_OWNER"]
REPO = os.environ["FIXTURE_REPO"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if parsed.path != "/api/v1/repos/search":
            self.send_error(404)
            return
        if parse_qs(parsed.query).get("owner") != [OWNER]:
            self.send_error(400, "unexpected owner")
            return
        payload = {
            "data": [{
                "name": "reuser-project",
                "full_name": f"{OWNER}/reuser-project",
                # Keep the API URL on the configured Forgejo origin so the
                # workload's clone URL policy exercises its normal HTTP path.
                # gitconfig rewrites this fixture-only URL to the local bare
                # repository mounted into the smoke container.
                "clone_url": (
                    f"http://forgejo-fixture:8081/{OWNER}/reuser-project.git"
                ),
                "empty": False,
            }]
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
