"""Rule-based bilingual message analysis: intents, safety signals, contact details and times.

Deterministic on purpose: it is auditable, testable and never sends message content to a third party.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.services.responder.language import strip_accents

# Intents
GREETING, THANKS = "greeting", "thanks"
AVAILABILITY, PRICE, MILEAGE, FEATURES, VEHICLE_DETAILS = "availability", "price", "mileage", "features", "vehicle_details"
HOURS, LOCATION, TEST_DRIVE, CONTACT_PROVIDED = "hours", "location", "test_drive", "contact_info_provided"
FINANCING, CREDIT, WARRANTY, TRADE_IN, NEGOTIATION = "financing", "credit", "warranty", "trade_in", "negotiation"
CONDITION, COMPLAINT, HUMAN_REQUEST, CANCEL_APPOINTMENT = "condition_history", "complaint", "human_request", "cancel_appointment"
UNKNOWN = "unknown_question"

# Topics that always go to a human.
ESCALATION_INTENTS = {FINANCING, CREDIT, WARRANTY, TRADE_IN, NEGOTIATION, CONDITION, COMPLAINT, HUMAN_REQUEST,
                      CANCEL_APPOINTMENT, UNKNOWN}


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


# Patterns run against accent-stripped lowercase text.
PATTERNS: dict[str, re.Pattern] = {
    GREETING: _rx(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|hola|buenas|buenos dias|buenas tardes|buenas noches)\b"),
    THANKS: _rx(r"\b(thanks|thank you|thx|gracias)\b"),
    AVAILABILITY: _rx(r"\bstill (available|for sale|there|have)\b", r"\bavailable\b", r"\bin stock\b", r"\bis it (sold|gone)\b",
                      r"\bsold yet\b", r"\bdisponible\b", r"\bsigue (a la venta|ahi)\b", r"\btodavia (lo|la) tienen\b",
                      r"\bya se vendio\b", r"\b(lo|la) tienen todavia\b", r"\baun (lo|la) tienen\b"),
    PRICE: _rx(r"\bprice\b", r"\bhow much\b", r"\bcost\b", r"\basking\b", r"\bprecio\b", r"\bcuanto\b", r"\bcuesta\b",
               r"\ben cuanto\b", r"\bvale\b"),
    NEGOTIATION: _rx(r"\bdiscount", r"\blowest\b", r"\bbest price\b", r"\bbest deal\b", r"\bnegotiab", r"\bcome down\b",
                     r"\b(take|do|accept) \$?\d", r"\bout the door\b", r"\botd\b", r"\bcash price\b", r"\bdescuento",
                     r"\brebaja", r"\bmejor precio\b", r"\bnegociable\b", r"\bultimo precio\b", r"\blo menos\b",
                     r"\bme lo deja en\b", r"\bdeal\b"),
    FINANCING: _rx(r"\bfinanc", r"\bmonthly payment", r"\bdown payment", r"\bloan\b", r"\bapr\b", r"\binterest rate",
                   r"\bbuy here pay here\b", r"\bbhph\b", r"\bpayments?\b", r"\bfinanciamiento\b", r"\bfinanciar\b",
                   r"\bpagos\b", r"\benganche\b", r"\bmensualidad", r"\bprestamo\b", r"\bplan de pagos\b"),
    CREDIT: _rx(r"\bcredit\b", r"\bcredit score\b", r"\bitin\b", r"\bcredito\b", r"\bburo\b"),
    WARRANTY: _rx(r"\bwarrant", r"\bguarantee", r"\bservice contract\b", r"\bgarantia\b"),
    TRADE_IN: _rx(r"\btrade[- ]?in\b", r"\btrade (my|in)\b", r"\bmy (current )?car (as|for)\b", r"\bintercambio\b",
                  r"\ba cuenta\b", r"\bdar mi (carro|coche|troca|camioneta)\b", r"\btomar mi (carro|coche|troca)\b"),
    CONDITION: _rx(r"\baccident", r"\bcarfax\b", r"\bautocheck\b", r"\b(clean|salvage|rebuilt|branded) title\b",
                   r"\bflood", r"\bcondition\b", r"\bmechanical\b", r"\bany (problems|issues)\b", r"\bcheck engine\b",
                   r"\bhistory report\b", r"\baccidente", r"\btitulo (limpio|salvage|rebuilt)\b", r"\bchoca", r"\bchoque\b",
                   r"\bcondicion\b", r"\b(problemas|fallas)\b", r"\bhistorial\b", r"\binundad", r"\bmecanic"),
    FEATURES: _rx(r"\bfeatures?\b", r"\bdoes it (have|come with)\b", r"\bbluetooth\b", r"\bbackup camera\b", r"\bsunroof\b",
                  r"\bmoonroof\b", r"\bleather\b", r"\bnavigation\b", r"\bcarplay\b", r"\bandroid auto\b", r"\bthird row\b",
                  r"\b(awd|4x4|4wd)\b", r"\bheated seats\b", r"\bcaracteristicas\b", r"\b(lo|la) tiene\b",
                  r"\btiene (camara|quemacocos|piel|bluetooth|navegacion|tercera fila|asientos)", r"\bcamara\b",
                  r"\bquemacocos\b", r"\bequipo\b", r"\bopciones\b"),
    MILEAGE: _rx(r"\bmileage\b", r"\bhow many miles\b", r"\bmiles (does|on)\b", r"\bmillas\b", r"\bmillaje\b", r"\bkilometraje\b"),
    HOURS: _rx(r"\bhours\b", r"\bwhat time (do|are) you\b", r"\b(are you|you guys) open\b", r"\bwhen (do you|are you) (open|close)",
               r"\bopen (today|tomorrow|on|sunday|saturday)\b", r"\bhorario", r"\babren\b", r"\bcierran\b", r"\ba que hora (abren|cierran)\b",
               r"\bestan abiertos\b"),
    LOCATION: _rx(r"\bwhere are you\b", r"\baddress\b", r"\blocation\b", r"\blocated\b", r"\bdirections\b", r"\bdireccion\b",
                  r"\bubicacion\b", r"\bdonde (estan|se encuentran|queda)\b", r"\bubicados\b"),
    TEST_DRIVE: _rx(r"\btest ?drive\b", r"\bcome (see|look|by|check)\b", r"\bstop by\b", r"\bsee (it|the car|the truck) in person\b",
                    r"\bappointment\b", r"\bschedule\b", r"\bvisit\b", r"\bprueba de manejo\b", r"\bmanejarlo\b", r"\bprobarl[oa]\b",
                    r"\bir a verl[oa]\b", r"\bpasar a verl[oa]\b", r"\bcita\b", r"\bagendar\b", r"\bvisitar"),
    CANCEL_APPOINTMENT: _rx(r"\bcancel (my|the) (appointment|test drive)\b", r"\bcan'?t make it\b", r"\breschedule\b",
                            r"\bcancelar (mi|la) cita\b", r"\bno (voy a )?(poder|puedo) ir\b", r"\bcambiar (mi|la) cita\b"),
    COMPLAINT: _rx(r"\bcomplain", r"\brefund\b", r"\bdispute\b", r"\blawyer\b", r"\battorney\b", r"\bsue\b", r"\bbbb\b",
                   r"\bbroke down\b", r"\byou sold me\b", r"\bqueja\b", r"\breembolso\b", r"\babogado\b", r"\bdemanda",
                   r"\bme vendieron\b", r"\bdevolver (el|la|mi) (carro|coche|troca|dinero)\b"),
    HUMAN_REQUEST: _rx(r"\b(talk|speak) (to|with) (a |an |the )?(real )?(person|human|someone|manager|salesman|salesperson|representative|agent)\b",
                       r"\breal person\b", r"\bhuman\b", r"\bmanager\b", r"\bcall me\b", r"\bare you a (bot|robot)\b",
                       r"\bhablar con (una |un |el |alguien)", r"\bpersona real\b", r"\bgerente\b", r"\bencargado\b",
                       r"\bhumano\b", r"\bllamame\b", r"\bllameme\b", r"\bme pueden llamar\b", r"\beres (un )?robot\b"),
}

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "end", "quit", "revoke", "optout", "cancel",
                    "alto", "parar", "para", "baja", "cancelar", "detener"}
OPT_OUT_PHRASES = _rx(
    r"\bstop (texting|messaging|contacting|sending|emailing|calling) me\b", r"\bunsubscribe\b", r"\bopt (me )?out\b",
    r"\bremove me from\b", r"\b(do not|don'?t) (contact|text|message|email|call) me\b", r"\bleave me alone\b",
    r"\bno more (texts|messages)\b", r"\bno me (contacten|escriban|manden|envien|llamen|molesten)\b",
    r"\bdej(e|en|a) de (escribirme|mandarme|enviarme|contactarme|molestarme|llamarme)\b",
    r"\bya no (me escriban|me manden|quiero (recibir )?mensajes)\b", r"\bno mas mensajes\b",
    r"\bdarme de baja\b", r"\bborrenme\b", r"^\s*(stop|alto)\s*[.!]*\s*$",
)
FRUSTRATION = _rx(r"\bridiculous\b", r"\bwaste of (my )?time\b", r"\bscam", r"\bterrible\b", r"\bworst\b", r"\bangry\b",
                  r"\bfrustrat", r"\bunacceptable\b", r"!!!", r"\bwtf\b", r"\blying\b", r"\blied\b", r"\bnobody (answers|responds)\b",
                  r"\bridicul", r"\bestafa", r"\bperdida de tiempo\b", r"\bmolest[oa]\b", r"\benojad[oa]\b", r"\bfrustrad[oa]\b",
                  r"\binaceptable\b", r"\bmentira", r"\bmintieron\b", r"\bnadie (contesta|responde)\b")

SSN_RX = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")
SSN_WORDS = _rx(r"\bssn\b", r"\bsocial security\b", r"\bseguro social\b")
CARD_RX = re.compile(r"\b(?:\d[ -]?){13,19}\b")
DOB_RX = _rx(r"\b(date of birth|born on|dob|fecha de nacimiento|naci el)\b")
DL_RX = _rx(r"\b(driver'?s? licen[cs]e|license number|licencia de manejo|numero de licencia)\b.{0,20}\d{5,}")
BANK_RX = _rx(r"\b(routing number|account number|bank account|numero de cuenta|cuenta bancaria)\b")

EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
NAME_RX = re.compile(r"\b(?:my name is|this is|i am|i'm|me llamo|mi nombre es|soy)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)?)")
VIN_RX = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
STOCK_RX = re.compile(r"\bstock\s*(?:#|no\.?|number)?\s*([A-Z0-9-]{3,12})\b", re.IGNORECASE)
YEAR_RX = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")

FEATURE_TERMS = {
    "bluetooth": "bluetooth", "backup camera": "backup camera", "camara": "backup camera", "sunroof": "sunroof",
    "moonroof": "sunroof", "quemacocos": "sunroof", "leather": "leather", "piel": "leather", "navigation": "navigation",
    "navegacion": "navigation", "carplay": "carplay", "android auto": "android auto", "third row": "third row",
    "tercera fila": "third row", "awd": "awd", "4x4": "4x4", "4wd": "4x4", "heated seats": "heated seats",
    "asientos calefactables": "heated seats", "remote start": "remote start",
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6,
}


@dataclass
class Analysis:
    language: str
    intents: list[str] = field(default_factory=list)
    opt_out: bool = False
    human_request: bool = False
    frustration: bool = False
    sensitive_data: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    name: str | None = None
    vins: list[str] = field(default_factory=list)
    stock_numbers: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    requested_features: list[str] = field(default_factory=list)
    requested_time: datetime | None = None  # naive local time in dealership timezone
    requested_time_text: str | None = None
    questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["requested_time"] = self.requested_time.isoformat() if self.requested_time else None
        return d


def _norm(text: str) -> str:
    return strip_accents(text.lower())


def is_opt_out(text: str) -> bool:
    norm = _norm(text).strip()
    bare = re.sub(r"[^a-z ]", "", norm).strip()
    return bare in OPT_OUT_KEYWORDS or bool(OPT_OUT_PHRASES.search(norm))


def detect_sensitive(text: str) -> list[str]:
    found = []
    if "[REDACTED-SSN]" in text:
        found.append("ssn")
    if "[REDACTED-CARD]" in text:
        found.append("payment_card")
    if "ssn" not in found and SSN_RX.search(text) or (SSN_WORDS.search(_norm(text)) and re.search(r"\d{4}", text)):
        found.append("ssn")
    for m in CARD_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("payment_card")
            break
    norm = _norm(text)
    if DOB_RX.search(norm):
        found.append("date_of_birth")
    if DL_RX.search(norm):
        found.append("drivers_license")
    if BANK_RX.search(norm):
        found.append("bank_account")
    return found


def redact(text: str) -> str:
    """Mask SSNs and payment card numbers before storage."""
    text = SSN_RX.sub("[REDACTED-SSN]", text)

    def _card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        return "[REDACTED-CARD]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group()

    return CARD_RX.sub(_card, text)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def parse_requested_time(text: str, now_local: datetime) -> tuple[datetime | None, str | None]:
    """Parse simple day + time expressions ('Saturday at 10am', 'mañana a las 3'). Returns local naive datetime."""
    norm = _norm(text)
    day: date | None = None
    if re.search(r"\b(tomorrow|tmrw)\b", norm) or re.search(r"(?<!la )(?<!en la )(?<!de la )\bmanana\b(?! en la| por la)", norm):
        day = (now_local + timedelta(days=1)).date()
    elif re.search(r"\b(today|hoy)\b", norm):
        day = now_local.date()
    else:
        for word, wd in WEEKDAYS.items():
            if re.search(rf"\b{word}\b", norm):
                delta = (wd - now_local.weekday()) % 7
                delta = 7 if delta == 0 else delta
                day = (now_local + timedelta(days=delta)).date()
                break
    m = re.search(r"\b(?:at|a las|alas|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.?m\.?|p\.?m\.?)?", norm) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", norm)
    if day is None or m is None:
        return None, None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if hour > 23 or minute > 59:
        return None, None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and 1 <= hour <= 7:
        hour += 12  # "at 3" during dealership hours means 3 PM
    if "de la tarde" in norm and hour < 12:
        hour += 12
    return datetime.combine(day, time(hour, minute)), m.group(0).strip()


def analyze(text: str, language: str, now_local: datetime | None = None) -> Analysis:
    norm = _norm(text)
    a = Analysis(language=language)
    a.opt_out = is_opt_out(text)
    for intent, rx in PATTERNS.items():
        if rx.search(norm):
            a.intents.append(intent)
    # "Stop by" is a visit, not an opt-out; "cancel" alone is an opt-out keyword, "cancel my appointment" is not.
    if CANCEL_APPOINTMENT in a.intents:
        a.opt_out = a.opt_out and not re.search(r"cita|appointment|test drive", norm)
    if NEGOTIATION in a.intents and PRICE not in a.intents and re.search(r"\$\s?\d", text):
        a.intents.append(PRICE)
    a.human_request = HUMAN_REQUEST in a.intents
    a.frustration = bool(FRUSTRATION.search(norm)) or _shouting(text)
    a.sensitive_data = detect_sensitive(text)
    a.emails = EMAIL_RX.findall(text)
    a.phones = ["".join(m.groups()) for m in PHONE_RX.finditer(text) if not a.sensitive_data]
    nm = NAME_RX.search(text)
    a.name = nm.group(1) if nm else None
    a.vins = [v for v in VIN_RX.findall(text.upper()) if re.search(r"\d", v) and re.search(r"[A-Z]", v)]
    a.stock_numbers = [s.upper() for s in STOCK_RX.findall(text)]
    a.years = [int(y) for y in YEAR_RX.findall(text)]
    a.requested_features = sorted({canon for term, canon in FEATURE_TERMS.items() if re.search(rf"\b{re.escape(term)}\b", norm)})
    if a.emails or a.phones or a.name:
        a.intents.append(CONTACT_PROVIDED)
    if now_local is not None:
        a.requested_time, a.requested_time_text = parse_requested_time(text, now_local)
        if a.requested_time and TEST_DRIVE not in a.intents and CANCEL_APPOINTMENT not in a.intents:
            a.intents.append(TEST_DRIVE)
    a.questions = [q.strip() for q in re.split(r"(?<=[?¿.!])\s+", text) if "?" in q and q.strip()]
    substantive = [i for i in a.intents if i not in (GREETING, THANKS, CONTACT_PROVIDED)]
    if not substantive and not a.opt_out and (a.questions or len(norm.split()) >= 6):
        a.intents.append(UNKNOWN)
    return a


def _shouting(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 20 and sum(c.isupper() for c in letters) / len(letters) > 0.8
