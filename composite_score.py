# -*- coding: utf-8 -*-
"""
composite_score.py — Sistema Sirio v9
5-Factor Composite Score (punto pendiente del mapeo del framework del hermano de Lu).

Pesos — mas peso a tecnico y macro porque el sistema es intradia/swing, tal
como especifico Lu sobre el embudo de su hermano:
    Tecnico:      40%
    Macro:        20%
    Sector:       15%
    Fundamental:  15%
    Geopolitico:  10%

CADA FACTOR APLICA UNA TEORIA CONCRETA, NO ES DECORACION:
  - Tecnico:      Sistema 6 Pasos (ya existente) + Minervini VCP (Volatility
                   Contraction Pattern) — un pullback sano contrae rango en
                   cada swing sucesivo antes del breakout, no solo cae ±5%.
  - Macro:        detectar_regimen.py (Markov-lite) + Zweig regla #6
                   ("no luches contra la Fed" -> castiga ALTA_VOLATILIDAD).
  - Sector:       fuerza relativa vs SPX — Zweig regla #1 ("la tendencia es
                   tu amiga") aplicada a nivel sector antes que ticker,
                   y el concepto de Livermore de operar el sector lider.
  - Fundamental:  CANSLIM de William O'Neil (C-A-N-S-I, la M de CANSLIM se
                   fusiona con el factor Macro para no duplicar peso).
  - Geopolitico:  deliberadamente cualitativo — Zweig regla #13 ("nunca
                   conoceras todas las respuestas, acepta la incertidumbre").
                   No se le fuerza un numero falso-preciso.

Elder (Triple Screen) no es un factor nuevo — ya es la estructura
Semanal-Diario-1H del sistema. Este modulo no la duplica, solo lo documenta
para que quede explicito en el mapeo (ver manual-operaciones-solares 7.3).
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from detectar_regimen import detectar_regimen, TENDENCIA, ALTA_VOLATILIDAD, REVERSION_MEDIA

PESOS = {
    "tecnico": 0.40,
    "macro": 0.20,
    "sector": 0.15,
    "fundamental": 0.15,
    "geopolitico": 0.10,
}


@dataclass
class ScoreCompuesto:
    total: float
    factores: dict = field(default_factory=dict)
    detalle: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 1), "factores": self.factores, "detalle": self.detalle}


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


# ── FACTOR TECNICO ────────────────────────────────────────────────────────
def factor_tecnico(close: pd.Series, high: pd.Series, low: pd.Series, rsi_actual: float,
                    dist_sma50_pct: float, volumen_ratio: float) -> tuple[float, str]:
    """
    Combina lo que ya exige el Sistema 6 Pasos (RSI centrado en 40-58,
    pullback cerca de SMA50) con el VCP de Minervini: mide si el rango
    diario se esta CONTRAYENDO en los ultimos 10 dias vs los 10 anteriores
    — contraccion = mayor probabilidad de breakout limpio, expansion = mayor
    probabilidad de que sea ruido/falsa señal.
    """
    # RSI: score maximo en el centro de la banda 40-58 (49), cae hacia los bordes
    centro_rsi = 49.0
    rsi_score = _clip(100 - abs(rsi_actual - centro_rsi) * 6)

    # Distancia a SMA50: score maximo en 0%, cae hacia ±5%
    pullback_score = _clip(100 - abs(dist_sma50_pct) * 18)

    # Volumen: 120% es el minimo, score sube hasta 200% y se estabiliza
    volumen_score = _clip((volumen_ratio - 0.8) * 125)

    # VCP — contraccion de rango (Minervini)
    rango_diario = (high - low) / close
    rango_reciente = rango_diario.tail(10).mean()
    rango_previo = rango_diario.tail(20).head(10).mean()
    if rango_previo > 0:
        contraccion_pct = (rango_previo - rango_reciente) / rango_previo * 100
    else:
        contraccion_pct = 0.0
    vcp_score = _clip(50 + contraccion_pct * 2.5)  # contrae -> sube; expande -> baja

    tecnico = 0.35 * rsi_score + 0.30 * pullback_score + 0.15 * volumen_score + 0.20 * vcp_score
    detalle = (f"RSI={rsi_score:.0f} Pullback={pullback_score:.0f} Vol={volumen_score:.0f} "
               f"VCP={vcp_score:.0f} (contraccion rango {contraccion_pct:+.1f}%)")
    return _clip(tecnico), detalle


# ── FACTOR MACRO ──────────────────────────────────────────────────────────
def factor_macro(df_spx: pd.DataFrame) -> tuple[float, str]:
    resultado = detectar_regimen(df_spx)
    mapa = {TENDENCIA: 100.0, REVERSION_MEDIA: 60.0, ALTA_VOLATILIDAD: 20.0}
    score = mapa[resultado.regimen]
    return score, f"Regimen={resultado.regimen} ({resultado.razon})"


# ── FACTOR SECTOR ──────────────────────────────────────────────────────────
def factor_sector(close_sector: pd.Series, close_spx: pd.Series, ventana: int = 20) -> tuple[float, str]:
    """Fuerza relativa: retorno del sector menos retorno del SPX en la misma
    ventana. Zweig #1 aplicado a nivel sector antes que ticker."""
    ret_sector = (close_sector.iloc[-1] / close_sector.iloc[-ventana] - 1) * 100
    ret_spx = (close_spx.iloc[-1] / close_spx.iloc[-ventana] - 1) * 100
    fuerza_relativa = ret_sector - ret_spx
    # +10% de fuerza relativa = 100 puntos, -10% = 0 puntos, escala lineal
    score = _clip(50 + fuerza_relativa * 5)
    return score, f"Fuerza relativa sector vs SPX ({ventana}d) = {fuerza_relativa:+.1f}pp"


