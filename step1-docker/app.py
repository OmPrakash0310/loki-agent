# A tiny web server. It answers when you visit http://localhost:8000
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # socket.gethostname() shows WHICH box (container) answered you
        msg = f"Hello from inside Docker! I am running in container: {socket.gethostname()}\n"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(msg.encode())
        print(f"Got a request for {self.path}", flush=True)  # this becomes a LOG line

print("Server starting on port 8000...", flush=True)
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
