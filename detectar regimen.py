"""
detectar_regimen.py — Sistema Sirio v8+
Modulo de deteccion de regimen de mercado.

POR QUE UN MODELO POR REGLAS Y NO UN HIDDEN MARKOV MODEL (HMM) COMPLETO:
--------------------------------------------------------------------------
El framework del hermano de Lu usa un modelo de Markov real (estados ocultos,
matriz de transicion, entrenado con maximizacion de verosimilitud). Eso es
correcto para su escala. Para Sirio, con maximo 4 posiciones simultaneas y un
bot que corre 7x/dia sin supervision constante, un HMM completo trae tres
problemas reales que no valen la pena para este tamano de cuenta:

  1. Opacidad: cuando el HMM dice "Alta Volatilidad" no hay forma rapida de
     verificar POR QUE sin re-entrenar o inspeccionar matrices — mal para un
     bot que debe ser depurable a las 7am sin laptop abierta.
  2. Necesita re-entrenamiento periodico (el numero de estados y las
     transiciones se degradan con el tiempo) — mantenimiento que compite con
     el tiempo real que tiene Lu.
  3. Con series relativamente cortas (490 tickers, historia de 1-2 anios),
     un HMM de 3 estados tiende a sobreajustar en submuestras.

Esta version usa 3 senales transparentes y verificables a ojo en TradingView
en 10 segundos: percentil de volatilidad realizada, pendiente/orden de SMAs,
y ADX. Cubre el mismo proposito practico (evitar tratar un mercado en crash
como si fuera un mercado en tendencia sana) con una decision que se puede
auditar leyendo el codigo, no una caja negra.

Si en el futuro Lu quiere el HMM real, este modulo expone la misma interfaz
de salida (regimen, razon) para que sea reemplazable sin tocar el resto del
bot.

USO:
    from detectar_regimen import detectar_regimen
    resultado = detectar_regimen(df_spx, df_vix=None, df_sector=None)
    # resultado = {"regimen": "ALTA_VOLATILIDAD", "razon": "...", "score": {...}}

Requiere: pandas, numpy (ya en requirements.txt del bot). Sin dependencias nuevas.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field


TENDENCIA = "TENDENCIA"
ALTA_VOLATILIDAD = "ALTA_VOLATILIDAD"
REVERSION_MEDIA = "REVERSION_MEDIA"


@dataclass
class ResultadoRegimen:
    regimen: str
    razon: str
    score: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"regimen": self.regimen, "razon": self.razon, "score": self.score}


def _pendiente_normalizada(serie: pd.Series, ventana: int) -> float:
    """Pendiente de una regresion lineal simple sobre los ultimos `ventana`
    valores, normalizada por el nivel de precio para que sea comparable
    entre tickers de distinto precio."""
    y = serie.tail(ventana).values
    if len(y) < ventana:
        return 0.0
    x = np.arange(len(y))
    pendiente = np.polyfit(x, y, 1)[0]
    return float(pendiente / np.mean(y))


def _percentil_volatilidad_realizada(precios: pd.Series, ventana_corta: int = 20,
                                      ventana_historica: int = 252) -> float:
    """Percentil (0-100) de la volatilidad realizada de los ultimos
    `ventana_corta` dias respecto a la distribucion de 1 anio (`ventana_historica`)."""
    retornos = precios.pct_change().dropna()
    if len(retornos) < ventana_historica:
        ventana_historica = len(retornos)
    vol_movil = retornos.rolling(ventana_corta).std() * np.sqrt(252)
    vol_movil = vol_movil.dropna()
    if len(vol_movil) < 2:
        return 50.0
    ultimo = vol_movil.iloc[-1]
    historico = vol_movil.tail(ventana_historica)
    percentil = (historico < ultimo).mean() * 100
    return float(percentil)


def _adx_simplificado(high: pd.Series, low: pd.Series, close: pd.Series,
                       periodo: int = 14) -> float:
    """ADX estandar (Wilder). Devuelve el ultimo valor."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / periodo, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / periodo, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / periodo, adjust=False).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / periodo, adjust=False).mean()
    return float(adx.iloc[-1]) if len(adx.dropna()) else 0.0


def _drawdown_reciente(precios: pd.Series, ventana: int = 30) -> float:
    """% de caida desde el maximo de los ultimos `ventana` dias hasta el
    ultimo cierre. Devuelve un numero negativo (o 0 si esta en maximos)."""
    ventana_precios = precios.tail(ventana)
    maximo = ventana_precios.max()
    ultimo = precios.iloc[-1]
    return float((ultimo / maximo - 1) * 100)


