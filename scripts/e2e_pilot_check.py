"""End-to-end pilot check of the integrated app: real web server + real worker processes + headless Chromium.

Builds a fresh SQLite database in a temporary directory, runs the real migrations and demo seed, starts
``uvicorn`` and the worker as separate processes, then drives the app the way people and the outside world would:
website-chat visitor (public API), dealership reviewer and agency admin (browser, admin with TOTP MFA),
inventory accuracy (stale vehicle), tenant isolation, worker kill -9 + restart, emergency stop, audit seals.
Synthetic data only; every provider is SIMULATED/sandbox; nothing leaves the machine.

    python scripts/e2e_pilot_check.py [--artifacts DIR]
"""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CHROMIUM = os.environ.get("E2E_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
PASSWORD = "e2e synthetic passphrase"
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout: float = 60, every: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default=None, help="directory for screenshots and process logs")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="df-e2e-"))
    art = Path(args.artifacts) if args.artifacts else tmp
    art.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("DF_")}
    env.update({"DF_ENV": "development", "DF_DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "DF_SECRET_KEY": secrets.token_urlsafe(48),
                "DF_BASE_URL": base, "DF_EMAIL_PROVIDER": "mock", "DF_CALENDAR_PROVIDER": "mock", "DF_ZOOM_PROVIDER": "mock",
                "DF_CALL_PROVIDER": "mock", "DF_REQUIRE_ADMIN_MFA": "true", "DF_LOG_LEVEL": "WARNING", "DF_ATTACHMENTS_DIR": str(tmp / "att"),
                "PYTHONPATH": str(ROOT)})
    os.environ.update(env)

    def run(*cmd: str) -> str:
        out = subprocess.run([PY, "-m", "dealerflow", *cmd], env=env, cwd=ROOT, capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            print(out.stdout, out.stderr)
        return out.stdout

    check("database migrated" in run("migrate"), "fresh database migrated and seeded (alembic upgrade head)")
    seed = run("demo-dealer")
    chat_key = seed.split("/chat/")[1].split()[0] if "/chat/" in seed else ""
    check(bool(chat_key), "demo dealership + inventory imported, website-chat key issued")

    # Users and a second dealership, created through the app's own models (synthetic).
    from sqlalchemy import select

    from dealerflow import mfa
    from dealerflow.db import session_scope, utcnow
    from dealerflow.models import User
    from dealerflow.models_dealer import Client, Vehicle
    from dealerflow.security import hash_password

    totp_secret = mfa.new_secret()
    with session_scope() as db:
        demo = db.scalar(select(Client).where(Client.slug == "demo-fixture-motors"))
        other = Client(name="Other Motors (E2E)", slug="other-motors-e2e", simulated=True)
        db.add(other)
        db.flush()
        pw = hash_password(PASSWORD)
        db.add_all([
            User(email="admin@agency-e2e.example", name="Agency admin", role="admin", password_hash=pw, mfa_enabled=True,
                 mfa_secret=mfa.seal(totp_secret), mfa_recovery_hashes=[]),
            User(email="reviewer@demo-e2e.example", name="Demo reviewer", role="viewer", password_hash=pw, client_id=demo.id,
                 client_role="dealer_reviewer"),
            User(email="admin@other-e2e.example", name="Other admin", role="viewer", password_hash=pw, client_id=other.id,
                 client_role="dealer_admin"),
        ])
        stale = db.scalar(select(Vehicle).where(Vehicle.client_id == demo.id, Vehicle.stock_number == "FX1003"))
        stale.last_seen_at = utcnow() - timedelta(days=5)  # older than the 24 h freshness window
        demo_id, other_id = demo.id, other.id

    logs = {n: open(art / f"{n}.log", "w") for n in ("web", "worker")}
    web = subprocess.Popen([PY, "-m", "uvicorn", "dealerflow.web.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)],
                           env=env, cwd=ROOT, stdout=logs["web"], stderr=subprocess.STDOUT)
    worker_cmd = [PY, "-c", "from dealerflow.worker import main; main(1.0)"]
    worker = subprocess.Popen(worker_cmd, env=env, cwd=ROOT, stdout=logs["worker"], stderr=subprocess.STDOUT)
    try:
        h = wait_for(lambda: _get_json(f"{base}/healthz"), 60)
        check(h and h.get("ok"), "web server healthy (/healthz)")
        check(wait_for(lambda: (_get_json(f"{base}/healthz") or {}).get("worker_alive"), 30), "worker heartbeat visible")

        from dealerflow.models_customer import CustomerConversation, CustomerMessage, ResponseDraft

        def draft_for(visitor: str):
            with session_scope() as db:
                conv = db.scalar(select(CustomerConversation).where(CustomerConversation.external_conversation_id == f"web:{visitor}"))
                if conv is None:
                    return None
                d = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id).order_by(ResponseDraft.id.desc()))
                return (conv.id, d.id, d.body, list(d.escalation_reasons), d.decision) if d else None

        # 1. Website-chat lead intake (public API) -> worker drafts a grounded reply.
        r = httpx.post(f"{base}/api/chat/{chat_key}/messages", json={"visitor_id": "visitor-e2e-0001",
                                                                    "text": "Hi! Is the 2019 Toyota Camry still available and what is the price?"})
        check(r.status_code == 200 and r.json().get("ok"), "website chat message accepted by the public API")
        d = wait_for(lambda: draft_for("visitor-e2e-0001"), 60)
        check(d and "$18,995" in d[2] and d[4] == "DRAFT_ONLY", "worker drafted a reply quoting the verified price, draft-only (Mode A)")
        conv_id = d[0] if d else 0

        # 2. Inventory accuracy: a stale vehicle's price is not quoted and the conversation escalates.
        httpx.post(f"{base}/api/chat/{chat_key}/messages", json={"visitor_id": "visitor-e2e-0002", "text": "How much is the 2018 Ford F-150 XLT?"})
        d2 = wait_for(lambda: draft_for("visitor-e2e-0002"), 60)
        check(d2 and "$26,500" not in d2[2] and "uncertain_inventory" in d2[3], "stale vehicle: no price quoted, escalated as uncertain inventory")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)
            page = browser.new_page()
            # 3. Dealership reviewer: inbox -> conversation -> send the draft (sandbox delivery).
            page.goto(f"{base}/login")
            page.fill("input[name=email]", "reviewer@demo-e2e.example")
            page.fill("input[name=password]", PASSWORD)
            page.click("button:has-text('Log in')")
            page.wait_for_url(f"**/dealers/{demo_id}**")
            check("PILOT MODE" in page.content(), "pilot-mode banner shown")
            page.goto(f"{base}/dealers/{demo_id}/inbox")
            check(page.locator(f"a[href='/dealers/{demo_id}/inbox/{conv_id}']").count() >= 1, "conversation listed in the dealership inbox")
            page.goto(f"{base}/dealers/{demo_id}/inbox/{conv_id}")
            page.screenshot(path=str(art / "conversation.png"), full_page=True)
            check("$18,995" in page.locator("textarea[name=body]").input_value(), "draft pre-filled for staff review")
            page.click("button:has-text('Send')")
            page.wait_for_load_state()

            def outbound_status():
                with session_scope() as db:
                    m = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == conv_id, CustomerMessage.direction == "OUTBOUND"))
                    return m.delivery_status if m and m.delivery_status not in ("QUEUED", "SENDING") else None

            check(wait_for(outbound_status, 60) == "SIMULATED", "staff reply delivered to the SANDBOX only (SIMULATED)")
            polled = httpx.get(f"{base}/api/chat/{chat_key}/messages", params={"visitor_id": "visitor-e2e-0001"}).json()
            check(polled["messages"] == [], "visitor sees nothing: a simulated reply is never presented as delivered")
            # 4. Tenant isolation (another dealership's admin, direct URL).
            other_page = browser.new_page()
            other_page.goto(f"{base}/login")
            other_page.fill("input[name=email]", "admin@other-e2e.example")
            other_page.fill("input[name=password]", PASSWORD)
            other_page.click("button:has-text('Log in')")
            other_page.wait_for_url(f"**/dealers/{other_id}**")
            resp = other_page.goto(f"{base}/dealers/{demo_id}/inbox/{conv_id}")
            check(resp.status == 404, "other dealership's admin gets 404 on this conversation")
            resp = other_page.goto(f"{base}/dealers/{other_id}/inbox/{conv_id}")
            check(resp.status == 404, "...and 404 when using their own workspace id with this conversation id")
            # 5. Agency admin with TOTP MFA -> pilot console.
            admin_page = browser.new_page()
            admin_page.goto(f"{base}/login")
            admin_page.fill("input[name=email]", "admin@agency-e2e.example")
            admin_page.fill("input[name=password]", PASSWORD)
            admin_page.click("button:has-text('Log in')")
            admin_page.wait_for_url("**/login/mfa")
            check(admin_page.goto(f"{base}/admin/pilot").url.endswith("/login/mfa"), "admin pages refused until the second factor is verified")
            admin_page.fill("input[name=code]", mfa.totp(totp_secret))
            admin_page.click("button:has-text('Verify')")
            admin_page.wait_for_load_state()
            admin_page.goto(f"{base}/admin/pilot")
            check("Send ledger" in admin_page.content() and "SIMULATED" in admin_page.content(), "pilot console shows the send ledger")
            admin_page.screenshot(path=str(art / "pilot_console.png"), full_page=True)

            # 6. Worker kill -9 and restart: queued work waits, then completes; nothing is lost.
            worker.send_signal(signal.SIGKILL)
            worker.wait(10)
            httpx.post(f"{base}/api/chat/{chat_key}/messages", json={"visitor_id": "visitor-e2e-0003", "text": "Do you have the Honda CR-V?"})
            time.sleep(4)
            check(draft_for("visitor-e2e-0003") is None, "worker killed (-9): new message waits in the queue")
            worker = subprocess.Popen(worker_cmd, env=env, cwd=ROOT, stdout=logs["worker"], stderr=subprocess.STDOUT)
            check(wait_for(lambda: draft_for("visitor-e2e-0003"), 60), "worker restarted: queued message processed")

            # 7. Emergency stop from the console blocks the next send.
            admin_page.fill("form[action='/admin/pilot/emergency-stop'] input[name=reason]", "e2e drill")
            admin_page.click("form[action='/admin/pilot/emergency-stop'] button")
            admin_page.wait_for_load_state()
            from dealerflow.models import AgentState

            with session_scope() as db:
                stopped = db.get(AgentState, 1).status == "emergency_stopped"
            check(stopped and "EMERGENCY STOP ON" in admin_page.content(), "emergency stop switched on from the console")
            conv3 = draft_for("visitor-e2e-0003")
            page.goto(f"{base}/dealers/{demo_id}/inbox/{conv3[0]}")
            page.click("button:has-text('Send')")
            page.wait_for_load_state()

            def conv3_status():
                with session_scope() as db:
                    m = db.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == conv3[0], CustomerMessage.direction == "OUTBOUND"))
                    return m.delivery_status if m and m.delivery_status not in ("QUEUED", "SENDING") else None

            st = wait_for(conv3_status, 60)
            check(st == "BLOCKED", f"send during emergency stop is BLOCKED (got {st})")
            browser.close()

        out = subprocess.run([PY, "-m", "dealerflow", "audit-verify"], env=env, cwd=ROOT, capture_output=True, text=True)
        check(out.returncode == 0 and "'ok': True" in out.stdout, "audit log: every row's seal verifies")
        web_log = (art / "web.log").read_text() + (art / "worker.log").read_text()
        check("Traceback" not in web_log, "no tracebacks in web or worker logs")
    finally:
        for proc in (web, worker):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; artifacts in {art}")
    return 1 if failed else 0


def _get_json(url: str):
    try:
        r = httpx.get(url, timeout=3)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


if __name__ == "__main__":
    sys.exit(main())
