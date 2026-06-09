"""Save user data to Vercel Blob storage."""
from http.server import BaseHTTPRequestHandler
import urllib.request
import json
import os


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
            username = data.get('username', '').strip().lower()
            lists = data.get('lists')

            if not username or lists is None:
                self._respond(400, {"error": "Missing username or lists"})
                return

            # Save to Vercel Blob as users/{username}.json
            blob_token = os.environ.get('BLOB_READ_WRITE_TOKEN')
            if not blob_token:
                self._respond(500, {"error": "Blob storage not configured"})
                return

            blob_content = json.dumps(lists).encode('utf-8')
            pathname = f"users/{username}.json"

            # Use Vercel Blob PUT API
            req = urllib.request.Request(
                f"https://blob.vercel-storage.com/{pathname}",
                data=blob_content,
                method='PUT',
                headers={
                    'Authorization': f'Bearer {blob_token}',
                    'Content-Type': 'application/json',
                    'x-api-version': '7',
                    'x-content-type': 'application/json',
                }
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())

            self._respond(200, {"ok": True, "url": result.get("url", "")})

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
