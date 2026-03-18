"""
SISTEMA LU — BOT v3.1
Escanea TODO el universo de acciones via Finviz (no solo una lista fija).
Filtra: precio $10-$150, volumen >1M, sobre SMA200 → luego verifica abanico SMA completo.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date
from urllib.parse import quote

# ── Claves (vienen de GitHub Secrets) ────────────────────────
WHATSAPP_NUMBER  = os.environ.get("WHATSAPP_NUMBER", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

if not WHATSAPP_NUMBER or not CALLMEBOT_APIKEY:
    print("ERROR: Falta WHATSAPP_NUMBER o CALLMEBOT_APIKEY en GitHub Secrets")
    exit(1)

import anthropic

# ── Configuración ─────────────────────────────────────────────
CONFIG = {
    "capital_usd":        33140,
    "risk_per_trade_pct": 2.0,
    "stop_loss_pct":      6.0,
    "price_min":          10.0,
    "price_max":          150.0,
    "rsi_min":            45,
    "rsi_max":            72,
    "min_volume":         1_000_000,
    "min_fan_to_alert":   3,
    "adx_min_trend":      20,
    "max_tickers_scan":   150,   # máximo tickers a revisar por escaneo
    "max_alertas_dia":    5,     # máximo alertas por día para no saturar WA
    "state_file":         "/tmp/lu_alertas_hoy.json",

    # Tickers prioritarios que SIEMPRE se revisan primero
    "watchlist_prioritaria": [
        "KGC", "OXY", "SLB", "BTU", "PAAS",
        "SQM", "FCX", "HIMS", "EW",  "DAR",
        "HAL", "BKR", "GDX", "GOAU",
        "JEPI", "SCHD", "IBIT",
    ]
}

# ── Anti-spam ─────────────────────────────────────────────────
def cargar_estado():
    try:
        with open(CONFIG["state_file"], "r") as f:
            e = json.load(f)
        if e.get("fecha") != str(date.today()):
            return {"fecha": str(date.today()), "alertados": [], "count": 0}
        return e
    except:
        return {"fecha": str(date.today()), "alertados": [], "count": 0}

def guardar_estado(e):
    try:
        with open(CONFIG["state_file"], "w") as f:
            json.dump(e, f)
    except:
        pass

# ── Obtener tickers del universo completo via Finviz ──────────
def obtener_universo_finviz():
    """
    Usa el screener de Finviz para obtener acciones que cumplen
    los filtros básicos: precio $10-$150, vol >1M, sobre SMA200.
    Devuelve lista de tickers.
    """
    print("\nObteniendo universo de Finviz...")
    try:
        # Finviz screener: precio 10-150, vol>1M, sobre SMA200, sobre SMA50
        url = (
            "https://finviz.com/screener.ashx?v=111"
            "&f=sh_price_o10,sh_price_u150"
            ",sh_avgvol_o1000"
            ",ta_sma200_pa"
            ",ta_sma50_pa"
            "&ft=4"
            "&o=-volume"
            "&r=1"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code != 200:
            print(f"Finviz error {resp.status_code}, usando watchlist prioritaria")
            return CONFIG["watchlist_prioritaria"]

        # Parsear tickers de la respuesta HTML
        from html.parser import HTMLParser

        class FinvizParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tickers = []
                self.in_ticker = False

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                # Finviz usa class="screener-link-primary" para los tickers
                if (tag == "a" and
                    attrs_dict.get("class") == "screener-link-primary"):
                    self.in_ticker = True

            def handle_data(self, data):
                if self.in_ticker:
                    t = data.strip()
                    if t and t.isalpha() and len(t) <= 5:
                        self.tickers.append(t)
                    self.in_ticker = False

        parser = FinvizParser()
        parser.feed(resp.text)
        tickers_finviz = parser.tickers

        if len(tickers_finviz) < 5:
            print("Pocos tickers de Finviz, usando watchlist prioritaria")
            return CONFIG["watchlist_prioritaria"]

        print(f"Finviz devolvió {len(tickers_finviz)} tickers")

        # Combinar: prioritarios primero + universo Finviz (sin duplicados)
        combinados = list(CONFIG["watchlist_prioritaria"])
        for t in tickers_finviz:
            if t not in combinados:
                combinados.append(t)

        # Limitar al máximo configurado
        resultado = combinados[:CONFIG["max_tickers_scan"]]
        print(f"Total a escanear: {len(resultado)} tickers")
        return resultado

    except Exception as e:
        print(f"Error obteniendo universo: {e}")
        print("Usando watchlist prioritaria")
        return CONFIG["watchlist_prioritaria"]

# ── Indicadores técnicos ──────────────────────────────────────
def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def sma(s, p):
    return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p + 1: return None
    d  = c.diff()
    g  = d.where(d > 0, 0.0)
    l  = -d.where(d < 0, 0.0)
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
    if   mv > sv and hv > 0:  estado = "Bullish"
    elif mv > sv:             estado = "Weak Bull"
    elif mv < sv and hv < 0:  estado = "Bearish"
    else:                     estado = "Weak Bear"
    return mv, sv, hv, estado

def calc_adx(h, l, c, p=14):
    if len(h) < p * 2: return None, None, None, "N/A"
    tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    dp  = h.diff(); dm = -l.diff()
    dp  = dp.where((dp > dm) & (dp > 0), 0.0)
    dm  = dm.where((dm > dp) & (dm > 0), 0.0)
    atr = tr.ewm(alpha=1/p, adjust=False).mean()
    dip = 100 * dp.ewm(alpha=1/p, adjust=False).mean() / atr
    dim = 100 * dm.ewm(alpha=1/p, adjust=False).mean() / atr
    dx  = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
    av  = float(dx.ewm(alpha=1/p, adjust=False).mean().iloc[-1])
    e   = "Strong Trend" if av >= 25 else "Moderate" if av >= 20 else "Weak"
    return av, float(dip.iloc[-1]), float(dim.iloc[-1]), e

# ── Obtener datos de un ticker ────────────────────────────────
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
            pm = float(getattr(fi, "pre_market_price", None) or 0)
            if pm > 0:
                precio = pm; es_pm = True
        except: pass
        pct = (precio - prev) / prev * 100
        vh  = float(v.iloc[-1]) if float(v.iloc[-1]) > 0 else float(v.iloc[-2])
        vp  = float(v.iloc[-20:].mean())
        mv, sv, hv, me = calc_macd(c)
        av, dip, dim, ae = calc_adx(h, l, c)
        nombre = ticker
        try: nombre = s.info.get("shortName", ticker)
        except: pass
        return {
            "ticker": ticker, "nombre": nombre,
            "precio": precio, "prev": prev, "pct": pct, "es_pm": es_pm,
            "sma8":   sma(c, 8),   "sma20":  sma(c, 20),
            "sma50":  sma(c, 50),  "sma200": sma(c, 200),
            "ema10":  float(ema(c, 10).iloc[-1]),
            "ema20":  float(ema(c, 20).iloc[-1]),
            "ema50":  float(ema(c, 50).iloc[-1]),
            "ema200": float(ema(c, 200).iloc[-1]),
            "rsi":  calc_rsi(c),
            "macd": mv, "macd_s": sv, "macd_h": hv, "macd_e": me,
            "adx":  av, "dip": dip, "dim": dim, "adx_e": ae,
            "vh": vh, "vp": vp, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

# ── Análisis del setup ────────────────────────────────────────
def analizar(d):
    p    = d["precio"]
    s8, s20, s50, s200 = d["sma8"], d["sma20"], d["sma50"], d["sma200"]
    c1 = bool(p   > s8)   if s8   else False
    c2 = bool(s8  > s20)  if s20  else False
    c3 = bool(s20 > s50)  if s50  else False
    c4 = bool(s50 > s200) if s200 else False
    fan      = sum([c1, c2, c3, c4])
    en_rango = CONFIG["price_min"] <= p <= CONFIG["price_max"]
    rsi_ok   = CONFIG["rsi_min"] <= d["rsi"] <= CONFIG["rsi_max"] if d["rsi"] else False
    vol_r    = d["vh"] / d["vp"] if d["vp"] > 0 else 0
    estado   = ("ABANICO COMPLETO" if fan == 4 and en_rango
                else "ABANICO PARCIAL" if fan >= 2 and en_rango
                else "ABANICO ROTO")
    return {
        "fan": fan, "estado": estado,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4,
        "en_rango": en_rango, "rsi_ok": rsi_ok, "vol_r": vol_r,
        "e10": p > d["ema10"], "e20": p > d["ema20"],
        "e50": p > d["ema50"], "e200": p > d["ema200"]
    }

def posicion(precio):
    cap  = CONFIG["capital_usd"]
    rp   = CONFIG["risk_per_trade_pct"] / 100
    sp   = CONFIG["stop_loss_pct"] / 100
    stop = precio * (1 - sp)
    rx   = precio - stop
    acc  = max(1, int((cap * rp) / rx))
    tot  = acc * precio
    perd = acc * rx
    t1   = precio * 1.12
    t2   = precio * 1.22
    rr   = (t1 - precio) / rx
    return {"acc": acc, "tot": tot, "stop": stop,
            "perd": perd, "t1": t1, "t2": t2, "rr": rr}

# ── IA ────────────────────────────────────────────────────────
def analizar_ia(d, a, pos):
    if not CLAUDE_API_KEY:
        return {"prob": 0, "señal": "SIN IA", "razon": "Sin API key", "alerta": ""}
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v  = d['rsi'] if d['rsi'] else 0
        prompt = (
            f"Analiza {d['ticker']} bajo Sistema Maestro v4.\n"
            f"Precio: ${d['precio']:.2f} ({d['pct']:+.2f}%)\n"
            f"Fan SMA: {a['fan']}/4\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | "
            f"ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"Vol: {a['vol_r']:.1f}x | Acc: {pos['acc']} | "
            f"Stop: ${pos['stop']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Responde EXACTAMENTE:\n"
            f"PROBABILIDAD: [0-100]\n"
            f"SENAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"RAZON: [max 2 frases]\n"
            f"ALERTA: [nivel o evento clave]"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=250,
            system="Analizador Sistema Maestro v4. Responde formato exacto, español.",
            messages=[{"role": "user", "content": prompt}]
        )
        txt = msg.content[0].text
        pr, se, ra, al = 0, "ESPERAR", "", ""
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr = int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SENAL:") or ln.upper().startswith("SEÑAL:"):
                se = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:") or ln.upper().startswith("RAZÓN:"):
                ra = ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"):
                al = ln.split(":",1)[1].strip()
        return {"prob": pr, "señal": se, "razon": ra, "alerta": al}
    except Exception as e:
        return {"prob": 0, "señal": "ERROR", "razon": str(e)[:80], "alerta": ""}

# ── WhatsApp ──────────────────────────────────────────────────
def send_wa(msg):
    try:
        url = (
            f"https://api.callmebot.com/whatsapp.php?"
            f"phone={WHATSAPP_NUMBER}"
            f"&text={quote(msg)}"
            f"&apikey={CALLMEBOT_APIKEY}"
        )
        r = requests.get(url, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"Error WhatsApp: {e}")
        return False

def build_msg(d, a, pos, ia):
    fecha  = datetime.now().strftime("%d/%m/%Y %H:%M")
    pm_tag = " [PRE-MARKET]" if d.get("es_pm") else ""
    emoji  = {"ENTRAR":"🟢","ESPERAR":"🟡","NO APLICA":"🔴"}.get(ia["señal"],"⚪")
    ef     = "✅" if a["fan"] == 4 else "⚠️"
    rsi_v  = d['rsi'] if d['rsi'] else 0
    return (
        f"🤖 *SISTEMA LU — SEÑAL*\n"
        f"{fecha}{pm_tag}\n\n"
        f"*{d['ticker']}*  {d.get('nombre','')}\n"
        f"💰 ${d['precio']:.2f}  ({d['pct']:+.1f}%)\n\n"
        f"{ef} *Abanico SMA {a['fan']}/4*\n"
        f"{'✅' if a['c1'] else '❌'} Precio > SMA8   ${d['sma8']:.2f}\n"
        f"{'✅' if a['c2'] else '❌'} SMA8  > SMA20   ${d['sma20']:.2f}\n"
        f"{'✅' if a['c3'] else '❌'} SMA20 > SMA50   ${d['sma50']:.2f}\n"
        f"{'✅' if a['c4'] else '❌'} SMA50 > SMA200  ${d['sma200']:.2f}\n\n"
        f"📊 *Indicadores*\n"
        f"MACD: {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f}  {'✅' if a['rsi_ok'] else '⚠️'}\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        f"Vol:  {a['vol_r']:.1f}x  {'✅' if a['vol_r']>=1.5 else '⚠️'}\n\n"
        f"📐 *Tu posición*\n"
        f"Comprar: *{pos['acc']} acciones*  (${pos['tot']:.0f})\n"
        f"🛑 Stop:     ${pos['stop']:.2f}\n"
        f"🎯 Target 1: ${pos['t1']:.2f}  +12%\n"
        f"🎯 Target 2: ${pos['t2']:.2f}  +22%\n"
        f"⚖️ R/R: {pos['rr']:.1f}x   Riesgo: ${pos['perd']:.0f}\n\n"
        f"{emoji} *IA {ia['prob']}%  {ia['señal']}*\n"
        f"{ia['razon']}\n"
        f"⚡ {ia['alerta']}\n\n"
        f"_Sistema Maestro v4_"
    )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"=== SISTEMA LU v3.1  {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")

    estado = cargar_estado()
    ya     = estado.get("alertados", [])
    count  = estado.get("count", 0)

    if count >= CONFIG["max_alertas_dia"]:
        print(f"Máximo de {CONFIG['max_alertas_dia']} alertas del día alcanzado. Fin.")
        return

    if ya:
        print(f"Ya alertados hoy: {', '.join(ya)}")

    # Obtener universo completo desde Finviz
    tickers = obtener_universo_finviz()

    # Quitar los ya alertados hoy
    tickers = [t for t in tickers if t not in ya]
    print(f"Tickers a revisar esta vuelta: {len(tickers)}")

    nuevas = 0

    for ticker in tickers:
        # Parar si llegamos al máximo de alertas del día
        if count + nuevas >= CONFIG["max_alertas_dia"]:
            print(f"Máximo de alertas diarias alcanzado ({CONFIG['max_alertas_dia']}). Fin.")
            break

        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)

        if d.get("error"):
            print(f"skip ({d['error'][:40]})")
            continue

        rsi_str = f"{d['rsi']:.0f}" if d['rsi'] else "N/A"
        print(f"${d['precio']:.2f} RSI:{rsi_str} MACD:{d['macd_e']}")

        a = analizar(d)

        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"]:
            continue

        print(f"  *** FAN {a['fan']}/4 — {a['estado']} — analizando con IA...")

        pos = posicion(d["precio"])
        ia  = analizar_ia(d, a, pos)
        print(f"  IA: {ia['prob']}%  {ia['señal']}")

        msg = build_msg(d, a, pos, ia)
        ok  = send_wa(msg)

        if ok:
            nuevas += 1
            ya.append(ticker)
            estado["alertados"] = ya
            estado["count"]     = count + nuevas
            guardar_estado(estado)
            print(f"  ✅ WhatsApp enviado ({count + nuevas}/{CONFIG['max_alertas_dia']} hoy)")
        else:
            print(f"  ⚠️ Error WhatsApp")

    print(f"\n=== Fin: {nuevas} alertas nuevas esta vuelta ===")

    # Mensaje de sin setups solo en el primer scan del día (8am)
    hora_actual = datetime.now().hour
    if nuevas == 0 and hora_actual == 13 and count == 0:
        send_wa(
            f"🤖 *SISTEMA LU*\n"
            f"{datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"Revisé {len(tickers)} tickers del mercado.\n"
            f"Sin setups válidos esta mañana.\n\n"
            f"✅ Estás protegida — esperar es correcto.\n"
            f"_Sistema Maestro v4_"
        )

if __name__ == "__main__":
    main()
