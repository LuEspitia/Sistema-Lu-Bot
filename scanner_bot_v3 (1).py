"""
SISTEMA LU — BOT v3.0
Todo ya configurado. No edites nada excepto las claves en GitHub Secrets.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date
from urllib.parse import quote
import anthropic

CONFIG = {
    # ── Estas 3 claves van en GitHub Secrets, no aquí ──
    "whatsapp_number":      os.environ.get("WHATSAPP_NUMBER", ""),
    "callmebot_apikey":     os.environ.get("CALLMEBOT_APIKEY", ""),
    "claude_api_key":       os.environ.get("ANTHROPIC_API_KEY", ""),

    # ── Tu capital y riesgo ──
    "capital_usd":          33140,
    "risk_per_trade_pct":   2.0,
    "stop_loss_pct":        6.0,

    # ── Filtros Sistema Maestro v4 ──
    "price_min":            10.0,
    "price_max":            150.0,
    "rsi_min":              45,
    "rsi_max":              72,
    "min_volume":           1_000_000,
    "min_fan_to_alert":     3,
    "adx_min_trend":        20,

    # ── Archivo anti-spam (no repetir alertas del mismo día) ──
    "state_file":           "/tmp/lu_alertas_hoy.json",

    # ── IBKR auto-ejecución: False = solo alertas, no compra sola ──
    "ibkr_auto_execute":    False,
    "ibkr_host":            "127.0.0.1",
    "ibkr_port":            7497,
    "ibkr_client_id":       1,
    "ibkr_min_prob_to_buy": 75,

    # ── Tu watchlist completa ──
    "watchlist": [
        # Swing trading (Sistema Maestro)
        "KGC",   # Kinross Gold
        "OXY",   # Occidental Petroleum
        "SLB",   # SLB oil services
        "BTU",   # Peabody Energy
        "PAAS",  # Pan American Silver
        "SQM",   # Sociedad Química (litio)
        "FCX",   # Freeport-McMoRan (cobre)
        "HIMS",  # Hims & Hers Health
        "EW",    # Edwards Lifesciences
        "DAR",   # Darling Ingredients
        "HAL",   # Halliburton
        "BKR",   # Baker Hughes
        # Oro (Capa 2)
        "GDX",   # VanEck Gold Miners  ~$94
        "GOAU",  # U.S. Global Go Gold ~$50
        # Dividendos (Capa 3)
        "JEPI",  # JPMorgan Premium Income ~$58
        "SCHD",  # Schwab Dividend ~$36
        # Bitcoin ETF (Reserva)
        "IBIT",  # iShares Bitcoin ETF ~$42
    ]
}

# ── Anti-spam ──────────────────────────────────────────────────
def cargar_estado():
    try:
        with open(CONFIG["state_file"], "r") as f:
            e = json.load(f)
        if e.get("fecha") != str(date.today()):
            return {"fecha": str(date.today()), "alertados": []}
        return e
    except:
        return {"fecha": str(date.today()), "alertados": []}

def guardar_estado(e):
    try:
        with open(CONFIG["state_file"], "w") as f:
            json.dump(e, f)
    except:
        pass

# ── Indicadores técnicos ───────────────────────────────────────
def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def sma(s, p):
    return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p + 1: return None
    d = c.diff()
    g = d.where(d > 0, 0.0)
    l = -d.where(d < 0, 0.0)
    ag = g.ewm(com=p-1, min_periods=p).mean()
    al = l.ewm(com=p-1, min_periods=p).mean()
    rs = ag / al
    return float((100 - (100 / (1 + rs))).iloc[-1])

def calc_macd(c):
    if len(c) < 35: return None, None, None, "N/A"
    ml = ema(c, 12) - ema(c, 26)
    sl = ema(ml, 9)
    hl = ml - sl
    mv, sv, hv = float(ml.iloc[-1]), float(sl.iloc[-1]), float(hl.iloc[-1])
    if mv > sv and hv > 0:   estado = "Bullish"
    elif mv > sv:            estado = "Weak Bull"
    elif mv < sv and hv < 0: estado = "Bearish"
    else:                    estado = "Weak Bear"
    return mv, sv, hv, estado

def calc_adx(h, l, c, p=14):
    if len(h) < p * 2: return None, None, None, "N/A"
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    dp = h.diff(); dm = -l.diff()
    dp = dp.where((dp > dm) & (dp > 0), 0.0)
    dm = dm.where((dm > dp) & (dm > 0), 0.0)
    atr = tr.ewm(alpha=1/p, adjust=False).mean()
    dip = 100 * dp.ewm(alpha=1/p, adjust=False).mean() / atr
    dim = 100 * dm.ewm(alpha=1/p, adjust=False).mean() / atr
    dx  = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
    av  = float(dx.ewm(alpha=1/p, adjust=False).mean().iloc[-1])
    e   = "Strong Trend" if av >= 25 else "Moderate" if av >= 20 else "Weak"
    return av, float(dip.iloc[-1]), float(dim.iloc[-1]), e

# ── Obtener datos ──────────────────────────────────────────────
def obtener_datos(ticker):
    try:
        s    = yf.Ticker(ticker)
        hist = s.history(period="1y", interval="1d", prepost=True)
        if hist.empty or len(hist) < 210:
            return {"ticker": ticker, "error": "Datos insuficientes"}
        c, h, l, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
        precio = float(c.iloc[-1])
        prev   = float(c.iloc[-2])
        es_pm  = False
        try:
            fi = s.fast_info
            pm = float(fi.get("pre_market_price") or 0)
            if pm > 0: precio = pm; es_pm = True
        except: pass
        pct = (precio - prev) / prev * 100
        vh  = float(v.iloc[-1]) if float(v.iloc[-1]) > 0 else float(v.iloc[-2])
        vp  = float(v.iloc[-20:].mean())
        mv, sv, hv, me = calc_macd(c)
        av, dip, dim, ae = calc_adx(h, l, c)
        return {
            "ticker": ticker,
            "nombre": s.info.get("shortName", ticker),
            "precio": precio, "prev": prev, "pct": pct, "es_pm": es_pm,
            "sma8":   sma(c, 8),   "sma20":  sma(c, 20),
            "sma50":  sma(c, 50),  "sma200": sma(c, 200),
            "ema10":  float(ema(c, 10).iloc[-1]),
            "ema20":  float(ema(c, 20).iloc[-1]),
            "ema50":  float(ema(c, 50).iloc[-1]),
            "ema200": float(ema(c, 200).iloc[-1]),
            "rsi": calc_rsi(c),
            "macd": mv, "macd_s": sv, "macd_h": hv, "macd_e": me,
            "adx": av, "dip": dip, "dim": dim, "adx_e": ae,
            "vh": vh, "vp": vp, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

# ── Análisis del setup ─────────────────────────────────────────
def analizar(d):
    p, s8, s20, s50, s200 = d["precio"], d["sma8"], d["sma20"], d["sma50"], d["sma200"]
    c1 = bool(p   > s8)   if s8   else False
    c2 = bool(s8  > s20)  if s20  else False
    c3 = bool(s20 > s50)  if s50  else False
    c4 = bool(s50 > s200) if s200 else False
    fan = sum([c1, c2, c3, c4])
    en_rango = CONFIG["price_min"] <= p <= CONFIG["price_max"]
    rsi_ok   = CONFIG["rsi_min"] <= d["rsi"] <= CONFIG["rsi_max"] if d["rsi"] else False
    vol_r    = d["vh"] / d["vp"] if d["vp"] > 0 else 0
    estado   = ("ABANICO COMPLETO ✅" if fan == 4 and en_rango
                else "ABANICO PARCIAL ⚠️" if fan >= 2 and en_rango
                else "ABANICO ROTO ❌")
    return {
        "fan": fan, "estado": estado,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4,
        "en_rango": en_rango, "rsi_ok": rsi_ok, "vol_r": vol_r,
        "adx_ok":   (d["adx"] >= CONFIG["adx_min_trend"]) if d["adx"] else False,
        "macd_bull": d["macd_e"] in ("Bullish", "Weak Bull"),
        "e10":  p > d["ema10"], "e20": p > d["ema20"],
        "e50":  p > d["ema50"], "e200": p > d["ema200"]
    }

def posicion(precio):
    cap = CONFIG["capital_usd"]
    rp  = CONFIG["risk_per_trade_pct"] / 100
    sp  = CONFIG["stop_loss_pct"] / 100
    stop = precio * (1 - sp)
    rx   = precio - stop
    acc  = int((cap * rp) / rx)
    tot  = acc * precio
    perd = acc * rx
    t1   = precio * 1.12
    t2   = precio * 1.22
    rr   = (t1 - precio) / rx
    return {"acc": acc, "tot": tot, "stop": stop,
            "perd": perd, "t1": t1, "t2": t2, "rr": rr, "pct": tot/cap*100}

# ── IA ─────────────────────────────────────────────────────────
def analizar_ia(d, a, pos):
    try:
        client = anthropic.Anthropic(api_key=CONFIG["claude_api_key"])
        prompt = (
            f"Analiza {d['ticker']} bajo Sistema Maestro v4.\n"
            f"Precio: ${d['precio']:.2f} ({d['pct']:+.2f}%) "
            f"{'[PRE-MARKET]' if d['es_pm'] else '[MERCADO ABIERTO]'}\n"
            f"Fan SMA: {a['fan']}/4\n"
            f"EMA10/20/50/200: {'Sí' if a['e10'] else 'No'}/"
            f"{'Sí' if a['e20'] else 'No'}/"
            f"{'Sí' if a['e50'] else 'No'}/"
            f"{'Sí' if a['e200'] else 'No'}\n"
            f"MACD: {d['macd_e']} | RSI: {d['rsi']:.0f} | "
            f"ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"Vol: {a['vol_r']:.1f}x | Pos: {pos['acc']} acc | "
            f"Stop: ${pos['stop']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Responde EXACTAMENTE:\n"
            f"PROBABILIDAD: [0-100]\n"
            f"SEÑAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"RAZON: [2 frases concretas]\n"
            f"ALERTA: [nivel o evento a vigilar]"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=(
                "Analizador Sistema Maestro v4. "
                "MACD bearish penaliza señal. ADX<20 penaliza. "
                "Responde solo en el formato exacto, en español."
            ),
            messages=[{"role": "user", "content": prompt}]
        )
        txt = msg.content[0].text
        pr, se, ra, al = 0, "NO APLICA", "", ""
        for ln in txt.splitlines():
            if ln.startswith("PROBABILIDAD:"):
                try: pr = int(ln.split(":")[1].strip().replace("%", ""))
                except: pass
            elif ln.startswith("SEÑAL:"):   se = ln.split(":", 1)[1].strip()
            elif ln.startswith("RAZON:"):   ra = ln.split(":", 1)[1].strip()
            elif ln.startswith("ALERTA:"):  al = ln.split(":", 1)[1].strip()
        return {"prob": pr, "señal": se, "razon": ra, "alerta": al}
    except Exception as e:
        return {"prob": 0, "señal": "ERROR", "razon": str(e), "alerta": ""}

# ── WhatsApp ───────────────────────────────────────────────────
def send_wa(msg):
    url = (
        f"https://api.callmebot.com/whatsapp.php?"
        f"phone={CONFIG['whatsapp_number']}"
        f"&text={quote(msg)}"
        f"&apikey={CONFIG['callmebot_apikey']}"
    )
    try:
        return requests.get(url, timeout=15).status_code == 200
    except:
        return False

def build_msg(d, a, pos, ia):
    fecha  = datetime.now().strftime("%d/%m/%Y %H:%M")
    pm_tag = " [PRE-MARKET]" if d.get("es_pm") else " [MERCADO ABIERTO]"
    emoji  = {"ENTRAR": "🟢", "ESPERAR": "🟡", "NO APLICA": "🔴"}.get(ia["señal"], "⚪")
    ef     = "✅" if a["fan"] == 4 else "⚠️"
    return (
        f"🤖 *SISTEMA LU — SEÑAL*\n{fecha}{pm_tag}\n\n"
        f"*{d['ticker']}* · {d.get('nombre', '')}\n"
        f"💰 ${d['precio']:.2f} ({d['pct']:+.2f}%)\n\n"
        f"{ef} *Abanico SMA ({a['fan']}/4)*\n"
        f"{'✅' if a['c1'] else '❌'} Precio > SMA8   (${d['sma8']:.2f})\n"
        f"{'✅' if a['c2'] else '❌'} SMA8  > SMA20   (${d['sma20']:.2f})\n"
        f"{'✅' if a['c3'] else '❌'} SMA20 > SMA50   (${d['sma50']:.2f})\n"
        f"{'✅' if a['c4'] else '❌'} SMA50 > SMA200  (${d['sma200']:.2f})\n\n"
        f"📊 *Indicadores*\n"
        f"┌───────────────────────┐\n"
        f"│ EMA10   {'Yes ✅' if a['e10']  else 'No  ❌'}\n"
        f"│ EMA20   {'Yes ✅' if a['e20']  else 'No  ❌'}\n"
        f"│ EMA50   {'Yes ✅' if a['e50']  else 'No  ❌'}\n"
        f"│ EMA200  {'Yes ✅' if a['e200'] else 'No  ❌'}\n"
        f"│ MACD    {d['macd_e']}\n"
        f"│ RSI     {'Bullish ✅' if a['rsi_ok'] else 'OB ⚠️' if d['rsi'] > 72 else 'Weak ❌'} ({d['rsi']:.0f})\n"
        f"│ ADX     {d['adx_e']} ({d['adx']:.0f})\n"
        f"└───────────────────────┘\n"
        f"Vol: {a['vol_r']:.1f}x prom {'✅' if a['vol_r'] >= 1.5 else '⚠️'}\n\n"
        f"📐 *Tu posición*\n"
        f"Comprar: *{pos['acc']} acciones* (${pos['tot']:.0f})\n"
        f"🛑 Stop loss: ${pos['stop']:.2f}\n"
        f"🎯 Target 1:  ${pos['t1']:.2f}  (+12%)\n"
        f"🎯 Target 2:  ${pos['t2']:.2f}  (+22%)\n"
        f"⚖️ R/R: {pos['rr']:.1f}x  |  Riesgo: ${pos['perd']:.0f}\n\n"
        f"{emoji} *IA — {ia['prob']}%*  ·  {ia['señal']}\n"
        f"{ia['razon']}\n"
        f"⚡ Vigilar: {ia['alerta']}\n\n"
        f"_Sistema Maestro v4 · IBKR_"
    )

# ── Main ───────────────────────────────────────────────────────
def ejecutar_scan():
    hora   = datetime.now().strftime("%H:%M")
    estado = cargar_estado()
    print(f"\n=== SISTEMA LU v3  {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    ya = estado.get("alertados", [])
    if ya: print(f"Ya alertados hoy: {', '.join(ya)}")

    nuevas = 0
    for ticker in CONFIG["watchlist"]:
        if ticker in ya:
            print(f"→ {ticker}: ya alertado hoy"); continue

        print(f"→ {ticker} ...", end=" ", flush=True)
        d = obtener_datos(ticker)
        if d.get("error"):
            print(f"ERROR: {d['error']}"); continue

        print(f"${d['precio']:.2f}  MACD:{d['macd_e']}  ADX:{d['adx_e']}")
        a = analizar(d)
        print(f"   Fan {a['fan']}/4 — {a['estado']}")

        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"]:
            continue

        pos = posicion(d["precio"])
        ia  = analizar_ia(d, a, pos)
        print(f"   IA: {ia['prob']}% → {ia['señal']}")

        msg = build_msg(d, a, pos, ia)
        ok  = send_wa(msg)

        if ok:
            nuevas += 1
            ya.append(ticker)
            guardar_estado(estado)
            print(f"   ✅ WhatsApp enviado")
        else:
            print(f"   ⚠️ Error enviando WhatsApp")

    print(f"\n=== Scan {hora} listo — {nuevas} alertas nuevas ===")

    if nuevas == 0 and hora in ("13:00", "13:01"):
        send_wa(
            f"🤖 *SISTEMA LU — Sin setups hoy*\n"
            f"{datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"Revisé {len(CONFIG['watchlist'])} tickers.\n"
            f"Ninguno cumple el abanico SMA mínimo hoy.\n\n"
            f"✅ Estás protegida — esperar es la estrategia.\n\n"
            f"_Sistema Maestro v4_"
        )

if __name__ == "__main__":
    ejecutar_scan()
