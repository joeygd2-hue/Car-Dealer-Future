"""End-to-end SANDBOX smoke test against a running web server and a separately running worker.

    uv run uvicorn app.main:app --port 8765 &      # terminal 1
    uv run python -m app.worker &                   # terminal 2
    SMOKE_BASE_URL=http://127.0.0.1:8765 uv run python -m scripts.smoke_e2e

Requires the demo seed (scripts.seed_demo). Uses only synthetic data; every send must come back SIMULATED or
BLOCKED (never SENT) because no live authorization exists. Exits non-zero on the first failed check.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid

import httpx
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    Appointment,
    Conversation,
    Dealership,
    Escalation,
    Message,
    ResponseDraft,
    SendAttempt,
)
from app.services import audit

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8765")
PASSWORD = "demo-password-123"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def wait_for(fn, timeout=30.0, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return None


def login(client: httpx.Client, email: str) -> str:
    r = client.post("/login", data={"email": email, "password": PASSWORD})
    page = client.get(r.headers.get("location", "/portal") if r.status_code == 303 else "/portal", follow_redirects=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    return m.group(1) if m else ""


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=15)
    check("web health", c.get("/healthz").json().get("ok") is True)
    check("worker health (separate process heartbeat)", c.get("/healthz/worker").status_code == 200)
    with SessionLocal() as db:
        d = db.scalar(select(Dealership).order_by(Dealership.id))
        key, did = d.public_chat_key, d.id
    run = uuid.uuid4().hex[:6]
    leads = {
        "en": ("Hi, is the 2019 Honda Civic still available? How much is it?", "en"),
        "es": ("Hola, ¿todavía está disponible el Civic? ¿Cuánto cuesta?", "es"),
        "fin": ("Do you offer financing on the F-150? My credit isn't great.", "en"),
        "td": ("I'd like to test drive the Civic Saturday at 11am. I'm Sam, 281-555-0177", "en"),
        "stop": ("STOP", "en"),
        "stale": ("Is the Equinox still available? What's the price?", "en"),
    }
    for tag, (text, _) in leads.items():
        r = c.post(f"/api/chat/{key}/messages", json={"visitor_id": f"smoke-{run}-{tag}", "text": text,
                                                      "client_message_id": f"{run}-{tag}"})
        check(f"chat lead accepted ({tag})", r.status_code == 200 and r.json()["ok"])

    def conv(tag):
        with SessionLocal() as db:
            return db.scalar(select(Conversation).where(Conversation.external_conversation_id == f"web:smoke-{run}-{tag}"))

    def draft(tag):
        cv = conv(tag)
        if not cv:
            return None
        with SessionLocal() as db:
            return db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cv.id))

    d_en = wait_for(lambda: draft("en"))
    check("worker drafted EN reply from verified inventory", bool(d_en) and "$18,995" in d_en.body and d_en.language == "en",
          d_en.body if d_en else "no draft")
    d_es = wait_for(lambda: draft("es"))
    check("worker drafted ES reply", bool(d_es) and d_es.language == "es" and "disponible" in d_es.body,
          d_es.body if d_es else "")
    d_fin = wait_for(lambda: draft("fin"))
    check("financing question escalated, no terms invented", bool(d_fin) and "financing" in d_fin.escalation_reasons
          and "approved" not in d_fin.body.lower(), d_fin.body if d_fin else "")
    with SessionLocal() as db:
        check("escalation record created", db.scalar(select(Escalation).where(Escalation.conversation_id == conv("fin").id)) is not None)
    d_stale = wait_for(lambda: draft("stale"))
    check("stale vehicle: price withheld and escalated", bool(d_stale) and "$16,450" not in d_stale.body
          and "uncertain_inventory" in d_stale.escalation_reasons, d_stale.body if d_stale else "")
    appt = wait_for(lambda: SessionLocal().scalar(select(Appointment).where(Appointment.conversation_id == conv("td").id)))
    check("test drive request recorded, not confirmed", bool(appt) and appt.status == "PENDING_CONFIRMATION")
    check("draft does not promise the time", "isn't confirmed" in wait_for(lambda: draft("td")).body)
    check("STOP opt-out recorded, no reply drafted", wait_for(lambda: conv("stop") and conv("stop").opted_out) and draft("stop") is None)

    # Staff reviews and sends the EN draft through the portal.
    staff = httpx.Client(base_url=BASE, timeout=15)
    login(staff, "manager@sugarland-demo.example")
    page = staff.get(f"/portal/{did}/conversations/{conv('en').id}")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = staff.post(f"/portal/{did}/conversations/{conv('en').id}/send",
                   data={"body": d_en.body, "draft_id": d_en.id, "csrf_token": csrf})
    check("staff send accepted", r.status_code in (200, 303))

    def out_status(tag):
        with SessionLocal() as db:
            m = db.scalar(select(Message).where(Message.conversation_id == conv(tag).id, Message.direction == "OUTBOUND")
                          .order_by(Message.id.desc()))
            return m.delivery_status if m and m.delivery_status not in ("QUEUED", "SENDING") else None

    check("sandbox: staff reply SIMULATED, not delivered", wait_for(lambda: out_status("en")) == "SIMULATED")
    poll = c.get(f"/api/chat/{key}/messages", params={"visitor_id": f"smoke-{run}-en"}).json()["messages"]
    check("visitor did not receive the simulated reply", poll == [])

    # Staff confirms the test drive → confirmation is queued and simulated.
    page = staff.get(f"/portal/{did}/appointments")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    staff.post(f"/portal/{did}/appointments/{appt.id}/confirm", data={"csrf_token": csrf})
    with SessionLocal() as db:
        check("appointment confirmed by staff", db.get(Appointment, appt.id).status == "CONFIRMED")
    check("confirmation message SIMULATED", wait_for(lambda: out_status("td")) == "SIMULATED")

    # Role permissions.
    check("dealer staff refused the admin workspace", staff.get("/admin").status_code == 403)
    check("dealer staff refused the pilot console", staff.get("/admin/pilot").status_code == 403)
    check("anonymous user redirected to login", httpx.get(f"{BASE}/portal/{did}", follow_redirects=False).status_code == 303)

    # Operator: emergency stop blocks a new send; lift it again.
    op = httpx.Client(base_url=BASE, timeout=15)
    login(op, "operator@demo.example")
    page = op.get("/admin")
    csrf_admin = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    op.post("/admin/dealerships", data={"name": f"Second Client {run} (SANDBOX)", "csrf_token": csrf_admin})
    with SessionLocal() as db:
        other = db.scalar(select(Dealership).where(Dealership.name == f"Second Client {run} (SANDBOX)"))
    check("operator created a second client", other is not None)
    check("dealer manager cannot see another client (404)", staff.get(f"/portal/{other.id}").status_code == 404)
    page = op.get("/admin/pilot")
    check("pilot console reachable", page.status_code == 200 and "Send ledger" in page.text)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    op.post("/admin/pilot/emergency-stop", data={"on": 1, "reason": "smoke drill", "csrf_token": csrf})
    check("emergency banner visible", "EMERGENCY STOP ACTIVE" in staff.get(f"/portal/{did}").text)
    page = staff.get(f"/portal/{did}/conversations/{conv('es').id}")
    csrf_s = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    staff.post(f"/portal/{did}/conversations/{conv('es').id}/send", data={"body": d_es.body, "draft_id": d_es.id, "csrf_token": csrf_s})
    check("emergency stop blocked the send", wait_for(lambda: out_status("es")) == "BLOCKED")
    op.post("/admin/pilot/emergency-stop", data={"on": 0, "reason": "smoke drill complete", "csrf_token": csrf})
    with SessionLocal() as db:
        counts = dict(db.execute(select(SendAttempt.status, func.count()).group_by(SendAttempt.status)).all())
        check("ledger has no live SENT attempts", "SENT" not in counts and not db.scalar(
            select(func.count(SendAttempt.id)).where(SendAttempt.live.is_(True))), str(counts))
        integrity = audit.verify_integrity(db)
        check("audit log integrity", integrity["ok"], f"{integrity['checked']} rows")
    check("worker healthy at end of run", c.get("/healthz/worker").status_code == 200)
    print(f"\nALL {len(results)} CHECKS PASSED")


if __name__ == "__main__":
    main()
