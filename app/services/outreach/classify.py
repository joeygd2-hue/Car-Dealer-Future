"""Deterministic bilingual classification of prospect replies."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.responder.analysis import is_opt_out
from app.services.responder.language import strip_accents

POSITIVE, MEETING, QUESTION, NOT_INTERESTED = "POSITIVE", "MEETING_REQUEST", "QUESTION", "NOT_INTERESTED"
OPT_OUT, OOO, BOUNCE, SENSITIVE, UNKNOWN = "OPT_OUT", "OUT_OF_OFFICE", "BOUNCE", "SENSITIVE", "UNKNOWN"


def _rx(*p: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{x})" for x in p), re.IGNORECASE)


RULES = [
    (BOUNCE, _rx(r"undeliverable", r"delivery status notification", r"mail delivery (failed|subsystem)", r"returned mail",
                 r"address not found", r"recipient address rejected", r"message not delivered")),
    (OOO, _rx(r"out of (the )?office", r"automatic reply", r"auto-?reply", r"on vacation", r"away until",
              r"fuera de la oficina", r"respuesta automatica", r"de vacaciones")),
    (OPT_OUT, _rx(r"unsubscribe", r"remove me", r"take me off", r"stop (emailing|contacting|sending)", r"do not (contact|email)",
                  r"don'?t (contact|email) (me|us)", r"no (me|nos) (envie|envien|escriba|escriban|contacte|contacten)",
                  r"darme de baja", r"darnos de baja", r"quitenme")),
    (SENSITIVE, _rx(r"lawyer", r"attorney", r"legal action", r"report (you|this)", r"\bspam", r"how did you get (my|this|our)",
                    r"complaint", r"harass", r"abogado", r"demanda", r"denunci", r"como (consiguio|obtuvo)")),
    (NOT_INTERESTED, _rx(r"not interested", r"no thanks", r"no thank you", r"we'?re (good|all set)", r"already (have|use|work with)",
                         r"not (a good fit|for us|at this time)", r"no estamos interesados", r"no me interesa", r"no gracias",
                         r"ya tenemos")),
    (MEETING, _rx(r"\b(schedule|set up|book) (a )?(call|meeting|time)", r"let'?s (talk|chat|meet)", r"\bzoom\b",
                  r"free (on|at|next)", r"available (on|at|next|this)", r"what times", r"send (me )?(some )?times",
                  r"call me", r"agendar", r"una llamada", r"reunion", r"cuando (puede|podemos)")),
    (QUESTION, _rx(r"\?", r"how much", r"pricing", r"\bcost\b", r"what do you", r"how does", r"cuanto", r"precio", r"como funciona")),
    (POSITIVE, _rx(r"\binterested\b", r"sounds (good|great|interesting)", r"tell me more", r"send (more )?info", r"\byes\b",
                   r"\bsure\b", r"me interesa", r"suena bien", r"mas informacion", r"\bclaro\b")),
]


@dataclass
class Classification:
    label: str
    confidence: float
    signals: list[str]

    @property
    def requires_human(self) -> bool:
        return self.label in (SENSITIVE, UNKNOWN) or self.confidence < 0.6


def classify_reply(subject: str, body: str, from_addr: str = "") -> Classification:
    # Only look at the new text, not the quoted original message.
    fresh = re.split(r"\n\s*(?:>|On .+wrote:|El .+escribi[oó]:|-----Original Message-----)", body or "", maxsplit=1)[0]
    text = strip_accents(f"{subject}\n{fresh}".lower())
    if re.search(r"mailer-daemon|postmaster", from_addr or "", re.IGNORECASE):
        return Classification(BOUNCE, 0.99, ["sender is mail system"])
    if is_opt_out(fresh):
        return Classification(OPT_OUT, 0.95, ["opt-out phrase"])
    matched = [(label, rx.search(text)) for label, rx in RULES]
    hits = [(label, m.group(0)) for label, m in matched if m]
    if not hits:
        return Classification(UNKNOWN, 0.3, [])
    label, signal = hits[0]
    # Mixed positive + negative signals are ambiguous → lower confidence so a human reviews.
    labels = {h[0] for h in hits}
    conf = 0.9
    if label in (POSITIVE, MEETING, QUESTION) and NOT_INTERESTED in labels:
        conf = 0.5
    if label == QUESTION and len(fresh) > 600:
        conf = 0.6
    return Classification(label, conf, [s for _, s in hits][:5])
