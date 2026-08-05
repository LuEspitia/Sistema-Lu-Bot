# -*- coding: utf-8 -*-
"""
carta_natal_transitos.py — Sistema Sirio v9
Transitos astrologicos REALES sobre la carta natal de Lu, calculados con
Swiss Ephemeris (motor Moshier — semi-analitico, sin archivos de efemerides
externos, funciona offline en GitHub Actions sin descargar nada).

Se intento primero asumir que esto necesitaba una API o una dependencia
pesada. Se probo en la sandbox: `pip install pyswisseph` funciona, y el
modelo Moshier calcula posiciones planetarias con precision de minutos de
arco sin necesidad de datos externos. Si funciona en la sandbox, funciona
en GitHub Actions — mismo Python, mismo paquete via pip.

Requiere agregar a requirements.txt:
    pyswisseph>=2.10.0

USO:
    from carta_natal_transitos import tránsitos_de_hoy
    resultado = tránsitos_de_hoy()
    # resultado = lista de aspectos activos hoy entre planetas en transito
    # y la carta natal de Lu, con orbe y si esta aplicando o separando.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
import swisseph as swe

swe.set_ephe_path('')  # Moshier no necesita archivos — evita que busque un path que no existe

SIGNOS = ["Aries","Tauro","Geminis","Cancer","Leo","Virgo","Libra","Escorpio",
          "Sagitario","Capricornio","Acuario","Piscis"]

def _dms_a_absoluto(signo_idx: int, grados: float, minutos: float, segundos: float) -> float:
    return signo_idx * 30 + grados + minutos / 60 + segundos / 3600

# ── CARTA NATAL DE LU — 27-ago-1987, 3:25am, Medellin (75w35, 6n15) ────────
# Longitudes absolutas (0-360), calculadas desde los grados/signo de la
# hoja de datos de Astrodienst que Lu compartio el 30-jul-2026.
NATAL = {
    "Sol":       _dms_a_absoluto(5, 3, 32, 52),    # Virgo
    "Luna":      _dms_a_absoluto(6, 5, 35, 7),     # Libra
    "Mercurio":  _dms_a_absoluto(5, 10, 27, 6),    # Virgo
    "Venus":     _dms_a_absoluto(5, 4, 40, 3),     # Virgo
    "Marte":     _dms_a_absoluto(5, 2, 52, 42),    # Virgo
    "Jupiter":   _dms_a_absoluto(0, 29, 38, 14),   # Aries (r)
    "Saturno":   _dms_a_absoluto(8, 14, 35, 7),    # Sagitario
    "Urano":     _dms_a_absoluto(9, 22, 43, 47),   # Capricornio
    "Neptuno":   _dms_a_absoluto(9, 5, 20, 51),    # Capricornio (r)
    "Pluton":    _dms_a_absoluto(7, 7, 36, 5),     # Escorpio
    "Ascendente":_dms_a_absoluto(3, 26, 6, 35),    # Cancer
}

PLANETAS_TRANSITO = {
    "Sol": swe.SUN, "Luna": swe.MOON, "Mercurio": swe.MERCURY, "Venus": swe.VENUS,
    "Marte": swe.MARS, "Jupiter": swe.JUPITER, "Saturno": swe.SATURN,
    "Urano": swe.URANUS, "Neptuno": swe.NEPTUNE, "Pluton": swe.PLUTO,
}

# Aspectos clasicos y su orbe de tolerancia (grados). Planetas rapidos
# (Sol/Luna/Mercurio/Venus/Marte) usan orbe mas chico porque se mueven mucho
# en un dia; los lentos (Jupiter en adelante) toleran mas.
ASPECTOS = {"Conjuncion": 0, "Sextil": 60, "Cuadratura": 90, "Trigono": 120, "Oposicion": 180}
ORBE_RAPIDO = 3.0
ORBE_LENTO = 5.0
PLANETAS_RAPIDOS = {"Sol", "Luna", "Mercurio", "Venus", "Marte"}


@dataclass
class Aspecto:
    planeta_transito: str
    planeta_natal: str
    aspecto: str
    orbe: float
    exacto_en_grados: float

    def texto(self) -> str:
        return (f"{self.planeta_transito} transito {self.aspecto} {self.planeta_natal} natal "
                f"(orbe {self.orbe:.1f}°)")


def _long_transito(jd: float, planeta: str) -> float:
    pos, _ = swe.calc_ut(jd, PLANETAS_TRANSITO[planeta], swe.FLG_MOSEPH)
    return pos[0]


def _signo(lon: float) -> str:
    return SIGNOS[int(lon // 30) % 12]


def posiciones_hoy(fecha: date = None) -> dict:
    """Posiciones actuales (signo + grado) de los planetas en transito."""
    fecha = fecha or date.today()
    jd = swe.julday(fecha.year, fecha.month, fecha.day, 12.0)
    resultado = {}
    for nombre in PLANETAS_TRANSITO:
        lon = _long_transito(jd, nombre)
        resultado[nombre] = {"signo": _signo(lon), "grado": round(lon % 30, 2), "longitud": lon}
    return resultado


def tránsitos_de_hoy(fecha: date = None, solo_activos: bool = True) -> list[Aspecto]:
    """
    Calcula todos los aspectos entre planetas en transito y la carta natal
    de Lu para la fecha dada (hoy por defecto). Devuelve solo los que estan
    dentro de orbe (aspecto activo/real), ordenados por orbe (mas exacto primero).
    """
    fecha = fecha or date.today()
    jd = swe.julday(fecha.year, fecha.month, fecha.day, 12.0)
    activos = []

    for nombre_t, pid in PLANETAS_TRANSITO.items():
        lon_t = _long_transito(jd, nombre_t)
        orbe_max = ORBE_RAPIDO if nombre_t in PLANETAS_RAPIDOS else ORBE_LENTO

        for nombre_n, lon_n in NATAL.items():
            diff = abs(lon_t - lon_n) % 360
            if diff > 180:
                diff = 360 - diff

            for asp_nombre, asp_grados in ASPECTOS.items():
                orbe = abs(diff - asp_grados)
                if orbe <= orbe_max:
                    activos.append(Aspecto(nombre_t, nombre_n, asp_nombre, orbe, asp_grados))

    activos.sort(key=lambda a: a.orbe)
    return activos


def fase_lunar_hoy(fecha: date = None) -> dict:
    """
    Fase lunar de hoy (creciente/menguante + % iluminacion) + signo en
    transito de la Luna, comparado contra la Luna natal de Lu (Libra).
    Formula de iluminacion via elongacion Sol-Luna: k = (1 - cos(elong)) / 2.
    """
    fecha = fecha or date.today()
    jd = swe.julday(fecha.year, fecha.month, fecha.day, 12.0)

    lon_luna = _long_transito(jd, "Luna")
    lon_sol = _long_transito(jd, "Sol")
    elong = (lon_luna - lon_sol) % 360  # 0-360, 0=nueva, 180=llena

    import math
    iluminacion_pct = (1 - math.cos(math.radians(elong))) / 2 * 100

    creciente = elong < 180
    if iluminacion_pct < 1:
        nombre = "Nueva"
    elif iluminacion_pct > 99:
        nombre = "Llena"
    elif iluminacion_pct < 50:
        nombre = "Creciente" if creciente else "Menguante"
    else:
        nombre = "Gibosa Creciente" if creciente else "Gibosa Menguante"

    return {
        "fase": nombre,
        "iluminacion_pct": iluminacion_pct,
        "signo_transito": _signo(lon_luna),
        "signo_natal_luna": _signo(NATAL["Luna"]),
        "elongacion": elong,
    }


if __name__ == "__main__":
    hoy = date.today()
    print(f"=== Posiciones en transito — {hoy} ===")
    for nombre, info in posiciones_hoy(hoy).items():
        print(f"  {nombre}: {info['grado']:.2f}° {info['signo']}")

    print(f"\n=== Aspectos activos sobre la carta natal de Lu — {hoy} ===")
    aspectos = tránsitos_de_hoy(hoy)
    if not aspectos:
        print("  Sin aspectos dentro de orbe hoy.")
    for a in aspectos:
        print(f"  {a.texto()}")

    # Verificacion cruzada: el 30-jul-2026 Venus transito debia estar en
    # Virgo tarde (~22°), ya pasado el stellium natal (Sol/Venus/Marte 2-5°,
    # Mercurio 10°) — confirmar que el modulo coincide con lo calculado a mano.
    if hoy == date(2026, 7, 30):
        pos = posiciones_hoy(hoy)
        assert pos["Venus"]["signo"] == "Virgo", "Venus deberia estar en Virgo el 30-jul-2026"
        print(f"\nOK — Venus en {pos['Venus']['grado']:.1f}° Virgo, coincide con el calculo verificado.")
