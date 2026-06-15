"""Take a screenshot of the deployed Streamlit Cloud app.

Re-uses the same Chrome DevTools Protocol approach as _capture_streamlit.py
but points at the public URL and gives the cloud cold-start more time.
"""
import base64
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).parent / "screenshots" / "live-tournament.png"
URL = "https://tadananette2026worldcup.streamlit.app/"
PORT = 9444


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
    "--window-size=1400,3000",
], creationflags=0x08000000)
try:
    for _ in range(30):
        try:
            urllib.request.urlopen(
                f"http://localhost:{PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    targets = json.loads(urllib.request.urlopen(
        f"http://localhost:{PORT}/json").read())
    page = [t for t in targets if t["type"] == "page"][0]
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
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": URL})
    print("Navigated to live URL; waiting for cloud cold-start...")

    probe = (
        "(() => {"
        "  const t = document.body ? document.body.innerText : '';"
        "  const btns = document.body"
        "    ? [...document.body.querySelectorAll('button')] : [];"
        "  const wake = btns.find(b => /get this app back up/i.test(b.innerText));"
        "  if (wake) { wake.click(); return 'wake-clicked'; }"
        "  if (/Statistical Predictor/.test(t) &&"
        "      (/Championship probability/.test(t)"
        "       || /Tournament/.test(t) || /Group/.test(t))) {"
        "    return 'ready';"
        "  }"
        "  if (/Please wait/.test(t) || /Loading/.test(t)) return 'loading';"
        "  return t ? 'unknown:' + t.slice(0, 60) : 'empty';"
        "})()"
    )

    # Total budget ~10 min: hibernated wake + first paint + chart render.
    deadline = time.time() + 600
    last_state = None
    ready = False
    while time.time() < deadline:
        r = send("Runtime.evaluate", {"expression": probe})
        state = r["result"].get("value") or "?"
        if state != last_state:
            print(f"[{int(time.time() - (deadline - 300))}s] state={state}")
            last_state = state
        if state == "ready":
            ready = True
            break
        time.sleep(3)

    if not ready:
        print("Warning: never reached 'ready'; capturing current state anyway.")
    # Charts and dataframes finish painting a few seconds after text appears.
    time.sleep(6)

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
