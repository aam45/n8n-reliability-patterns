"""End-to-end check of the three workflows in a throwaway n8n container.

Starts a local stub (fake LLM, alert and handover receiver), imports and publishes the workflows
in n8nio/n8n, sends real HTTP requests and checks the answers and what arrived at the stub.
Needs only Docker and Python 3 (standard library). Usage: python tests/run_tests.py
"""
import hashlib
import hmac
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "n8nio/n8n:2.44.6@sha256:d02752c6ab446ae9db8deec2f9cc887c378f77cdcad31650814df936b7386dfb"
CONTAINER = "n8n-reliability-patterns-test"
N8N_PORT = 5690
N8N = f"http://localhost:{N8N_PORT}"
SECRET = "test-secret-0123456789-0123456789-abcdef"
WORKFLOW_IDS = ["rlpErrorAlert001", "rlpSignedHook001", "rlpFailingDemo01", "rlpClassifyApr01"]

received = {"alert": [], "handover": [], "llm_auth": []}


class Stub(BaseHTTPRequestHandler):
    """Plays the LLM (OpenAI-compatible) and the systems the workflows send to."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/v1/chat/completions":
            received["llm_auth"].append(self.headers.get("Authorization"))
            text = body["messages"][-1]["content"]
            if "Rechnung" in text:
                content = json.dumps({"category": "rechnung", "confidence": 0.95})
            elif "kaputt" in text:
                content = "Das ist kein JSON."
            elif "erfinde" in text:
                content = json.dumps({"category": "urlaub", "confidence": 0.99})
            else:
                content = json.dumps({"category": "sonstiges", "confidence": 0.4})
            self._send({"choices": [{"message": {"role": "assistant", "content": content}}]})
        elif self.path in ("/alert", "/handover"):
            received[self.path[1:]].append(body)
            self._send({"ok": True})
        else:
            self._send({"error": "unknown path"}, 404)

    def _send(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def signed_post(path, payload, secret=SECRET):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return call("POST", N8N + path, raw, {"Content-Type": "application/json", "X-Signature": sig})


def json_post(path, payload):
    return call("POST", N8N + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def wait_for(what, check, seconds=30):
    """Poll until check() is true; the workflows answer first and work on afterwards."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.5)
    print(f"  timeout waiting for {what}")
    return False


failures = []


def expect(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok:
        failures.append(name)


def start_n8n(stub_port):
    stub = f"http://host.docker.internal:{stub_port}"
    publish = " && ".join(f"n8n publish:workflow --id={i}" for i in WORKFLOW_IDS)
    setup = ("n8n import:credentials --input=/fixtures/credentials.json"
             " && n8n import:workflow --separate --input=/workflows"
             f" && {publish} && n8n start")
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        "-p", f"127.0.0.1:{N8N_PORT}:5678",
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{ROOT / 'workflows'}:/workflows:ro",
        "-v", f"{ROOT / 'tests' / 'fixtures'}:/fixtures:ro",
        "-e", f"WEBHOOK_URL={N8N}/",
        "-e", "N8N_BLOCK_ENV_ACCESS_IN_NODE=false",
        "-e", "NODE_FUNCTION_ALLOW_BUILTIN=crypto",
        "-e", "N8N_DIAGNOSTICS_ENABLED=false",
        "-e", "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
        "-e", f"WEBHOOK_SECRET={SECRET}",
        "-e", f"ALERT_WEBHOOK_URL={stub}/alert",
        "-e", f"HANDOVER_URL={stub}/handover",
        "-e", f"LLM_BASE_URL={stub}/v1",
        "-e", "LLM_MODEL=stub-model",
        "-e", "CONFIDENCE_THRESHOLD=0.8",
        "--entrypoint", "sh", IMAGE, "-c", setup,
    ], check=True, capture_output=True)

    def ready():
        try:
            return call("GET", N8N + "/healthz")[0] == 200
        except OSError:
            return False

    if not wait_for("n8n to start", ready, seconds=180):
        print(subprocess.run(["docker", "logs", "--tail", "60", CONTAINER],
                             capture_output=True, text=True).stdout)
        raise SystemExit("n8n did not start")
    # /healthz answers before the published workflows have registered their webhooks.
    wait_for("webhooks", lambda: call("POST", N8N + "/webhook/signed-event", b"{}",
                                      {"Content-Type": "application/json"})[0] != 404, seconds=60)


