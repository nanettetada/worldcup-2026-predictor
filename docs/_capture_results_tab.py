"""Click the Results & Accuracy tab in the local Streamlit app and screenshot."""
import base64
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).parent / "screenshots" / "dashboard-results.png"
URL = "http://localhost:8501/"
PORT = 9377


def free(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


while not free(PORT):
    PORT += 1

proc = subprocess.Popen([
    CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
    "--no-first-run", "--no-default-browser-check",
    f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
    "--window-size=1400,2200",
], creationflags=0x08000000)
try:
    for _ in range(30):
        try:
            urllib.request.urlopen(
                f"http://localhost:{PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    page = [t for t in json.loads(urllib.request.urlopen(
        f"http://localhost:{PORT}/json").read()) if t["type"] == "page"][0]
    from websocket import create_connection
    ws = create_connection(page["webSocketDebuggerUrl"])
    cmd_id = 0

    def send(method, params=None):
        global cmd_id
        cmd_id += 1
        ws.send(json.dumps({"id": cmd_id, "method": method,
                            "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == cmd_id:
                return msg["result"]

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": URL})
    time.sleep(20)

    for attempt in range(20):
        r = send("Runtime.evaluate", {
            "expression":
                "(() => {"
                "  const tabs = [...document.querySelectorAll('[role=\"tab\"]')];"
                "  const t = tabs.find(x => /Results/.test(x.innerText));"
                "  if (t) { t.click(); return 'clicked'; }"
                "  return 'no-tab';"
                "})()"
        })
        if r["result"].get("value") == "clicked":
            print(f"Clicked Results tab on attempt {attempt + 1}")
            break
        time.sleep(1)
    time.sleep(6)  # let the tab content paint

    shot = send("Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True})
    OUT.write_bytes(base64.b64decode(shot["data"]))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    ws.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
