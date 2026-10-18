"""Spanish vocabulary for listing generation. Unknown terms are kept verbatim and
flagged for human translation review rather than machine-guessed."""
from __future__ import annotations

COLORS = {
    "black": "negro", "white": "blanco", "silver": "plateado", "gray": "gris", "grey": "gris", "red": "rojo",
    "blue": "azul", "green": "verde", "brown": "café", "beige": "beige", "tan": "beige", "gold": "dorado",
    "orange": "anaranjado", "yellow": "amarillo", "purple": "morado", "maroon": "guinda", "burgundy": "vino",
    "charcoal": "gris oscuro", "pearl": "perla", "bronze": "bronce",
}
TRANSMISSION = {"en": {"automatic": "automatic", "manual": "manual", "cvt": "CVT (continuously variable)"},
                "es": {"automatic": "automática", "manual": "manual", "cvt": "CVT (variable continua)"}}
DRIVETRAIN = {"en": {"FWD": "front-wheel drive (FWD)", "RWD": "rear-wheel drive (RWD)", "AWD": "all-wheel drive (AWD)",
                     "4WD": "four-wheel drive (4WD)", "2WD": "two-wheel drive (2WD)"},
              "es": {"FWD": "tracción delantera (FWD)", "RWD": "tracción trasera (RWD)", "AWD": "tracción integral (AWD)",
                     "4WD": "doble tracción 4x4 (4WD)", "2WD": "tracción sencilla (2WD)"}}
FUEL = {"en": {"gasoline": "gasoline", "diesel": "diesel", "hybrid": "hybrid", "plug_in_hybrid": "plug-in hybrid",
               "electric": "electric", "flex_fuel": "flex fuel (E85)"},
        "es": {"gasoline": "gasolina", "diesel": "diésel", "hybrid": "híbrido", "plug_in_hybrid": "híbrido enchufable",
               "electric": "eléctrico", "flex_fuel": "combustible flexible (E85)"}}
CONDITION = {"en": {"used": "used", "new": "new", "certified_pre_owned": "certified pre-owned"},
             "es": {"used": "usado", "new": "nuevo", "certified_pre_owned": "seminuevo certificado"}}
BODY = {"sedan": "sedán", "suv": "SUV", "truck": "camioneta pickup", "pickup": "camioneta pickup", "coupe": "cupé",
        "hatchback": "hatchback", "minivan": "minivan", "van": "van", "convertible": "convertible", "wagon": "station wagon",
        "crossover": "crossover"}
TITLE = {"en": {"clean": "clean", "salvage": "salvage", "rebuilt": "rebuilt", "flood": "flood-damage", "lemon_buyback": "lemon buyback"},
         "es": {"clean": "limpio", "salvage": "de salvamento (salvage)", "rebuilt": "reconstruido (rebuilt)",
                "flood": "por daño de inundación", "lemon_buyback": "de recompra por defecto (lemon buyback)"}}

FEATURES = {
    "bluetooth": "Bluetooth", "backup camera": "cámara de reversa", "rear view camera": "cámara de reversa",
    "rearview camera": "cámara de reversa", "navigation": "navegación", "navigation system": "sistema de navegación",
    "gps navigation": "navegación GPS", "sunroof": "quemacocos", "moonroof": "quemacocos", "panoramic sunroof": "quemacocos panorámico",
    "leather seats": "asientos de piel", "heated seats": "asientos con calefacción", "heated front seats": "asientos delanteros con calefacción",
    "cooled seats": "asientos ventilados", "ventilated seats": "asientos ventilados", "power seats": "asientos eléctricos",
    "third row seating": "tercera fila de asientos", "3rd row seating": "tercera fila de asientos",
    "apple carplay": "Apple CarPlay", "android auto": "Android Auto", "keyless entry": "entrada sin llave",
    "push button start": "encendido por botón", "remote start": "encendido remoto", "cruise control": "control de crucero",
    "adaptive cruise control": "control de crucero adaptativo", "blind spot monitor": "monitor de punto ciego",
    "blind spot monitoring": "monitoreo de punto ciego", "lane departure warning": "alerta de cambio de carril",
    "lane keep assist": "asistente de permanencia en carril", "alloy wheels": "rines de aleación",
    "tow package": "paquete de remolque", "towing package": "paquete de remolque", "trailer hitch": "enganche para remolque",
    "power windows": "vidrios eléctricos", "power locks": "seguros eléctricos", "power liftgate": "puerta trasera eléctrica",
    "dual zone climate control": "clima de doble zona", "automatic climate control": "clima automático",
    "usb ports": "puertos USB", "usb port": "puerto USB", "wifi hotspot": "punto de acceso Wi-Fi",
    "premium audio": "audio premium", "satellite radio": "radio satelital", "parking sensors": "sensores de estacionamiento",
    "fog lights": "faros antiniebla", "led headlights": "faros LED", "running boards": "estribos", "bed liner": "protector de caja",
    "roof rack": "parrilla de techo", "heated steering wheel": "volante con calefacción", "touchscreen": "pantalla táctil",
    "touch screen": "pantalla táctil", "wireless charging": "carga inalámbrica", "keyless start": "encendido sin llave",
    "forward collision warning": "alerta de colisión frontal", "automatic emergency braking": "frenado automático de emergencia",
    "rear cross traffic alert": "alerta de tráfico cruzado trasero", "360 camera": "cámara 360", "memory seats": "asientos con memoria",
    "power mirrors": "espejos eléctricos", "heated mirrors": "espejos con calefacción", "spray-in bedliner": "protector de caja rociado",
}


def color_es(color: str | None) -> tuple[str | None, bool]:
    """Return (spanish, translated?)."""
    if not color:
        return None, True
    c = color.strip()
    if c.lower() in COLORS:
        return COLORS[c.lower()], True
    words = c.lower().split()
    base = next((COLORS[w] for w in reversed(words) if w in COLORS), None)
    if base:
        return f"{base} ({c})", True
    return c, False


def feature_es(feature: str) -> tuple[str, bool]:
    hit = FEATURES.get(feature.strip().lower())
    return (hit, True) if hit else (feature, False)


def body_es(body: str | None) -> tuple[str | None, bool]:
    if not body:
        return None, True
    hit = BODY.get(body.strip().lower())
    return (hit, True) if hit else (body, False)
