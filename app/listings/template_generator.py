"""Deterministic bilingual listing generator.

Produces English and Spanish content strictly from verified facts and approved
dealer settings. It is the default generator and the fallback when AI
generation is disabled or unavailable. FAQ answers, feature lists and
disclaimers are always produced here, even when the AI writes the prose.
"""
from __future__ import annotations

import re
from typing import Any

from app.listings import i18n
from app.listings.dealer_settings import ListingSettings


def money(v: int | None) -> str | None:
    return f"${v:,}" if v is not None else None


def miles(v: int | None) -> str | None:
    return f"{v:,}" if v is not None else None


def vehicle_name(f: dict[str, Any], with_trim: bool = True) -> str:
    v = f["vehicle"]
    parts = [str(v["year"]) if v.get("year") else None, v.get("make"), v.get("model"), v.get("trim") if with_trim else None]
    return " ".join(p for p in parts if p)


def render_title(f: dict[str, Any], template: str) -> str:
    v = f["vehicle"]
    values = {k: (str(v.get(k)) if v.get(k) is not None else "") for k in ("year", "make", "model", "trim", "body_style")}
    values["mileage"] = miles(v.get("mileage")) or ""
    values["price"] = money(f.get("price_usd")) or ""
    try:
        title = template.format(**values)
    except (KeyError, IndexError, ValueError):
        title = "{year} {make} {model} {trim}".format(**values)
    return re.sub(r"\s+", " ", title).strip(" -·|")


