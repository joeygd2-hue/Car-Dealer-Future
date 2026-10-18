"""Lightweight English/Spanish detection (no external model)."""

from __future__ import annotations

import re
import unicodedata

_ES = {
    "hola", "buenas", "buenos", "gracias", "precio", "cuanto", "cuesta", "carro", "coche", "camioneta", "troca",
    "disponible", "todavia", "tiene", "tienen", "quiero", "puedo", "para", "por", "favor", "usted", "ustedes",
    "esta", "estan", "donde", "cuando", "manana", "hoy", "sabado", "domingo", "lunes", "martes", "miercoles",
    "jueves", "viernes", "millas", "prueba", "manejo", "cita", "verlo", "sigue", "vendido", "pero", "como",
    "que", "el", "la", "los", "las", "es", "un", "una", "y", "con", "mi", "me", "si", "no", "mas", "pagos",
    "credito", "garantia", "llamar", "hablar", "persona", "gerente", "horario", "abren", "cierran", "direccion",
    "busco", "interesa", "interesado", "interesada", "seria", "nombre", "llamo", "numero", "telefono", "correo",
    "dejen", "molesten", "escriban", "agendar", "hora", "las", "del", "al", "muy", "bien", "ese", "este", "esa",
    "estamos", "ubicados", "en", "nuestro", "nuestra", "nuestros", "su", "sus", "le", "lo", "de", "gustaria",
    "aparece", "inventario", "actual", "vehiculo", "incluyen", "caracteristicas", "aprobadas", "equipo", "necesito",
    "confirmar", "alguien", "pronto", "gusto", "ayudo", "funcionan", "mejor", "solicitud", "enviada", "aun", "confirmada",
    "precio", "publicado", "registro", "indica", "ya", "vendio", "puede", "ayudarle", "similares", "lamento", "molestia",
    "gerente", "directamente", "pregunta", "seguridad", "informacion", "comparta", "medio", "gracias", "avisenos",
    "algo", "otro", "dia", "le", "funcionaria", "atencion", "fuera", "ese", "esta", "cerrado", "tiene", "venta",
    "pendiente", "asi", "estado", "ubicacion", "estan", "cual", "cuantas", "ofrecen", "tengo", "ha", "tenido",
}
_EN = {
    "hi", "hello", "hey", "thanks", "thank", "price", "how", "much", "car", "truck", "suv", "available",
    "still", "is", "it", "the", "a", "an", "and", "you", "your", "what", "when", "where", "can", "could",
    "would", "i", "my", "me", "do", "does", "have", "has", "miles", "mileage", "test", "drive", "appointment",
    "today", "tomorrow", "saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "open",
    "hours", "address", "location", "please", "want", "looking", "interested", "this", "that", "with", "for",
    "of", "to", "are", "there", "any", "come", "see", "call", "talk", "person", "manager", "credit", "warranty",
    "trade", "stop", "name", "number", "phone", "email", "schedule", "time", "works", "yes", "ok",
    "we", "our", "located", "listed", "currently", "showing", "inventory", "record", "shows", "need", "confirm",
    "team", "someone", "follow", "up", "shortly", "happy", "help", "best", "request", "sent", "isn", "yet", "will",
    "be", "sold", "similar", "sorry", "trouble", "shared", "directly", "question", "security", "information",
    "share", "here", "welcome", "let", "us", "know", "anything", "else", "other", "day", "outside", "business",
    "which", "would", "like", "include", "approved", "features", "listed", "among", "vehicle", "questions",
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-zñ]+", strip_accents(text.lower()))


def detect_language(text: str, prior: str | None = None) -> tuple[str, float]:
    """Return (language, confidence). Falls back to ``prior`` (or English) when unclear."""
    toks = tokens(text)
    es = sum(1 for t in toks if t in _ES)
    en = sum(1 for t in toks if t in _EN)
    if re.search(r"[ñ¿¡áéíóú]", text.lower()):
        es += 2
    total = es + en
    if total == 0:
        return (prior or "en"), 0.3
    if es == en:
        return (prior or "en"), 0.5
    lang = "es" if es > en else "en"
    conf = max(es, en) / total
    if prior and prior != lang and conf < 0.7 and total < 4:
        return prior, 0.5
    return lang, round(conf, 2)
