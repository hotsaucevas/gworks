"""Load user data from Vercel Blob storage."""
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error
import json
import os
from urllib.parse import parse_qs, urlparse


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        username = params.get('username', [''])[0].strip().lower()

        if not username:
            self._respond(400, {"error": "Missing username"})
            return

        blob_token = os.environ.get('BLOB_READ_WRITE_TOKEN')
        if not blob_token:
            self._respond(500, {"error": "Blob storage not configured"})
            return

        try:
            # List blobs to find the user's file URL
            pathname = f"users/{username}.json"
            list_url = f"https://blob.vercel-storage.com?prefix={pathname}"
            req = urllib.request.Request(
                list_url,
                headers={
                    'Authorization': f'Bearer {blob_token}',
                    'x-api-version': '7',
                }
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())

            blobs = result.get('blobs', [])
            if not blobs:
                self._respond(404, {"error": "User not found", "lists": {}})
                return

            # Download the blob content
            download_url = blobs[0].get('downloadUrl') or blobs[0].get('url')
            dl_req = urllib.request.Request(
                download_url,
                headers={
                    'Authorization': f'Bearer {blob_token}',
                }
            )
            dl_resp = urllib.request.urlopen(dl_req, timeout=10)
            lists = json.loads(dl_resp.read())

            self._respond(200, {"lists": lists})

        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._respond(404, {"error": "User not found", "lists": {}})
            else:
                self._respond(e.code, {"error": f"Blob error: {e.code}"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