def main():
    stub_port = free_port()
    server = ThreadingHTTPServer(("0.0.0.0", stub_port), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        start_n8n(stub_port)

        print("\n01 Signierter Webhook mit Duplikat-Schutz")
        code, body = signed_post("/webhook/signed-event", {"id": "evt-1", "total": 42})
        expect("signed event is accepted", code == 200 and body.get("status") == "accepted", (code, body))
        code, body = signed_post("/webhook/signed-event", {"id": "evt-1", "total": 42})
        expect("same id again is reported as duplicate", code == 200 and body.get("status") == "duplicate", (code, body))
        code, body = signed_post("/webhook/signed-event", {"id": "evt-2"}, secret="wrong-secret-0123456789-0123456789")
        expect("wrong signature gets 401", code == 401 and body.get("status") == "invalid_signature", (code, body))
        code, body = call("POST", N8N + "/webhook/signed-event", b'{"id":"evt-3"}', {"Content-Type": "application/json"})
        expect("missing signature gets 401", code == 401, (code, body))
        code, body = signed_post("/webhook/signed-event", {"total": 1})
        expect("event without id gets 400", code == 400 and body.get("status") == "missing_id", (code, body))

        print("\n02 Fehler-Workflow mit Alarm")
        call("GET", N8N + "/webhook/fail-demo")
        got = wait_for("alert", lambda: len(received["alert"]) > 0)
        alert = received["alert"][0] if got else {}
        expect("failing workflow triggers an alert", got, received["alert"])
        expect("alert names workflow, node and error",
               all(s in alert.get("text", "") for s in ("02b Demo", "Zielsystem aufrufen", "Zielsystem nicht erreichbar")),
               alert)

        print("\n03 KI-Einordnung mit Freigabe")
        code, body = json_post("/webhook/classify", {"id": "msg-1", "text": "Anbei die Rechnung Nr. 4711"})
        expect("sure answer is decided automatically",
               code == 200 and body.get("decision") == "auto" and body.get("category") == "rechnung", (code, body))
        expect("LLM is called with the stored credential",
               received["llm_auth"][:1] == ["Bearer test-key-only-for-the-local-stub"], received["llm_auth"])
        got = wait_for("handover msg-1", lambda: any(h["id"] == "msg-1" for h in received["handover"]))
        expect("auto decision is handed over as 'model'",
               got and any(h == {"id": "msg-1", "category": "rechnung", "decidedBy": "model", "confidence": 0.95}
                           for h in received["handover"]), received["handover"])

        code, body = json_post("/webhook/classify", {"id": "msg-2", "text": "Hallo, kurze Frage zu Ihren Zeiten"})
        expect("unsure answer goes to review",
               code == 200 and body.get("decision") == "review" and "approveUrl" in body, (code, body))
        expect("nothing is handed over before approval",
               not any(h["id"] == "msg-2" for h in received["handover"]), received["handover"])
        approve = body.get("approveUrl", "").replace("category=sonstiges", "category=bestellung")
        code, _ = call("GET", approve)
        expect("approval link works", code == 200, code)
        got = wait_for("handover msg-2", lambda: any(h["id"] == "msg-2" for h in received["handover"]))
        expect("approved message is handed over with the person's category",
               got and any(h["id"] == "msg-2" and h["category"] == "bestellung" and h["decidedBy"] == "human"
                           for h in received["handover"]), received["handover"])

        code, body = json_post("/webhook/classify", {"id": "msg-3", "text": "Antwort kaputt"})
        expect("answer that is not JSON goes to review",
               code == 200 and body.get("decision") == "review" and body.get("suggestion") is None, (code, body))
        code, body = json_post("/webhook/classify", {"id": "msg-4", "text": "erfinde eine Kategorie"})
        expect("category outside the schema goes to review",
               code == 200 and body.get("decision") == "review" and body.get("reason") == "Antwort ausserhalb des Schemas",
               (code, body))
    finally:
        if failures:
            print(subprocess.run(["docker", "logs", "--tail", "40", CONTAINER], capture_output=True, text=True).stdout)
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        server.shutdown()

    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
