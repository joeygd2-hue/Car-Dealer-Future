from __future__ import annotations

from sqlalchemy import select

from app.inventory.importer import run_import
from app.inventory.parsers import parse_csv
from app.models import InventorySource, Listing, Vehicle
from tests.conftest import SAMPLE_CSV, ctx_for


def import_csv(client_id: str, csv_text: str = SAMPLE_CSV, source: InventorySource | None = None, full_snapshot: bool = True,
               source_name: str = "Test CSV"):
    ctx = ctx_for(client_id)
    if source is None:
        source = ctx.db.scalar(select(InventorySource).where(InventorySource.name == source_name))
    if source is None:
        source = InventorySource(client_id=client_id, name=source_name, source_type="csv_upload", full_snapshot=full_snapshot)
        ctx.db.add(source)
        ctx.db.flush()
    else:
        source = ctx.db.get(InventorySource, source.id)
    headers, rows = parse_csv(csv_text)
    run = run_import(ctx, source, headers, rows, trigger="upload")
    ctx.db.commit()
    return ctx, run, source


def configure_dealer(ctx, **overrides):
    from app.listings.dealer_settings import update_settings
    patch = {"contact": {"dealership_name": "Alpha Auto", "phone": "(281) 555-0199", "email": "sales@alpha.test",
                         "website": "", "address": "123 Main St, Sugar Land, TX"},
             "business_hours": {"en": "Mon-Sat 9am-7pm", "es": "Lun-Sáb 9am-7pm"},
             "disclaimers": [{"en": "Price excludes tax, title and license.", "es": "El precio no incluye impuestos, título ni placas."}]}
    patch.update(overrides)
    update_settings(ctx, patch)
    ctx.db.commit()


def vehicle_by_stock(ctx, stock: str) -> Vehicle:
    return ctx.db.scalar(select(Vehicle).where(Vehicle.stock_number == stock))


def listing_for(ctx, vehicle: Vehicle) -> Listing:
    return ctx.db.scalar(select(Listing).where(Listing.vehicle_id == vehicle.id))
