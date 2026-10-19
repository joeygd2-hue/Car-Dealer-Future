"""Phase 4, step 8: the complete business journey in one run, with SIMULATED providers and synthetic data.

1 prospect discovered -> 2 outreach reviewed and sent (mock) -> 3 reply matched -> 4 meeting booked (mock calendar+Zoom)
-> 5 client created -> 6 inventory imported -> 7 listings generated/approved -> 8 publishing marked manual
-> 9 bilingual approved response -> 10 summary to the right dealership -> 11 revenue + expenses recorded
-> 12 SEO report -> 13 client report reflects the data -> 14 cross-client access is blocked.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from dealerflow.integrations.email import MockEmailProvider
from dealerflow.jobs import run_pending
from dealerflow.models import Client, InboundMessage, Meeting, OutreachMessage, Prospect, SeoSite, Suppression, Template
from dealerflow.services import crm, meetings, outreach, seo_audit
from dealerflow.services.discovery import run_source
from dealerflow.services.inbox import poll_inbox_job
from tests.dealer_helpers import drain
from tests.phase4 import phase2_import_and_approve, user_with_key
from tests.test_phase4_seo_reports import FakeSite


def test_complete_business_journey(client, db, admin):
    _, h_admin = user_with_key(db, "e2e-admin@p4.example", "admin")
    _, h_rep = user_with_key(db, "e2e-rep@p4.example", "rep")

    # 1. Discovery from the SIMULATED fixture source (deduplicated).
    assert run_source(db, "fixture", actor_id=admin.id)["created"] == 7
    db.commit()
    p = db.scalar(select(Prospect).where(Prospect.name == "Brazos Bend Motors (SIMULATED)"))

    # 2. Outreach drafted from an approved template, approved by a person, sent through the mock provider.
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
    msg = outreach.create_draft(db, p, tpl, actor=admin)
    outreach.approve(db, msg, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(OutreachMessage, msg.id).status == "sent"

    # 3. The reply is associated with the correct prospect.
    msg = db.get(OutreachMessage, msg.id)
    MockEmailProvider.inject_reply("sales@brazosbendmotors.example", "Re: " + msg.subject, "Yes, let's book a call next week.",
                                   in_reply_to=msg.rfc_message_id)
    poll_inbox_job(db, {})
    db.commit()
    assert db.scalar(select(InboundMessage)).prospect_id == p.id

    # 4. Meeting booked through the test calendar + Zoom after human approval.
    m = db.scalar(select(Meeting).where(Meeting.prospect_id == p.id))
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Meeting, m.id).status == "booked"

    # 5. Deal won -> client workspace (prospecting stops, address suppressed).
    p = db.get(Prospect, p.id)
    crm.set_stage(db, p, "won", actor=admin, reason="Signed (synthetic)")
    db.commit()
    c = client.post("/api/v1/clients", headers=h_rep, json={"prospect_id": p.id, "contact_email": "owner@brazos.example"}).json()
    cid = c["id"]
    assert c["name"] == p.name and db.scalar(select(Suppression).where(Suppression.reason == "existing_client"))
    other = client.post("/api/v1/clients", headers=h_rep, json={"name": "Other Dealer (TEST)", "contact_email": "o@other.example"}).json()

    # 6-8. Inventory (Phase 2 import), EN/ES listing drafted by the worker, approved by the dealership,
    #      Facebook Marketplace publication marked manual (no API exists for it).
    _, h_dealer = user_with_key(db, "owner@brazos-dealer.example", "client", cid, client_role="dealer_admin")
    run = phase2_import_and_approve(client, h_dealer, cid)
    assert run["counts"]["created"] == 1 and len(run["approved"]) == 1
    pubs = client.get(f"/api/v1/dealers/{cid}/publications", headers=h_dealer).json()
    assert {(x["channel"], x["status"]) for x in pubs} == {("facebook_marketplace", "manual_required")}
    assert client.get(f"/api/v1/dealers/{other['id']}/vehicles", headers=h_dealer).status_code == 404

    # 9-10. Lead to conversation (Phase 3): a Spanish-speaking customer writes through the dealership's website chat
    #       (public widget API), the worker drafts a grounded Spanish reply and records a test-drive request, the
    #       dealership's admin is notified (mock email), replies (SIMULATED delivery) and confirms the appointment.
    key = db.get(Client, cid).public_chat_key
    r = client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-e2e-0001",
                                                       "text": "Hola, ¿puedo hacer una prueba de manejo del Civic el sábado a las 11?"})
    assert r.status_code == 200 and r.json()["ok"]
    drain()  # process the message (draft + appointment request), then deliver the staff notification email
    convs = client.get(f"/api/v1/dealers/{cid}/conversations", headers=h_dealer).json()["items"]
    # The prospect came from the SIMULATED fixture source, so its dealership and conversations are SIMULATED.
    assert len(convs) == 1 and convs[0]["channel"] == "WEBSITE_CHAT" and convs[0]["simulated"]
    conv = client.get(f"/api/v1/dealers/{cid}/conversations/{convs[0]['id']}", headers=h_dealer).json()
    draft = conv["pending_draft"]
    assert conv["language"] == "es" and draft["language"] == "es" and "prueba de manejo" in draft["body"]
    assert conv["summary"]["vehicle_of_interest"].startswith("2019 Honda Civic")
    appts = client.get(f"/api/v1/dealers/{cid}/appointments", headers=h_dealer).json()["items"]
    assert [a["status"] for a in appts] == ["PENDING_CONFIRMATION"]  # never presented to the customer as confirmed
    assert any(m[0].to == "owner@brazos-dealer.example" for m in MockEmailProvider.outbox)  # the right dealership's admin
    assert client.get(f"/api/v1/dealers/{other['id']}/conversations", headers=h_dealer).status_code == 404
    r = client.post(f"/api/v1/dealers/{cid}/conversations/{conv['id']}/reply", headers=h_dealer,
                    json={"body": draft["body"], "draft_id": draft["id"]})
    assert r.status_code == 202, r.text
    assert client.post(f"/api/v1/dealers/{cid}/appointments/{appts[0]['id']}/confirm", headers=h_dealer).json()["status"] == "CONFIRMED"
    drain()
    msgs = client.get(f"/api/v1/dealers/{cid}/conversations/{conv['id']}", headers=h_dealer).json()["messages"]
    outbound = [m for m in msgs if m["direction"] == "OUTBOUND"]
    assert outbound[0]["sender_type"] == "STAFF" and "confirmada" in outbound[1]["body"]  # reply, then the confirmation
    assert {m["delivery_status"] for m in outbound} == {"SIMULATED"}  # (the helper also runs the future reminder jobs)
    assert client.get(f"/api/chat/{key}/messages?visitor_id=visitor-e2e-0001").json()["messages"] == []  # nothing reached the visitor

    # 11. Revenue and expenses.
    today = date.today()
    inv = client.post("/api/v1/invoices", headers=h_admin, json={"client_id": cid, "issue_date": str(today), "due_date": str(today + timedelta(days=15)),
                                                               "issue": True, "lines": [{"kind": "setup", "description": "Setup", "unit_amount_cents": 50000}]}).json()
    client.post("/api/v1/payments", headers=h_admin, json={"client_id": cid, "invoice_id": inv["id"], "amount_cents": 50000, "received_date": str(today)})
    cats = {x["code"]: x["id"] for x in client.get("/api/v1/expense-categories", headers=h_admin).json()}
    client.post("/api/v1/expenses", headers=h_admin, json={"expense_date": str(today), "vendor": "Data API", "category_id": cats["data_sources"],
                                                         "amount_cents": 3200, "business_purpose": "Prospect discovery"})
    fin = client.get("/api/v1/finance/report", headers=h_admin).json()
    assert fin["collected_net_cents"] == 50000 and fin["expenses_cents"] == 3200 and fin["operating_result_cash_cents"] == 46800
    assert fin["meetings_booked"] == 0  # the meeting was SIMULATED: never counted as a real result

    # 12. SEO audit on an authorized site.
    site = client.post("/api/v1/seo/sites", headers=h_rep, json={"client_id": cid, "base_url": "https://alpha.example"}).json()
    client.post(f"/api/v1/seo/sites/{site['id']}/authorize", headers=h_admin, json={"note": "Signed SEO authorization (synthetic)"})
    a = seo_audit.start_audit(db, db.get(SeoSite, site["id"]), None)
    seo_audit.run_audit(db, a, http_client=FakeSite().client(), sleep=lambda s: None)
    db.commit()
    issues = client.get(f"/api/v1/seo/issues?client_id={cid}", headers=h_rep).json()
    assert issues and all(i["evidence"] for i in issues)

    # 13. The client report reflects the underlying data.
    rep = client.post("/api/v1/client-reports", headers=h_rep, json={"client_id": cid, "period_start": str(today.replace(day=1)), "period_end": str(today)}).json()
    data = rep["data"]
    assert data["inventory"]["vehicles_processed"]["value"] == 1 and data["listings"]["generated"]["value"] == 1  # one bilingual listing
    assert data["listings"]["approved"]["value"] == 1 and data["listings"]["manual_publication_required"]["value"] == 1
    assert data["seo"]["issues_open_at_end"]["value"] == len(issues)
    # SIMULATED conversations are never reported as real inquiries or appointments.
    assert data["inquiries"]["received"]["value"] == 0 and data["inquiries"]["simulated_excluded"] == 1
    assert data["appointments"]["test_drive_requests"]["value"] == 0
    client.post(f"/api/v1/client-reports/{rep['id']}/publish", headers=h_rep)

    # 14. Cross-client access is blocked, including direct API requests.
    _, h_client = user_with_key(db, "owner@brazos-portal.example", "client", cid)
    _, h_other = user_with_key(db, "owner@other-portal.example", "client", other["id"])
    assert client.get(f"/api/v1/portal/reports/{rep['id']}", headers=h_client).status_code == 200
    assert client.get(f"/api/v1/portal/reports/{rep['id']}", headers=h_other).status_code == 404
    assert client.get(f"/api/v1/portal/invoices/{inv['id']}", headers=h_other).status_code == 404
    assert client.get("/api/v1/portal/seo-issues", headers=h_other).json() == []
    assert client.get(f"/api/v1/seo/issues?client_id={cid}", headers=h_other).status_code == 403
    assert db.get(Client, cid).status == "onboarding"  # services not activated until the checklist is complete
