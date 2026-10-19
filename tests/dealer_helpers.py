"""Shared helpers for Phase 2 (dealership inventory/listings/publishing) tests. Fixture data only."""

from __future__ import annotations

from sqlalchemy import select, update

from dealerflow.db import session_scope, utcnow
from dealerflow.models import Job, User
from dealerflow.models import Client
from dealerflow.models_dealer import InventorySource, Listing, Vehicle
from dealerflow.security import hash_password
from dealerflow.services.inventory.importer import run_import
from dealerflow.services.inventory.parsers import parse_csv
from dealerflow.tenancy import ClientContext, system_context

PASSWORD = "correct horse battery"

SAMPLE_CSV = """Stock #,VIN,Year,Make,Model,Trim,Body,Miles,Price,Ext Color,Int Color,Trans,Drive,Fuel,Options,Photo URLs,Address,City,State,Zip,Condition
A100,1HGCM82633A004352,2003,Honda,Accord,EX,Sedan,145000,"$5,995",Silver,Gray,Automatic,FWD,Gas,Bluetooth|Sunroof|Heated Seats,https://img.example/a100-1.jpg|https://img.example/a100-2.jpg,123 Main St,Sugar Land,TX,77478,Used
B200,,2019,toyota,camry,SE,Sedan,45000,18995,Black,Black,auto,fwd,gasoline,Backup Camera|Apple CarPlay,https://img.example/b200.jpg,123 Main St,Sugar Land,TX,77478,Used
C300,,2018,Ford,F-150,XLT,Truck,82000,26500,White,Gray,Automatic,4x4,Gasoline,Tow Package|Bed Liner,https://img.example/c300.jpg,123 Main St,Sugar Land,TX,77478,Used
"""


def make_client(name: str = "Alpha Auto") -> int:
    with session_scope() as db:
        c = Client(name=name, city="Sugar Land", state="TX", status="active")
        db.add(c)
        db.flush()
        return c.id


def make_dealer_user(client_id: int, email: str, role: str = "dealer_admin") -> int:
    with session_scope() as db:
        u = User(email=email, name=email.split("@")[0], role="client", password_hash=hash_password(PASSWORD),
                 client_id=client_id, client_role=role)
        db.add(u)
        db.flush()
        return u.id


def make_staff(email: str, role: str = "admin") -> int:
    with session_scope() as db:
        u = User(email=email, name=email.split("@")[0], role=role, password_hash=hash_password(PASSWORD))
        db.add(u)
        db.flush()
        return u.id


def ctx_as(client_id: int, user_id: int | None = None) -> ClientContext:
    """System context, or a context acting as a given user (role rules applied by the services)."""
    ctx = system_context(client_id)
    if user_id is not None:
        user = ctx.db.get(User, user_id)
        ctx.user, ctx.is_staff = user, user.client_id is None
        ctx.role = user.role if user.client_id is None else user.client_role
    return ctx


def import_csv(client_id: int, csv_text: str = SAMPLE_CSV, source_name: str = "Test CSV", full_snapshot: bool = True):
    ctx = system_context(client_id)
    src = ctx.db.scalar(select(InventorySource).where(InventorySource.name == source_name))
    if src is None:
        src = InventorySource(client_id=client_id, name=source_name, source_type="csv_upload", full_snapshot=full_snapshot)
        ctx.db.add(src)
        ctx.db.flush()
    headers, rows = parse_csv(csv_text)
    run = run_import(ctx, src, headers, rows, trigger="upload")
    ctx.db.commit()
    return ctx, run, src


def configure_dealer(ctx: ClientContext, **overrides) -> None:
    from dealerflow.services.listings.dealer_settings import update_settings

    patch = {"contact": {"dealership_name": "Alpha Auto", "phone": "(281) 555-0199", "email": "sales@alpha.example",
                         "website": "", "address": "123 Main St, Sugar Land, TX"},
             "business_hours": {"en": "Mon-Sat 9am-7pm", "es": "Lun-Sáb 9am-7pm"},
             "disclaimers": [{"en": "Price excludes tax, title and license.", "es": "El precio no incluye impuestos, título ni placas."}],
             "review": {"allow_agency_approval": True, "require_approval": True, "require_both_languages": True,
                        "auto_submit_for_approval": True, "max_batch_size": 25}}
    patch.update(overrides)
    update_settings(ctx, patch)
    ctx.db.commit()


def vehicle_by_stock(ctx: ClientContext, stock: str) -> Vehicle:
    return ctx.db.scalar(select(Vehicle).where(Vehicle.stock_number == stock))


def listing_for(ctx: ClientContext, vehicle: Vehicle) -> Listing:
    return ctx.db.scalar(select(Listing).where(Listing.vehicle_id == vehicle.id))


def drain(rounds: int = 10) -> dict:
    """Run due jobs with the real job runner. Pending jobs scheduled in the future (backoff) are made due first,
    which stands in for waiting out the backoff."""
    from dealerflow import handlers  # noqa: F401
    from dealerflow.jobs import run_pending

    total: dict[str, int] = {}
    for _ in range(rounds):
        with session_scope() as db:
            db.execute(update(Job).where(Job.status == "pending").values(run_after=utcnow()))
        counts = run_pending()
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        if not counts:
            break
    return total


def page_csrf(client, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.text.split('name="csrf" value="')[1].split('"')[0]


def dealer_login(client, email: str, password: str = PASSWORD) -> None:
    """Log in a dealership user (they cannot open the staff /settings page that conftest.login uses)."""
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": email, "password": password, "csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