# ── FACTOR FUNDAMENTAL (CANSLIM) ────────────────────────────────────────────
def factor_fundamental(info_yf: dict) -> tuple[float, str]:
    """
    info_yf: el dict que devuelve yfinance Ticker.info — se leen los campos
    ya disponibles ahi para aproximar CANSLIM sin llamadas extra a otra API.

    C — Current quarterly earnings growth   -> earningsQuarterlyGrowth
    A — Annual earnings growth              -> earningsGrowth
    N — New (cerca de maximos = "algo nuevo pasando") -> fiftyTwoWeekHigh vs precio
    S — Supply/demand (volumen relativo)    -> ya cubierto en factor tecnico, no se duplica
    L — Leader (fuerza relativa del TICKER, no solo el sector) -> se pasa desde factor_sector
        aplicado al ticker mismo si se quiere mayor precision (opcional)
    I — Institutional sponsorship           -> heldPercentInstitutions
    M — Market direction                    -> se fusiona con factor_macro, no se duplica aqui

    Campos ausentes (comunes en tickers pequeños/ETFs) se tratan como neutral
    (50) en vez de penalizar — Zweig #13, no fingir certeza que no hay.
    """
    def _neutral_si_falta(valor, escala=100.0, offset=0.5):
        if valor is None:
            return 50.0
        return _clip(offset * 100 + valor * escala)

    c = _neutral_si_falta(info_yf.get("earningsQuarterlyGrowth"), escala=150)
    a = _neutral_si_falta(info_yf.get("earningsGrowth"), escala=150)

    precio = info_yf.get("currentPrice") or info_yf.get("regularMarketPrice")
    ath = info_yf.get("fiftyTwoWeekHigh")
    if precio and ath and ath > 0:
        dist_ath_pct = (precio / ath - 1) * 100  # negativo = bajo el ATH
        n = _clip(100 + dist_ath_pct * 2.5)  # cerca del ATH = mas puntos (momentum de "algo nuevo")
    else:
        n = 50.0

    inst = info_yf.get("heldPercentInstitutions")
    i_score = _clip(inst * 100) if inst is not None else 50.0

    fundamental = 0.30 * c + 0.25 * a + 0.25 * n + 0.20 * i_score
    detalle = f"C={c:.0f} A={a:.0f} N={n:.0f} I={i_score:.0f} (CANSLIM parcial, S y L via otros factores)"
    return _clip(fundamental), detalle


# ── FACTOR GEOPOLITICO ──────────────────────────────────────────────────────
def factor_geopolitico(flag_manual: Optional[float] = None) -> tuple[float, str]:
    """
    Deliberadamente NO automatizado con falsa precision. Se pasa un flag
    manual (0-100) cuando Lu/Solares identifican un catalizador geopolitico
    relevante esa semana (sanciones, guerra, elecciones). Sin flag = neutral.
    """
    if flag_manual is None:
        return 50.0, "Sin catalizador geopolitico marcado esta semana — neutral"
    return _clip(flag_manual), f"Flag manual de catalizador geopolitico = {flag_manual}"


# ── COMPOSITE FINAL ─────────────────────────────────────────────────────────
def composite_score(*, close: pd.Series, high: pd.Series, low: pd.Series, rsi_actual: float,
                     dist_sma50_pct: float, volumen_ratio: float, df_spx: pd.DataFrame,
                     close_sector: pd.Series, info_yf: dict,
                     flag_geopolitico: Optional[float] = None) -> ScoreCompuesto:
    t_score, t_det = factor_tecnico(close, high, low, rsi_actual, dist_sma50_pct, volumen_ratio)
    m_score, m_det = factor_macro(df_spx)
    s_score, s_det = factor_sector(close_sector, df_spx["Close"])
    f_score, f_det = factor_fundamental(info_yf)
    g_score, g_det = factor_geopolitico(flag_geopolitico)

    total = (PESOS["tecnico"] * t_score + PESOS["macro"] * m_score + PESOS["sector"] * s_score
             + PESOS["fundamental"] * f_score + PESOS["geopolitico"] * g_score)

    return ScoreCompuesto(
        total=total,
        factores={"tecnico": round(t_score, 1), "macro": round(m_score, 1),
                  "sector": round(s_score, 1), "fundamental": round(f_score, 1),
                  "geopolitico": round(g_score, 1)},
        detalle={"tecnico": t_det, "macro": m_det, "sector": s_det,
                 "fundamental": f_det, "geopolitico": g_det},
    )


