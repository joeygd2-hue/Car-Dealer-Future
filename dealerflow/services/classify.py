"""Rule-based reply classifier (English and Spanish).

Deterministic and explainable: returns the label, a confidence and the matched rules.
Anything ambiguous comes back with low confidence and is escalated to a person.
Opt-out wins over everything else: if a message could be an opt-out it is treated as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LABELS = ("opt_out", "bounce", "auto_reply", "meeting_request", "interested", "question", "not_interested",
          "referral", "unclear")

_R = lambda *pats: [re.compile(p, re.I | re.M) for p in pats]  # noqa: E731

OPT_OUT = _R(r"\bunsubscribe\b", r"\bremove me\b", r"\btake me off\b", r"\bstop (emailing|contacting|sending)\b",
             r"^\s*stop\s*$", r"\bdo not (contact|email)\b", r"\bdon'?t (contact|email) (me|us)\b",
             r"\bopt[- ]?out\b", r"\bno more emails\b", r"\bdarme de baja\b", r"^\s*baja\s*$", r"\bdar de baja\b",
             r"\bno me (contacte|escriba|envíe|envie)\b", r"\bno nos (contacte|escriba)\b", r"\bdejen? de (escribir|enviar)\b",
             r"\bcancelar (la )?suscripci[oó]n\b", r"\bquit(ar|en)me de (la|su) lista\b")
BOUNCE = _R(r"delivery status notification", r"undeliverable", r"mail delivery (failed|subsystem)",
            r"address not found", r"user unknown", r"recipient address rejected", r"mailbox unavailable")
AUTO = _R(r"out of (the )?office", r"automatic reply", r"auto[- ]?reply", r"fuera de la oficina",
          r"respuesta autom[aá]tica", r"i am currently away", r"on vacation")
MEETING = _R(r"\b(schedule|set up|book|arrange|have) (a |an )?(call|meeting|zoom|demo|time to talk)\b",
             r"\bwhen (are|would) you (be )?(free|available)\b", r"\b(let'?s|can we|could we) (talk|meet|chat|hop on)\b",
             r"\bsend (me )?(a |an )?(invite|calendar)\b", r"\bavailable (on|this|next|tomorrow)\b",
             r"\b(agendar|programar) (una )?(llamada|reuni[oó]n|cita)\b", r"\bpodemos (hablar|reunirnos)\b",
             r"\b(cu[aá]ndo|que d[ií]a) (est[aá]|tiene) disponible\b", r"\bzoom\b")
INTERESTED = _R(r"\binterested\b", r"\btell me more\b", r"\bsounds (good|great|interesting)\b", r"\bsend (me )?(more )?info",
                r"\bme interesa\b", r"\bm[aá]s informaci[oó]n\b", r"\bsuena bien\b")
NOT_INTERESTED = _R(r"\bnot interested\b", r"\bno thanks?\b", r"\bno,? thank you\b", r"\bwe('re| are) (all )?set\b",
                    r"\balready have (a|an|someone)\b", r"\bno (me|nos) interesa\b", r"\bno gracias\b")
QUESTION = _R(r"\?", r"\bhow much\b", r"\bwhat does it cost\b", r"\bpricing\b", r"\bprice\b", r"\bcu[aá]nto cuesta\b",
              r"\bprecio\b", r"\bhow does (it|this) work\b", r"\bc[oó]mo funciona\b")
REFERRAL = _R(r"\b(talk|speak|reach out) (to|with) (my|our) (manager|owner|partner|gm)\b", r"\bcontact (him|her|them) at\b",
              r"\bhable con (mi|nuestro) (gerente|due[nñ]o|socio)\b")
NEGATION_NEAR_INTEREST = re.compile(r"\b(not|no|never)\b[^.?!]{0,20}\binterested\b", re.I)
QUOTED = re.compile(r"(^>.*$)|(^On .+wrote:$)|(^El .+escribi[oó]:$)", re.M)


@dataclass
class Classification:
    label: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    secondary: list[str] = field(default_factory=list)


def strip_quoted(body: str) -> str:
    lines = []
    for line in body.splitlines():
        if re.match(r"^(On .+wrote:|El .+escribi[oó]:|-----Original Message-----|From: )", line.strip()):
            break
        if line.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _hits(patterns, text) -> list[str]:
    return [p.pattern for p in patterns if p.search(text)]


def classify(subject: str, body: str, headers: dict[str, str] | None = None) -> Classification:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    text = strip_quoted(body or "")
    full = f"{subject or ''}\n{text}"
    if (h := _hits(OPT_OUT, full)):
        return Classification("opt_out", 0.95, [f"opt-out phrase: {x}" for x in h])
    if headers.get("x-failed-recipients") or "delivery-status" in headers.get("content-type", "") or _hits(BOUNCE, full):
        return Classification("bounce", 0.9, ["bounce/delivery-failure markers"])
    if headers.get("auto-submitted", "no").lower() not in ("no", "") or headers.get("x-autoreply") or _hits(AUTO, full):
        return Classification("auto_reply", 0.85, ["auto-reply header or phrase"])
    found = {
        "meeting_request": _hits(MEETING, full),
        "interested": [] if NEGATION_NEAR_INTEREST.search(full) else _hits(INTERESTED, full),
        "not_interested": _hits(NOT_INTERESTED, full),
        "question": _hits(QUESTION, text),
        "referral": _hits(REFERRAL, full),
    }
    positives = [k for k in ("meeting_request", "interested") if found[k]]
    if found["not_interested"] and positives:
        return Classification("unclear", 0.4, ["mixed positive and negative signals"],
                              secondary=[k for k, v in found.items() if v])
    if found["not_interested"]:
        return Classification("not_interested", 0.85, [f"phrase: {x}" for x in found["not_interested"]])
    if found["meeting_request"]:
        conf = 0.85 if len(found["meeting_request"]) > 1 or found["interested"] else 0.7
        return Classification("meeting_request", conf, [f"phrase: {x}" for x in found["meeting_request"]],
                              secondary=[k for k in ("question", "interested") if found[k]])
    if found["referral"]:
        return Classification("referral", 0.7, [f"phrase: {x}" for x in found["referral"]])
    if found["interested"]:
        return Classification("interested", 0.75, [f"phrase: {x}" for x in found["interested"]],
                              secondary=["question"] if found["question"] else [])
    if found["question"]:
        return Classification("question", 0.6, [f"phrase: {x}" for x in found["question"]])
    return Classification("unclear", 0.3, ["no rule matched"])
