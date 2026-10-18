"""Content guardrails applied to every revision (generated or human-edited).

Errors block approval; warnings must be explicitly acknowledged by the approver.
Checks: misleading urgency, unsupported claims, spec contradictions (year,
mileage, price, drivetrain, transmission, fuel), keyword stuffing, character
limits, required fields, approved disclaimers and language completeness.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.listings.dealer_settings import ListingSettings

URGENCY = [
    r"act now", r"act fast", r"won'?t last", r"hurry", r"limited time", r"today only", r"last chance", r"selling fast",
    r"going fast", r"don'?t miss (out|this)", r"before it'?s gone", r"first come,? first serve", r"once in a lifetime",
    r"priced to sell fast", r"only \d+ left", r"call now", r"must go",
    r"no te lo pierdas", r"no se lo pierda", r"ap[uú]r[ae]te", r"ap[uú]rese", r"por tiempo limitado", r"[uú]ltima oportunidad",
    r"solo hoy", r"se va r[aá]pido", r"no durar[aá]", r"llame ya", r"antes de que se vaya",
]

# (pattern, fact_key that would make it supportable, message)
CLAIMS = [
    (r"(with|includes?|included|under|factory|extended|remaining|full|bumper[- ]to[- ]bumper|powertrain)\s+warrant", None, "warranty claim"),
    (r"warranty (included|available|remaining|coverage)", None, "warranty claim"),
    (r"(con|incluye|incluida|bajo)\s+garant[ií]a", None, "warranty claim"),
    (r"garant[ií]a (incluida|de f[aá]brica|extendida|disponible)", None, "warranty claim"),
    (r"accident[- ]free|no accidents|never (been )?(in an )?accident|sin accidentes|nunca (ha )?chocado", None, "accident-history claim"),
    (r"clean (carfax|autocheck|history)|historial limpio", None, "vehicle-history claim"),
    (r"clean title|t[ií]tulo limpio", "title_clean", "clean-title claim"),
    (r"\b(one|1|single)[- ]owner\b|un (solo )?due[ñn]o", "one_owner", "ownership claim"),
    (r"\bcertified\b|\bcertificado\b|\bcpo\b", "certified", "certification claim"),
    (r"guaranteed (approval|financing)|everyone (is )?approved|no credit check|bad credit (ok|okay|no problem)|"
     r"aprobaci[oó]n garantizada|sin (revisi[oó]n|chequeo) de cr[eé]dito|mal cr[eé]dito (ok|no importa)", None, "financing promise"),
    (r"financing available|we finance|in[- ]house financing|buy here,? pay here|financiamiento disponible|"
     r"financiamos|financiamiento en casa", "financing", "financing claim"),
    (r"\$\s?\d[\d,]*\s*(down|/\s*mo|per month|a month|monthly)|\d+\s*(al mes|mensuales)|enganche de", None, "payment/down-payment claim"),
    (r"inspected|inspeccionado|\d+[- ]point inspection", None, "inspection claim"),
    (r"no (dealer|hidden) fees|sin cargos ocultos", None, "fee claim"),
    (r"(lowest|best) price|mejor precio|precio m[aá]s bajo", None, "superlative price claim"),
]
SUBJECTIVE = [r"like new", r"mint condition", r"perfect condition", r"flawless", r"como nuevo", r"impecable", r"perfectas condiciones"]

DRIVE_WORDS = {"AWD": [r"\bawd\b", r"all[- ]wheel", r"tracci[oó]n integral"],
               "4WD": [r"\b4wd\b", r"\b4x4\b", r"four[- ]wheel", r"doble tracci[oó]n"],
               "FWD": [r"\bfwd\b", r"front[- ]wheel", r"tracci[oó]n delantera"],
               "RWD": [r"\brwd\b", r"rear[- ]wheel", r"tracci[oó]n trasera"]}
TRANS_WORDS = {"manual": [r"\bmanual\b", r"stick shift", r"est[aá]ndar"], "automatic": [r"\bautomatic\b", r"autom[aá]tic[ao]"],
               "cvt": [r"\bcvt\b"]}
FUEL_WORDS = {"electric": [r"\belectric\b(?! (windows|seats|mirrors|locks))", r"\bel[eé]ctrico\b(?! (vidrios|asientos))", r"\bev\b"],
              "hybrid": [r"\bhybrid\b", r"\bh[ií]brido\b"], "diesel": [r"\bdiesel\b", r"\bdi[eé]sel\b"]}
STOP = set("the and with for this that from your have will are our you que con para los las del una este esta por sus como más millas miles "
           "vehicle vehículo listing anuncio dealership concesionario".split())


def _issue(code, severity, message, lang=None, field=None):
    return {"code": code, "severity": severity, "message": message, "lang": lang, "field": field}


def _approved_snippets(facts: dict[str, Any], lang: str, lc_features: list[str] | None = None) -> list[str]:
    d = facts["dealer"]
    snips = [x.get(lang, "") for x in d["disclaimers"]] + [d["financing_statement"].get(lang, ""), d["business_hours"].get(lang, ""),
                                                           d["call_to_action"].get(lang, "")]
    snips += [d["contact"].get(k, "") for k in ("phone", "email", "website", "address")]
    loc = facts["location"]
    snips += [loc.get("address") or "", loc.get("postal") or ""]
    if facts["vehicle"].get("vin"):
        snips.append(facts["vehicle"]["vin"])
    if facts["vehicle"].get("stock_number"):
        snips.append(str(facts["vehicle"]["stock_number"]))
    # Dealer-supplied feature names are verified data; don't spec-check them.
    snips += list(facts["features"]) + list(lc_features or [])
    return [s for s in snips if s]


def _strip(text: str, snippets: list[str]) -> str:
    for s in sorted(snippets, key=len, reverse=True):
        text = text.replace(s, " ")
    return text


def _fact_support(facts: dict[str, Any], key: str | None, lang: str) -> bool:
    v = facts["vehicle"]
    if key == "title_clean":
        return v.get("title_status") == "clean"
    if key == "one_owner":
        return v.get("owner_count") == 1
    if key == "certified":
        return v.get("condition") == "certified_pre_owned"
    if key == "financing":
        return bool(facts["dealer"]["financing_statement"].get(lang))
    return False


def _nums(pattern: str, text: str) -> list[int]:
    out = []
    for m in re.finditer(pattern, text, flags=re.I):
        raw = re.sub(r"[^\d]", "", m.group(1))
        if raw:
            out.append(int(raw))
    return out


def check_language(facts: dict[str, Any], lc: dict[str, Any], lang: str, s: ListingSettings) -> list[dict]:
    issues: list[dict] = []
    v = facts["vehicle"]
    prose = "\n".join([lc.get("title", ""), lc.get("description", ""), lc.get("call_to_action", "")]
                      + [f"{q.get('q', '')} {q.get('a', '')}" for q in lc.get("faq", [])])
    es_feats = [f for f in lc.get("features", []) if lang == "es"]
    checkable = _strip(prose, _approved_snippets(facts, lang, es_feats if len(es_feats) <= len(facts["features"]) else []))
    low = checkable.lower()

    for pat in URGENCY:
        if re.search(pat, low):
            issues.append(_issue("misleading_urgency", "error", f"Urgency language is not allowed: '{re.search(pat, low).group(0)}'", lang))
    for pat, key, label in CLAIMS:
        m = re.search(pat, low)
        if m and not _fact_support(facts, key, lang):
            issues.append(_issue("unsupported_claim", "error", f"Unsupported {label}: '{m.group(0)}' is not in the verified data", lang))
    for pat in SUBJECTIVE:
        m = re.search(pat, low)
        if m:
            issues.append(_issue("subjective_claim", "warning", f"Subjective condition claim '{m.group(0)}'; confirm it is accurate", lang))

    # Spec consistency.
    for y in set(int(x) for x in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", checkable)):
        if v.get("year") and y != v["year"]:
            issues.append(_issue("spec_mismatch", "error", f"Year {y} does not match the vehicle year {v['year']}", lang, "year"))
    for mi in set(_nums(r"(\d{1,3}(?:[,.]\d{3})+|\d+)\s*(?:miles|mi\b|millas)", checkable)):
        if v.get("mileage") is None or mi != v["mileage"]:
            issues.append(_issue("spec_mismatch", "error", f"Mileage {mi:,} does not match the reported mileage "
                                 f"{v['mileage']:,}" if v.get("mileage") is not None else f"Mileage {mi:,} stated but none was supplied", lang, "mileage"))
    for p in set(_nums(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?", checkable)):
        if facts.get("price_usd") is None or p != facts["price_usd"]:
            issues.append(_issue("spec_mismatch", "error", f"Price ${p:,} does not match the listed price", lang, "price"))
    for drive, pats in DRIVE_WORDS.items():
        if any(re.search(p, low) for p in pats) and v.get("drivetrain") != drive:
            issues.append(_issue("spec_mismatch", "error", f"Text mentions {drive} but the drivetrain is {v.get('drivetrain') or 'not supplied'}", lang, "drivetrain"))
    for trans, pats in TRANS_WORDS.items():
        if any(re.search(p, low) for p in pats) and v.get("transmission") != trans:
            issues.append(_issue("spec_mismatch", "error", f"Text mentions a {trans} transmission but data says {v.get('transmission') or 'not supplied'}", lang, "transmission"))
    for fuel, pats in FUEL_WORDS.items():
        if any(re.search(p, low) for p in pats):
            ok = v.get("fuel_type") == fuel or (fuel == "hybrid" and v.get("fuel_type") == "plug_in_hybrid") or \
                 (fuel == "electric" and v.get("fuel_type") == "plug_in_hybrid")
            if not ok:
                issues.append(_issue("spec_mismatch", "error", f"Text mentions {fuel} but fuel type is {v.get('fuel_type') or 'not supplied'}", lang, "fuel_type"))
    for fld in ("make", "model"):
        if v.get(fld) and v[fld].lower() not in lc.get("title", "").lower():
            issues.append(_issue("title_missing_spec", "error", f"Title must include the {fld} ({v[fld]})", lang, "title"))

    # Features must come from the dealer's list.
    if lang == "en":
        allowed = {x.lower() for x in facts["features"]}
        for feat in lc.get("features", []):
            if feat.lower() not in allowed:
                issues.append(_issue("unverified_feature", "error", f"Feature '{feat}' is not in the dealer's feature list", lang, "features"))
    elif len(lc.get("features", [])) > len(facts["features"]):
        issues.append(_issue("unverified_feature", "error", "Spanish feature list has more items than the dealer's list", lang, "features"))

    # Keyword stuffing / formatting.
    words = [w for w in re.findall(r"[a-záéíóúñü]{4,}", (lc.get("description", "")).lower()) if w not in STOP]
    for w, n in Counter(words).items():
        if n > 8:
            issues.append(_issue("keyword_stuffing", "error", f"'{w}' repeated {n} times in the description", lang, "description"))
    tw = [w for w in re.findall(r"\w+", lc.get("title", "").lower()) if len(w) > 2]
    if any(n > 2 for n in Counter(tw).values()):
        issues.append(_issue("keyword_stuffing", "error", "Title repeats words", lang, "title"))
    if len(lc.get("search_terms", [])) > 12:
        issues.append(_issue("keyword_stuffing", "error", "More than 12 search terms", lang, "search_terms"))
    letters = re.findall(r"[A-Za-zÁÉÍÓÚÑ]", lc.get("description", ""))
    caps = sum(1 for c in letters if c.isupper())
    if letters and caps / len(letters) > 0.3:
        issues.append(_issue("all_caps", "warning", "Too much ALL-CAPS text", lang, "description"))
    if prose.count("!") > 3:
        issues.append(_issue("excessive_punctuation", "warning", "Too many exclamation marks", lang))
    if re.search(r"https?://", lc.get("description", "")):
        issues.append(_issue("link_in_description", "warning", "Links in descriptions are rejected by some channels (e.g. Meta catalogs)", lang, "description"))

    # Limits.
    for channel in s.enabled_channels + ["default"]:
        lim = s.character_limits.get(channel) or s.character_limits.get("default")
        if lim and len(lc.get("title", "")) > lim.title:
            issues.append(_issue("too_long", "error", f"Title is {len(lc['title'])} chars; {channel} limit is {lim.title}", lang, "title"))
        if lim and len(lc.get("description", "")) > lim.description:
            issues.append(_issue("too_long", "error", f"Description exceeds {channel} limit of {lim.description}", lang, "description"))
    if not lc.get("title", "").strip() or not lc.get("description", "").strip():
        issues.append(_issue("empty_content", "error", "Title and description are required", lang))

    # Approved disclaimers present verbatim.
    for d in facts["dealer"]["disclaimers"]:
        if d.get(lang) and d[lang] not in lc.get("description", ""):
            issues.append(_issue("missing_disclaimer", "error", f"Approved disclaimer missing: '{d[lang][:60]}'", lang, "description"))
    c = facts["dealer"]["contact"]
    if not (c.get("phone") or c.get("email")):
        issues.append(_issue("missing_contact", "warning", "No dealership phone or email configured for the call to action", lang))
    return issues


def check_content(facts: dict[str, Any], content: dict[str, Any], s: ListingSettings) -> dict[str, list[dict]]:
    issues: list[dict] = []
    for lang in s.languages:
        if lang not in content or not isinstance(content[lang], dict):
            if s.review.require_both_languages:
                issues.append(_issue("missing_language", "error", f"{'Spanish' if lang == 'es' else 'English'} version is missing", lang))
            continue
        issues += check_language(facts, content[lang], lang, s)
    present = {"photos": bool(facts["photos"]), "price": facts.get("price_usd") is not None}
    for f in s.required_fields:
        ok = present[f] if f in present else facts["vehicle"].get(f) not in (None, "")
        if not ok:
            issues.append(_issue("missing_required_field", "error", f"Required field '{f}' is missing from the inventory data", None, f))
    if facts["vehicle"].get("title_status") in ("salvage", "rebuilt", "flood", "lemon_buyback"):
        for lang in s.languages:
            desc = (content.get(lang) or {}).get("description", "").lower()
            if not any(w in desc for w in ("salvage", "rebuilt", "flood", "lemon", "salvamento", "reconstruido", "inundación", "recompra")):
                issues.append(_issue("branded_title_not_disclosed", "error", "Branded title must be disclosed in the description", lang))
    # de-duplicate
    seen, uniq = set(), []
    for i in issues:
        k = (i["code"], i["message"], i["lang"])
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    return {"errors": [i for i in uniq if i["severity"] == "error"], "warnings": [i for i in uniq if i["severity"] == "warning"]}


def warning_key(w: dict) -> str:
    return f"{w['code']}:{w.get('lang') or '-'}:{w.get('field') or '-'}"