if __name__ == "__main__":
    # Prueba con datos sinteticos (misma sandbox sin acceso a Yahoo Finance).
    rng = np.random.default_rng(11)
    n = 300
    fechas = pd.date_range("2025-10-01", periods=n, freq="B")

    # Ticker candidato: tendencia sana con contraccion de rango REAL al final
    # (VCP autentico — el rango intradiario se estrecha en los ultimos 10-20
    # dias, no solo la volatilidad de cierre-a-cierre).
    ret = rng.normal(0.0010, 0.010, n - 15)
    ret_contraccion = rng.normal(0.0008, 0.004, 15)
    retornos = np.concatenate([ret, ret_contraccion])
    precios = 100 * np.cumprod(1 + retornos)

    amplitud_rango = np.concatenate([
        np.abs(rng.normal(0.012, 0.004, n - 15)),   # rango normal
        np.abs(rng.normal(0.004, 0.0015, 15)),      # ultimos 15 dias: rango se contrae de verdad
    ])
    high = precios * (1 + amplitud_rango)
    low = precios * (1 - amplitud_rango)
    df_ticker = pd.DataFrame({"Close": precios, "High": high, "Low": low}, index=fechas)

    # SPX de referencia en TENDENCIA sana (misma parametrizacion validada en
    # detectar_regimen.py: drift consistente, ADX fuerte, sin drawdown real)
    ret_spx = rng.normal(0.0012, 0.009, n)
    precios_spx = 100 * np.cumprod(1 + ret_spx)
    df_spx = pd.DataFrame({
        "Close": precios_spx,
        "High": precios_spx * 1.008,
        "Low": precios_spx * 0.992,
        "Open": precios_spx,
    }, index=fechas)

    # Sector con fuerza relativa positiva CONCENTRADA en los ultimos 20 dias
    # (sector rotando a liderar justo ahora, no un drift historico diluido)
    close_sector = precios_spx.copy()
    close_sector[-20:] = close_sector[-20:] * (1 + np.linspace(0, 0.07, 20))
    close_sector = pd.Series(close_sector, index=fechas)

    info_yf_simulado = {
        "earningsQuarterlyGrowth": 0.18,
        "earningsGrowth": 0.22,
        "currentPrice": float(precios[-1]),
        "fiftyTwoWeekHigh": float(precios[-1]) * 1.03,
        "heldPercentInstitutions": 0.62,
    }

    resultado = composite_score(
        close=df_ticker["Close"], high=df_ticker["High"], low=df_ticker["Low"],
        rsi_actual=48.0, dist_sma50_pct=1.2, volumen_ratio=1.4,
        df_spx=df_spx, close_sector=close_sector, info_yf=info_yf_simulado,
        flag_geopolitico=None,
    )
    print("=== Prueba composite_score con datos sinteticos (candidato fuerte) ===")
    import json
    print(json.dumps(resultado.to_dict(), indent=2, ensure_ascii=False))
    assert resultado.total > 65, "Un candidato fuerte deberia superar el score_minimo de 65"
    print("\nOK — score total supera 65 con un candidato tecnica y fundamentalmente fuerte.")

    # Segunda prueba: candidato DEBIL (RSI tardio, sin contraccion, sector rezagado)
    info_yf_debil = {"earningsQuarterlyGrowth": -0.05, "earningsGrowth": -0.10,
                      "currentPrice": float(precios[-1]) * 0.85,
                      "fiftyTwoWeekHigh": float(precios[-1]), "heldPercentInstitutions": 0.15}
    close_sector_debil = pd.Series(precios_spx * (1 - np.linspace(0, 0.05, n)), index=fechas)
    resultado_debil = composite_score(
        close=df_ticker["Close"], high=df_ticker["High"], low=df_ticker["Low"],
        rsi_actual=62.0, dist_sma50_pct=8.0, volumen_ratio=0.6,
        df_spx=df_spx, close_sector=close_sector_debil, info_yf=info_yf_debil,
        flag_geopolitico=None,
    )
    print("\n=== Prueba composite_score con candidato debil ===")
    print(json.dumps(resultado_debil.to_dict(), indent=2, ensure_ascii=False))
    assert resultado_debil.total < 50, "Un candidato debil deberia quedar claramente bajo el score_minimo"
    print("\nOK — score total del candidato debil queda claramente bajo 65.")
