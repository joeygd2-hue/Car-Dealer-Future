"""Answering routine questions from approved company information only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KnowledgeItem

# Anything touching these goes to a person, whatever the knowledge base says.
SENSITIVE = re.compile(
    r"(contract|legal|lawyer|attorney|sue|lawsuit|refund|guarantee|warrant|discount|negotiat|complain|angry|"
    r"cancel|terminate|privacy|data|abogado|demanda|contrato|garant[ií]a|descuento|queja|reembolso)", re.I)


@dataclass
class Answer:
    item: KnowledgeItem | None
    text: str | None
    escalate: bool
    reason: str


def find_answer(db: Session, question: str, language: str) -> Answer:
    if SENSITIVE.search(question):
        return Answer(None, None, True, "Sensitive topic (contract, pricing negotiation, legal, complaint or data)")
    items = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.approved.is_(True))))
    scored = []
    lowered = question.lower()
    for it in items:
        kws = [k.strip().lower() for k in it.keywords.split(",") if k.strip()]
        hits = [k for k in kws if k in lowered]
        if hits:
            scored.append((len(hits), it))
    if not scored:
        return Answer(None, None, True, "No approved answer matches this question")
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return Answer(None, None, True, "More than one approved answer matches equally; a person should choose")
    item = scored[0][1]
    text = item.answer_es if language == "es" and item.answer_es else item.answer_en
    if not text:
        return Answer(item, None, True, f"Approved answer has no {language} text")
    return Answer(item, text, False, f"Matched approved answer '{item.topic}'")
