import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# =================================================
# LOCAL RUNNER
# Serves the same app.handler on http://localhost:8000 with SQLite storage under <repo>/data.
# Never imports the AWS modules or talks to AWS. Setup: pip install -r requirements.txt
# Run from anywhere: python dev/local.py
# =================================================

# Repo root (this file lives in dev/)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PORT = 8000

# Local mode settings must be in place before the app is imported
os.makedirs(DATA_DIR, exist_ok=True)
os.environ["MOCKGEN_LOCAL"] = "true"
os.environ["MOCKGEN_LOCAL_DIR"] = DATA_DIR
os.environ["SECURE_COOKIE"] = "false"
sys.path.insert(0, os.path.join(ROOT, "src"))

import app  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    # Turn a local HTTP request into an ALB event and write back the handler's response
    def handle_request(self, method: str) -> None:
        """
        Translate between http.server and the ALB event/response format the Lambda expects.

        Arguments:
            method (str): HTTP method
        """
        # Build the ALB-style event
        length = int(self.headers.get("Content-Length") or 0)
        event = {
            "httpMethod": method,
            "path": self.path.split("?")[0],
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length).decode("utf-8") if length else "",
            "isBase64Encoded": False,
        }

        # Run the app and send the response
        result = app.handler(event, None)
        body = result["body"].encode("utf-8")
        self.send_response(result["statusCode"])
        for key, value in result["headers"].items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # GET requests
    def do_GET(self) -> None:
        """
        Handle GET.
        """
        self.handle_request("GET")

    # POST requests
    def do_POST(self) -> None:
        """
        Handle POST.
        """
        self.handle_request("POST")


# Start the server
if __name__ == "__main__":
    print(f"Mock data generator running locally at http://localhost:{PORT}  (data in {DATA_DIR})")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
