"""One-shot Streamlit screenshot via Chrome DevTools Protocol."""
import base64
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).parent / "screenshots" / "dashboard-tournament.png"
URL = "http://localhost:8501/"
PORT = 9333


def free_port(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


while not free_port(PORT):
    PORT += 1


proc = subprocess.Popen([
    CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
    "--no-first-run", "--no-default-browser-check",
    f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
    "--window-size=1400,3000",
], creationflags=0x08000000)
try:
    # Chrome takes a moment to start the debugging endpoint
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    targets = json.loads(urllib.request.urlopen(
        f"http://localhost:{PORT}/json").read())
    page = [t for t in targets if t["type"] == "page"][0]
    ws_url = page["webSocketDebuggerUrl"]

    from websocket import create_connection
    ws = create_connection(ws_url)
    cmd_id = 0

    def send(method, params=None):
        global cmd_id
        cmd_id += 1
        ws.send(json.dumps({"id": cmd_id, "method": method,
                            "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == cmd_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": URL})
    print(f"Navigated; waiting for Streamlit to paint...")

    # Real wall-clock wait: Streamlit cold start + first render
    time.sleep(25)

    # Poll the DOM for the Tournament tab's main heading
    for attempt in range(20):
        r = send("Runtime.evaluate", {
            "expression":
                "document.body.innerText.includes('Championship probability') "
                "|| document.body.innerText.includes('World Cup 2026')"
        })
        if r["result"].get("value"):
            print(f"Detected Streamlit content (attempt {attempt + 1})")
            break
        time.sleep(2)
    time.sleep(3)  # give charts a chance to finalize

    shot = send("Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True})
    OUT.write_bytes(base64.b64decode(shot["data"]))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    ws.close()
finally:
    proc.terminate()
    proc.wait(timeout=10)
