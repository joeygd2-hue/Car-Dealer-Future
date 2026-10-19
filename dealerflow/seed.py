"""Idempotent seed data: pipeline stages, starter templates, knowledge-base placeholders, example campaign.

Templates and answers are starting points. Edit them so every statement is true for your business.
Knowledge items are seeded *unapproved*; the agent ignores them until an admin approves them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Campaign, KnowledgeItem, Template
from .services.agent import state
from .services.crm import seed_stages

TEMPLATES = [
    ("intro", "en", "Lead follow-up for {dealership_name}",
     "Hi,\n\nI'm {sender_name} with {business_name}. We set up automated lead follow-up for independent "
     "dealerships around Houston, so internet leads get a quick reply in English or Spanish and follow-ups "
     "don't slip.\n\nWould a 20-minute Zoom call be useful to see whether it fits {dealership_name}? "
     "If the timing isn't right, just say so.\n\nThanks,\n{sender_name}"),
    ("intro", "es", "Seguimiento de clientes para {dealership_name}",
     "Hola,\n\nSoy {sender_name} de {business_name}. Configuramos seguimiento automático de clientes para "
     "concesionarios independientes en el área de Houston, para que los clientes de internet reciban una "
     "respuesta rápida en español o inglés.\n\n¿Le serviría una llamada de 20 minutos por Zoom para ver si "
     "le conviene a {dealership_name}? Si no es buen momento, no hay problema.\n\nGracias,\n{sender_name}"),
    ("follow_up_1", "en", "Following up — {dealership_name}",
     "Hi,\n\nJust following up on my note last week about lead follow-up for {dealership_name}. "
     "Happy to share how it works on a short Zoom call, or I can leave you alone — whichever you prefer.\n\n"
     "{sender_name}"),
    ("follow_up_1", "es", "Seguimiento — {dealership_name}",
     "Hola,\n\nLe escribo para dar seguimiento a mi mensaje sobre el seguimiento de clientes para "
     "{dealership_name}. Con gusto le muestro cómo funciona en una llamada corta por Zoom, o si prefiere no "
     "recibir más mensajes, me avisa.\n\n{sender_name}"),
]

KNOWLEDGE = [
    ("What the service is", "what do you do,what is this,how does it work,how does this work,qué hacen,cómo funciona",
     "[EDIT BEFORE APPROVING] Describe your service in two sentences.",
     "[EDITAR ANTES DE APROBAR] Describa su servicio en dos oraciones."),
    ("Pricing", "how much,price,pricing,cost,cuánto cuesta,precio",
     "[EDIT BEFORE APPROVING] State your published pricing, or say pricing depends on setup and offer a call.",
     "[EDITAR ANTES DE APROBAR] Indique sus precios publicados u ofrezca una llamada."),
    ("Spanish support", "spanish,español,bilingual,bilingüe",
     "[EDIT BEFORE APPROVING] Explain how Spanish-language lead replies work.",
     "[EDITAR ANTES DE APROBAR] Explique cómo funcionan las respuestas en español."),
]


def seed(db: Session) -> None:
    seed_stages(db)
    for name, lang, subject, body in TEMPLATES:
        if not db.scalar(select(Template).where(Template.name == name, Template.language == lang)):
            db.add(Template(name=name, language=lang, subject=subject, body=body, active=True))
    for topic, kws, en, es in KNOWLEDGE:
        if not db.scalar(select(KnowledgeItem).where(KnowledgeItem.topic == topic)):
            db.add(KnowledgeItem(topic=topic, keywords=kws, answer_en=en, answer_es=es, approved=False))
    if not db.scalar(select(Campaign).where(Campaign.name == "Sugar Land intro")):
        db.add(Campaign(name="Sugar Land intro", status="paused", language_mode="auto", approval_mode="manual",
                        steps=[{"template": "intro", "delay_days": 0}, {"template": "follow_up_1", "delay_days": 5}],
                        min_score=55, daily_cap=10))
    state(db)
    db.flush()
    from .services.maintenance import seed_phase4

    seed_phase4(db)
    db.flush()