def detectar_regimen(df_referencia: pd.DataFrame, umbral_vol_alta: float = 80.0,
                      umbral_vol_baja: float = 30.0, umbral_adx_tendencia: float = 22.0,
                      umbral_drawdown: float = -10.0) -> ResultadoRegimen:
    """
    df_referencia: DataFrame con columnas ['Open','High','Low','Close'] del
    indice/ETF de referencia (SPX, o el sector foco si se quiere un regimen
    sectorial en vez de macro). Viene directo de yfinance.download().

    Clasifica en TENDENCIA / ALTA_VOLATILIDAD / REVERSION_MEDIA usando:
      - percentil de volatilidad realizada 20d vs 1 anio
      - drawdown real desde el maximo de 30 dias (evita falsos positivos:
        un ruido estadistico de volatilidad NO es lo mismo que una caida real)
      - ADX(14)
      - pendiente normalizada de SMA50 (20 dias)

    Esto es una ETIQUETA INFORMATIVA (ver manual-operaciones-solares 7.2) —
    no bloquea entradas, el Semaforo sigue siendo el unico filtro que bloquea.
    """
    close = df_referencia["Close"]
    sma50 = close.rolling(50).mean()

    percentil_vol = _percentil_volatilidad_realizada(close)
    drawdown = _drawdown_reciente(close)
    adx = _adx_simplificado(df_referencia["High"], df_referencia["Low"], close)
    pendiente_sma50 = _pendiente_normalizada(sma50.dropna(), 20)

    score = {
        "percentil_volatilidad_20d": round(percentil_vol, 1),
        "drawdown_30d_pct": round(drawdown, 1),
        "adx_14": round(adx, 1),
        "pendiente_sma50_normalizada": round(pendiente_sma50, 5),
    }

    # Regla 1: ALTA_VOLATILIDAD requiere volatilidad elevada Y una caida real
    # reciente (o el espejo: rebote violento saliendo de esa caida). Exigir
    # las dos evita que un ruido estadistico normal en un mercado en tendencia
    # sana se marque como crash solo porque la vol de la ventana repunto.
    if percentil_vol >= umbral_vol_alta and drawdown <= umbral_drawdown:
        return ResultadoRegimen(
            regimen=ALTA_VOLATILIDAD,
            razon=(f"Volatilidad realizada en percentil {percentil_vol:.0f} vs el ultimo anio "
                   f"(umbral {umbral_vol_alta:.0f}) JUNTO CON caida de {drawdown:.1f}% desde el "
                   f"maximo de 30 dias (umbral {umbral_drawdown:.0f}%). Es un regimen de crash/"
                   f"rebote violento, no de tendencia sana — esperar estructuralmente menos "
                   f"setups Sistema 6 Pasos."),
            score=score,
        )

    # Regla 2: tendencia limpia = ADX fuerte + pendiente SMA50 consistente
    if adx >= umbral_adx_tendencia and abs(pendiente_sma50) > 1e-4:
        direccion = "alcista" if pendiente_sma50 > 0 else "bajista"
        return ResultadoRegimen(
            regimen=TENDENCIA,
            razon=f"ADX {adx:.1f} >= {umbral_adx_tendencia:.0f} con SMA50 en pendiente {direccion} "
                  f"consistente. Frecuencia normal de setups esperada.",
            score=score,
        )

    # Regla 3: todo lo demas es rango / reversion a la media
    return ResultadoRegimen(
        regimen=REVERSION_MEDIA,
        razon=f"ADX {adx:.1f} debil y volatilidad en percentil {percentil_vol:.0f} (no extrema). "
              f"Mercado lateral — setups escasos pero de mejor calidad cuando aparecen.",
        score=score,
    )


if __name__ == "__main__":
    # Prueba con datos sinteticos (esta sandbox no tiene acceso a Yahoo Finance).
    # En el bot real, reemplazar por:
    #   import yfinance as yf
    #   df_spx = yf.download("^GSPC", period="1y", interval="1d")
    #   resultado = detectar_regimen(df_spx)
    rng = np.random.default_rng(7)
    n = 300
    fechas = pd.date_range("2025-10-01", periods=n, freq="B")

    # Simula un crash en los ultimos 25 dias (como SOX en jun-jul 2026) para
    # verificar que el clasificador lo marca ALTA_VOLATILIDAD.
    ret_normal = rng.normal(0.0004, 0.008, n - 25)
    ret_crash = rng.normal(-0.01, 0.035, 25)
    retornos = np.concatenate([ret_normal, ret_crash])
    precios = 100 * np.cumprod(1 + retornos)

    df_test = pd.DataFrame({
        "Close": precios,
        "High": precios * (1 + rng.uniform(0.001, 0.02, n)),
        "Low": precios * (1 - rng.uniform(0.001, 0.02, n)),
        "Open": precios,
    }, index=fechas)

    resultado = detectar_regimen(df_test)
    print("=== Prueba con datos sinteticos (crash simulado ultimos 25 dias) ===")
    print(resultado.to_dict())
    assert resultado.regimen == ALTA_VOLATILIDAD, "El clasificador deberia marcar el crash simulado"
    print("\nOK — el clasificador detecta el regimen de alta volatilidad correctamente.")
