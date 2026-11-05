"""Serve both MapleWave agent pages locally on separate ports."""

import http.server
import os
import socketserver
import threading

REVIEW_PORT = 8000   # index.html  — BAR Review Copilot
INTAKE_PORT = 8001   # intake.html — BAR Intake Assistant

# Always serve from the directory this script lives in
os.chdir(os.path.dirname(os.path.abspath(__file__)))

Handler = http.server.SimpleHTTPRequestHandler


def start_server(port: int) -> None:
    """Start a TCPServer on the given port and serve forever."""
    with socketserver.TCPServer(("", port), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    for port in (REVIEW_PORT, INTAKE_PORT):
        t = threading.Thread(target=start_server, args=(port,), daemon=True)
        t.start()

    print(f"BAR Review Copilot  → http://localhost:{REVIEW_PORT}/index.html")
    print(f"BAR Intake Assistant → http://localhost:{INTAKE_PORT}/intake.html")
    print("Press Ctrl+C to stop.")

    threading.Event().wait()  # block main thread until Ctrl+C
