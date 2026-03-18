"""
SISTEMA LU - BOT v3.2
- Precio real corregido (sin ajuste automatico de yfinance)
- Encoding de caracteres especiales corregido
- Timeframe: diario (1d) - correcto para swing trading
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date
from urllib.parse import quote

WHATSAPP_NUMBER  = os.environ.get("WHATSAPP_NUMBER", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

if not WHATSAPP_NUMBER or not CALLMEBOT_APIKEY:
    print("ERROR: Falta WHATSAPP_NUMBER o CALLMEBOT_APIKEY en GitHub Secrets")
    exit(1)

import anthropic

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
    "max_tickers_scan":   150,
    "max_alertas_dia":    5,
    "state_file":         "/tmp/lu_alertas_hoy.json",
    "watchlist_prioritaria": [
        "KGC", "OXY", "SLB", "BTU", "PAAS",
        "SQM", "FCX", "HIMS", "EW",  "DAR",
        "HAL", "BKR", "GDX", "GOAU",
        "JEPI", "SCHD", "IBIT",
    ]
}

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

def obtener_universo_finviz():
    print("\nObteniendo universo de Finviz...")
    try:
        url = (
            "https://finviz.com/screener.ashx?v=111"
            "&f=sh_price_o10,sh_price_u150"
            ",sh_avgvol_o1000"
            ",ta_sma200_pa"
            ",ta_sma50_pa"
            "&ft=4&o=-volume&r=1"
        )
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code != 200:
            print(f"Finviz error {resp.status_code}, usando watchlist")
            return CONFIG["watchlist_prioritaria"]

        from html.parser import HTMLParser

        class FP(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tickers = []; self.capture = False
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if tag == "a" and d.get("class") == "screener-link-primary":
                    self.capture = True
            def handle_data(self, data):
                if self.capture:
                    t = data.strip()
                    if t and t.isalpha() and len(t) <= 5:
                        self.tickers.append(t)
                    self.capture = False

        p = FP(); p.feed(resp.text)
        tickers = p.tickers

        if len(tickers) < 5:
            return CONFIG["watchlist_prioritaria"]

        print(f"Finviz: {len(tickers)} tickers")
        combinados = list(CONFIG["watchlist_prioritaria"])
        for t in tickers:
            if t not in combinados:
                combinados.append(t)
        return combinados[:CONFIG["max_tickers_scan"]]

    except Exception as e:
        print(f"Error Finviz: {e} - usando watchlist")
        return CONFIG["watchlist_prioritaria"]

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def sma_val(s, p):
    return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p + 1: return None
    d  = c.diff()
    g  = d.where(d > 0, 0.0); l = -d.where(d < 0, 0.0)
    ag = g.ewm(com=p-1, min_periods=p).mean()
    al = l.ewm(com=p-1, min_periods=p).mean()
    rs = ag / al
    return float((100 - (100 / (1 + rs))).iloc[-1])

def calc_macd(c):
    if len(c) < 35: return None, None, None, "N/A"
    ml = ema(c, 12) - ema(c, 26); sl = ema(ml, 9); hl = ml - sl
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

def obtener_datos(ticker):
    try:
        s = yf.Ticker(ticker)

        # ── CLAVE: auto_adjust=False para precios reales sin distorsion ──
        hist = s.history(
            period="1y",
            interval="1d",
            auto_adjust=False,   # <-- evita que yfinance distorsione los precios
            prepost=False        # <-- solo mercado regular, sin pre/post market
        )

        if hist.empty or len(hist) < 210:
            return {"ticker": ticker, "error": "Datos insuficientes"}

        c = hist["Close"]
        h = hist["High"]
        l = hist["Low"]
        v = hist["Volume"]

        # Precio de cierre mas reciente (sin distorsion)
        precio = float(c.iloc[-1])
        prev   = float(c.iloc[-2])

        # Intentar precio pre-market por separado
        es_pm = False
        try:
            fi = s.fast_info
            pm = float(getattr(fi, "pre_market_price", None) or 0)
            # Solo usar pre-market si es razonable (dentro del 15% del cierre)
            if pm > 0 and abs(pm - precio) / precio < 0.15:
                precio = pm
                es_pm  = True
        except:
            pass

        pct = (precio - prev) / prev * 100
        vh  = float(v.iloc[-1]) if float(v.iloc[-1]) > 0 else float(v.iloc[-2])
        vp  = float(v.iloc[-20:].mean())

        mv, sv, hv_macd, me = calc_macd(c)
        av, dip, dim, ae    = calc_adx(h, l, c)

        nombre = ticker
        try:
            info   = s.info
            nombre = info.get("shortName", ticker)
        except:
            pass

        return {
            "ticker": ticker, "nombre": nombre,
            "precio": precio, "prev": prev, "pct": pct, "es_pm": es_pm,
            "sma8":   sma_val(c, 8),   "sma20":  sma_val(c, 20),
            "sma50":  sma_val(c, 50),  "sma200": sma_val(c, 200),
            "ema10":  float(ema(c, 10).iloc[-1]),
            "ema20":  float(ema(c, 20).iloc[-1]),
            "ema50":  float(ema(c, 50).iloc[-1]),
            "ema200": float(ema(c, 200).iloc[-1]),
            "rsi":  calc_rsi(c),
            "macd": mv, "macd_s": sv, "macd_h": hv_macd, "macd_e": me,
            "adx":  av, "dip": dip, "dim": dim, "adx_e": ae,
            "vh": vh, "vp": vp, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

def analizar(d):
    p = d["precio"]
    s8, s20, s50, s200 = d["sma8"], d["sma20"], d["sma50"], d["sma200"]
    c1 = bool(p   > s8)   if s8   else False
    c2 = bool(s8  > s20)  if s20  else False
    c3 = bool(s20 > s50)  if s50  else False
    c4 = bool(s50 > s200) if s200 else False
    fan = sum([c1, c2, c3, c4])
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

def analizar_ia(d, a, pos):
    if not CLAUDE_API_KEY:
        return {"prob": 0, "senal": "SIN IA", "razon": "Sin API key", "alerta": ""}
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v  = d["rsi"] if d["rsi"] else 0
        prompt = (
            f"Analiza {d['ticker']} bajo Sistema Maestro v4.\n"
            f"Precio real: ${d['precio']:.2f} ({d['pct']:+.2f}%)\n"
            f"Timeframe: diario (1d) - swing trading 5-10 dias\n"
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
            system=(
                "Analizador Sistema Maestro v4 swing trading. "
                "Timeframe diario. MACD bearish penaliza. "
                "RSI>72 penaliza. ADX<20 penaliza. "
                "Responde solo en formato exacto, en espanol."
            ),
            messages=[{"role": "user", "content": prompt}]
        )
        txt = msg.content[0].text
        pr, se, ra, al = 0, "ESPERAR", "", ""
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr = int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif "SENAL:" in ln.upper() or "SEÑAL:" in ln.upper():
                se = ln.split(":",1)[1].strip()
            elif "RAZON:" in ln.upper() or "RAZON:" in ln.upper():
                ra = ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"):
                al = ln.split(":",1)[1].strip()
        return {"prob": pr, "senal": se, "razon": ra, "alerta": al}
    except Exception as e:
        return {"prob": 0, "senal": "ERROR", "razon": str(e)[:80], "alerta": ""}

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
    emoji  = {"ENTRAR":"verde","ESPERAR":"amarillo","NO APLICA":"rojo"}.get(ia["senal"],"")
    ef     = "OK" if a["fan"] == 4 else "PARCIAL"
    rsi_v  = d["rsi"] if d["rsi"] else 0

    # Señal con palabras (evita problemas de encoding con emojis)
    senal_txt = {
        "ENTRAR":    "ENTRAR",
        "ESPERAR":   "ESPERAR",
        "NO APLICA": "NO APLICA"
    }.get(ia["senal"], ia["senal"])

    return (
        f"SISTEMA LU - SENAL\n"
        f"{fecha}{pm_tag}\n"
        f"Timeframe: DIARIO (swing 5-10 dias)\n\n"
        f"{d['ticker']} - {d.get('nombre','')}\n"
        f"Precio: ${d['precio']:.2f} ({d['pct']:+.1f}%)\n\n"
        f"Abanico SMA {a['fan']}/4 - {ef}\n"
        f"{'SI' if a['c1'] else 'NO'} Precio > SMA8   ${d['sma8']:.2f}\n"
        f"{'SI' if a['c2'] else 'NO'} SMA8  > SMA20   ${d['sma20']:.2f}\n"
        f"{'SI' if a['c3'] else 'NO'} SMA20 > SMA50   ${d['sma50']:.2f}\n"
        f"{'SI' if a['c4'] else 'NO'} SMA50 > SMA200  ${d['sma200']:.2f}\n\n"
        f"Indicadores\n"
        f"MACD: {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f} ({'OK' if a['rsi_ok'] else 'ALTO' if rsi_v > 72 else 'BAJO'})\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        f"Vol:  {a['vol_r']:.1f}x prom\n\n"
        f"Tu posicion\n"
        f"Comprar: {pos['acc']} acciones (${pos['tot']:.0f})\n"
        f"Stop loss: ${pos['stop']:.2f}\n"
        f"Target 1:  ${pos['t1']:.2f}  (+12%)\n"
        f"Target 2:  ${pos['t2']:.2f}  (+22%)\n"
        f"R/R: {pos['rr']:.1f}x  Riesgo: ${pos['perd']:.0f}\n\n"
        f"IA {ia['prob']}% - {senal_txt}\n"
        f"{ia['razon']}\n"
        f"Vigilar: {ia['alerta']}\n\n"
        f"Sistema Maestro v4 - Diario"
    )

def main():
    print(f"=== SISTEMA LU v3.2  {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")

    estado = cargar_estado()
    ya     = estado.get("alertados", [])
    count  = estado.get("count", 0)

    if count >= CONFIG["max_alertas_dia"]:
        print(f"Maximo de alertas del dia alcanzado ({CONFIG['max_alertas_dia']}). Fin.")
        return

    tickers = obtener_universo_finviz()
    tickers = [t for t in tickers if t not in ya]
    print(f"Tickers a revisar: {len(tickers)}")

    nuevas = 0

    for ticker in tickers:
        if count + nuevas >= CONFIG["max_alertas_dia"]:
            break

        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)

        if d.get("error"):
            print(f"skip"); continue

        rsi_str = f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        print(f"${d['precio']:.2f}  RSI:{rsi_str}  MACD:{d['macd_e']}")

        a = analizar(d)

        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"]:
            continue

        print(f"  *** FAN {a['fan']}/4 - {a['estado']}")

        pos = posicion(d["precio"])
        ia  = analizar_ia(d, a, pos)
        print(f"  IA: {ia['prob']}% - {ia['senal']}")

        msg = build_msg(d, a, pos, ia)
        ok  = send_wa(msg)

        if ok:
            nuevas += 1
            ya.append(ticker)
            estado["alertados"] = ya
            estado["count"]     = count + nuevas
            guardar_estado(estado)
            print(f"  OK WhatsApp enviado")
        else:
            print(f"  ERROR WhatsApp")

    print(f"\n=== Fin: {nuevas} alertas nuevas ===")

    hora_actual = datetime.now().hour
    if nuevas == 0 and hora_actual == 13 and count == 0:
        send_wa(
            f"SISTEMA LU\n"
            f"{datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"Revise {len(tickers)} tickers del mercado.\n"
            f"Sin setups validos esta manana.\n\n"
            f"Estas protegida - esperar es correcto.\n"
            f"Sistema Maestro v4 - Diario"
        )

if __name__ == "__main__":
    main()
