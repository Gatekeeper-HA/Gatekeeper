"""Keyword classification of the visitor's answer."""

from __future__ import annotations

DELIVERY_WORDS = {
    "delivery", "deliver", "fedex", "ups", "amazon", "package",
    "doordash", "uber eats", "ubereats", "instacart",
    "entrega", "paquete",
}

SALES_WORDS = {
    "sales", "selling", "soliciting", "solicitation", "canvassing",
    "campaign", "petition", "ventas", "vender",
}

MAINTENANCE_WORDS = {
    "maintenance", "repair", "service", "technician", "tech",
    "contractor", "landscaping", "plumber", "electrician",
    "mantenimiento", "servicio", "tecnico", "técnico",
}


def classify_response(text: str) -> tuple[str, str]:
    """Return ``(classification, reply_key)`` for a transcript.

    ``reply_key`` indexes ``Settings.replies``.
    """
    normalized = text.lower().strip()

    if not normalized:
        return "unknown_uncooperative", "no_answer"

    if any(word in normalized for word in DELIVERY_WORDS):
        return "likely_delivery", "delivery"

    if any(word in normalized for word in SALES_WORDS):
        return "unknown_cooperative", "sales"

    if any(word in normalized for word in MAINTENANCE_WORDS):
        return "unknown_cooperative", "maintenance"

    if len(normalized.split()) <= 1:
        return "unknown_uncooperative", "no_answer"

    return "unknown_cooperative", "generic"