def translated_features(f: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    en = list(f["features"])
    es, untranslated = [], []
    for feat in en:
        t, ok = i18n.feature_es(feat)
        es.append(t)
        if not ok:
            untranslated.append(feat)
    return en, es, untranslated


def _contact_line(s: dict[str, Any], lang: str) -> str:
    c = s["contact"]
    bits = []
    if c.get("phone"):
        bits.append(("Call or text " if lang == "en" else "Llame o envíe mensaje al ") + c["phone"])
    if c.get("email"):
        bits.append(("email " if lang == "en" else "correo: ") + c["email"])
    return ", ".join(bits)


def build_faq(f: dict[str, Any], lang: str) -> list[dict[str, str]]:
    v, d = f["vehicle"], f["dealer"]
    name = vehicle_name(f)
    phone = d["contact"].get("phone") or ("the dealership" if lang == "en" else "el concesionario")
    hours = d["business_hours"].get(lang)
    loc = f["location"]
    where = ", ".join(x for x in (loc.get("name") or d["contact"].get("dealership_name"), loc.get("address") or d["contact"].get("address"),
                                  loc.get("city"), loc.get("state")) if x)
    fin = d["financing_statement"].get(lang)
    title_status = v.get("title_status")
    if lang == "en":
        faq = [
            {"q": "Is this vehicle still available?",
             "a": f"Inventory changes often, so please contact us at {phone} to confirm the {name} is available before you visit."},
            {"q": "How many miles does it have?",
             "a": f"The dealership reports {miles(v['mileage'])} miles." if v.get("mileage") is not None
             else "Mileage was not provided in this listing. Please ask us for the current odometer reading."},
            {"q": "What is the price?",
             "a": f"The listed price is {money(f['price_usd'])}. Please ask us about taxes, title and registration fees."
             if f.get("price_usd") else "Please contact us for pricing."},
            {"q": "Does it have accident or ownership history information?",
             "a": (f"The dealership reports a {i18n.TITLE['en'].get(title_status, title_status)} title. " if title_status else "")
             + "This listing does not include accident history. Ask us for a vehicle history report if one is available."},
            {"q": "Does it come with a warranty?",
             "a": "Warranty information is not part of this listing. Ask the dealership what coverage, if any, applies."},
            {"q": "Do you offer financing?",
             "a": fin if fin else "Please contact the dealership to ask about payment options."},
            {"q": "Can I see it in person or schedule a test drive?",
             "a": "Yes, contact us to schedule a visit." + (f" We are located at {where}." if where else "") + (f" Hours: {hours}." if hours else "")},
        ]
    else:
        faq = [
            {"q": "¿Este vehículo sigue disponible?",
             "a": f"El inventario cambia con frecuencia; comuníquese al {phone} para confirmar que el {name} está disponible antes de visitarnos."},
            {"q": "¿Cuántas millas tiene?",
             "a": f"El concesionario reporta {miles(v['mileage'])} millas." if v.get("mileage") is not None
             else "El millaje no se incluyó en este anuncio. Pregúntenos por la lectura actual del odómetro."},
            {"q": "¿Cuál es el precio?",
             "a": f"El precio publicado es {money(f['price_usd'])}. Pregúntenos sobre impuestos, título y cuotas de registro."
             if f.get("price_usd") else "Comuníquese con nosotros para conocer el precio."},
            {"q": "¿Tiene información de accidentes o dueños anteriores?",
             "a": (f"El concesionario reporta un título {i18n.TITLE['es'].get(title_status, title_status)}. " if title_status else "")
             + "Este anuncio no incluye historial de accidentes. Pídanos un reporte de historial del vehículo si está disponible."},
            {"q": "¿Tiene garantía?",
             "a": "La información de garantía no forma parte de este anuncio. Pregunte al concesionario qué cobertura aplica, si existe alguna."},
            {"q": "¿Ofrecen financiamiento?",
             "a": fin if fin else "Comuníquese con el concesionario para preguntar sobre opciones de pago."},
            {"q": "¿Puedo verlo en persona o hacer una prueba de manejo?",
             "a": "Sí, contáctenos para agendar una visita." + (f" Estamos en {where}." if where else "") + (f" Horario: {hours}." if hours else "")},
        ]
    return faq


def build_search_terms(f: dict[str, Any], lang: str) -> list[str]:
    v = f["vehicle"]
    city = f["location"].get("city")
    mk, md, yr, tr = v.get("make"), v.get("model"), v.get("year"), v.get("trim")
    terms: list[str] = []
    if lang == "en":
        terms += [f"{yr} {mk} {md}" if yr else None, f"{mk} {md} {tr}" if tr else None, f"used {mk} {md}",
                  f"used {mk} {md} {city}" if city else None, v.get("body_style") and f"used {v['body_style'].lower()}",
                  v.get("fuel_type") in ("hybrid", "electric", "plug_in_hybrid") and f"{i18n.FUEL['en'][v['fuel_type']]} {md}"]
    else:
        body, _ = i18n.body_es(v.get("body_style"))
        terms += [f"{mk} {md} {yr}" if yr else None, f"{mk} {md} usado", f"{mk} {md} usado en {city}" if city else None,
                  body and f"{body} usado", v.get("fuel_type") in ("hybrid", "electric", "plug_in_hybrid") and f"{md} {i18n.FUEL['es'][v['fuel_type']]}"]
    out = []
    for t in terms:
        if t and t not in out:
            out.append(re.sub(r"\s+", " ", t).strip())
    return out[:8]


def _cta(f: dict[str, Any], lang: str) -> str:
    d = f["dealer"]
    custom = d["call_to_action"].get(lang)
    if custom:
        return custom
    contact = _contact_line(d, lang)
    if lang == "en":
        return f"Contact us to ask questions or schedule a test drive{': ' + contact if contact else ''}."
    return f"Contáctenos para resolver sus dudas o agendar una prueba de manejo{': ' + contact if contact else ''}."


OPENERS = {
    "professional": {"en": "This {name} is available at {dealer}.", "es": "Este {name} está disponible en {dealer}."},
    "friendly": {"en": "Take a look at this {name} from {dealer}.", "es": "Conozca este {name} de {dealer}."},
    "straightforward": {"en": "{name} for sale at {dealer}.", "es": "{name} a la venta en {dealer}."},
}


def build_description(f: dict[str, Any], lang: str, s: ListingSettings, features: list[str]) -> str:
    v, d = f["vehicle"], f["dealer"]
    name = vehicle_name(f)
    dealer = d["contact"].get("dealership_name") or f["location"].get("name") or ("our dealership" if lang == "en" else "nuestro concesionario")
    lines = [OPENERS[s.brand_voice][lang].format(name=name, dealer=dealer)]
    specs: list[str] = []
    if lang == "en":
        if v.get("condition"):
            specs.append(f"Condition: {i18n.CONDITION['en'][v['condition']]}")
        if v.get("mileage") is not None:
            specs.append(f"Mileage: {miles(v['mileage'])} miles")
        if f.get("price_usd"):
            specs.append(f"Price: {money(f['price_usd'])}")
        if v.get("exterior_color"):
            specs.append(f"Exterior: {v['exterior_color']}")
        if v.get("interior_color"):
            specs.append(f"Interior: {v['interior_color']}")
        if v.get("body_style"):
            specs.append(f"Body style: {v['body_style']}")
        if v.get("transmission"):
            specs.append(f"Transmission: {i18n.TRANSMISSION['en'][v['transmission']]}")
        if v.get("drivetrain"):
            specs.append(f"Drivetrain: {i18n.DRIVETRAIN['en'][v['drivetrain']]}")
        if v.get("fuel_type"):
            specs.append(f"Fuel: {i18n.FUEL['en'][v['fuel_type']]}")
        if v.get("engine"):
            specs.append(f"Engine: {v['engine']}")
        if v.get("title_status"):
            specs.append(f"Title: {i18n.TITLE['en'][v['title_status']]} (as reported by the dealership)")
        if v.get("vin"):
            specs.append(f"VIN: {v['vin']}")
        if v.get("stock_number"):
            specs.append(f"Stock #: {v['stock_number']}")
        feat_head = "Features listed by the dealership:"
    else:
        if v.get("condition"):
            specs.append(f"Condición: {i18n.CONDITION['es'][v['condition']]}")
        if v.get("mileage") is not None:
            specs.append(f"Millaje: {miles(v['mileage'])} millas")
        if f.get("price_usd"):
            specs.append(f"Precio: {money(f['price_usd'])}")
        if v.get("exterior_color"):
            specs.append(f"Exterior: {i18n.color_es(v['exterior_color'])[0]}")
        if v.get("interior_color"):
            specs.append(f"Interior: {i18n.color_es(v['interior_color'])[0]}")
        if v.get("body_style"):
            specs.append(f"Carrocería: {i18n.body_es(v['body_style'])[0]}")
        if v.get("transmission"):
            specs.append(f"Transmisión: {i18n.TRANSMISSION['es'][v['transmission']]}")
        if v.get("drivetrain"):
            specs.append(f"Tracción: {i18n.DRIVETRAIN['es'][v['drivetrain']]}")
        if v.get("fuel_type"):
            specs.append(f"Combustible: {i18n.FUEL['es'][v['fuel_type']]}")
        if v.get("engine"):
            specs.append(f"Motor: {v['engine']}")
        if v.get("title_status"):
            specs.append(f"Título: {i18n.TITLE['es'][v['title_status']]} (según el concesionario)")
        if v.get("vin"):
            specs.append(f"VIN: {v['vin']}")
        if v.get("stock_number"):
            specs.append(f"No. de inventario: {v['stock_number']}")
        feat_head = "Equipamiento indicado por el concesionario:"
    lines.append("\n".join(f"• {x}" for x in specs))
    if features:
        lines.append(feat_head + "\n" + "\n".join(f"• {x}" for x in features))
    if f.get("dealer_description") and lang == "en":
        lines.append("Dealer notes: " + f["dealer_description"].strip())
    loc = f["location"]
    where = ", ".join(x for x in (loc.get("address") or d["contact"].get("address"), loc.get("city"), loc.get("state")) if x)
    if where:
        lines.append(("Location: " if lang == "en" else "Ubicación: ") + where)
    hours = d["business_hours"].get(lang)
    if hours:
        lines.append(("Hours: " if lang == "en" else "Horario: ") + hours)
    lines.append(_cta(f, lang))
    for disc in d["disclaimers"]:
        if disc.get(lang):
            lines.append(disc[lang])
    return "\n\n".join(lines)


def generate(f: dict[str, Any], s: ListingSettings) -> tuple[dict[str, Any], list[str]]:
    """Return (content, uncertainty_notes)."""
    feats_en, feats_es, untranslated = translated_features(f)
    uncertainty: list[str] = []
    if f["missing"]:
        uncertainty.append("Not supplied by the dealership (left out, not guessed): " + ", ".join(sorted(set(f["missing"]))))
    if untranslated:
        uncertainty.append("Features kept in English in the Spanish version; please review translation: " + ", ".join(untranslated))
    for field in ("exterior_color", "interior_color"):
        val = f["vehicle"].get(field)
        if val and not i18n.color_es(val)[1]:
            uncertainty.append(f"Color '{val}' kept untranslated in Spanish")
    if f["vehicle"].get("body_style") and not i18n.body_es(f["vehicle"]["body_style"])[1]:
        uncertainty.append(f"Body style '{f['vehicle']['body_style']}' kept untranslated in Spanish")
    if f.get("dealer_description"):
        uncertainty.append("Dealer-written notes are included verbatim in English only; verify every claim in them")
    title_en = render_title(f, s.title_template)
    content: dict[str, Any] = {"languages": s.languages, "photos": f["photos"], "price_display": money(f.get("price_usd"))}
    for lang, feats in (("en", feats_en), ("es", feats_es)):
        if lang not in s.languages:
            continue
        title = title_en
        if lang == "es":
            cond = f["vehicle"].get("condition")
            title = title_en + (f" {i18n.CONDITION['es'][cond]}" if cond in ("used", "certified_pre_owned") else "")
        content[lang] = {
            "title": title,
            "description": build_description(f, lang, s, feats),
            "features": feats,
            "search_terms": build_search_terms(f, lang),
            "call_to_action": _cta(f, lang),
            "faq": build_faq(f, lang),
            "disclaimers": [d[lang] for d in f["dealer"]["disclaimers"] if d.get(lang)],
        }
    return content, uncertainty
