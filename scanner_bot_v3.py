"""
SISTEMA LU — BOT v3.2
Correcciones: validación de datos malos de yfinance, riesgo $200, cálculos corregidos.
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
    "risk_per_trade_pct": 0.60,   # 0.60% de $33,140 = ~$200 por trade
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

# ── Universo Finviz ───────────────────────────────────────────
def obtener_universo():
    print("\nObteniendo tickers de Finviz...")
    try:
        url = (
            "https://finviz.com/screener.ashx?v=111"
            "&f=sh_price_o10,sh_price_u150"
            ",sh_avgvol_o1000"
            ",ta_sma200_pa,ta_sma50_pa"
            "&ft=4&o=-volume&r=1"
        )
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=30)

        tickers_finviz = []
        if resp.status_code == 200:
            from html.parser import HTMLParser
            class FP(HTMLParser):
                def __init__(self):
                    super().__init__(); self.tickers=[]; self._next=False
                def handle_starttag(self, tag, attrs):
                    d = dict(attrs)
                    if tag=="a" and d.get("class")=="screener-link-primary":
                        self._next=True
                def handle_data(self, data):
                    if self._next:
                        t=data.strip()
                        if t and t.isalpha() and len(t)<=5:
                            self.tickers.append(t)
                        self._next=False
            p = FP(); p.feed(resp.text)
            tickers_finviz = p.tickers
            print(f"Finviz: {len(tickers_finviz)} tickers")

        combinados = list(CONFIG["watchlist_prioritaria"])
        for t in tickers_finviz:
            if t not in combinados:
                combinados.append(t)

        resultado = combinados[:CONFIG["max_tickers_scan"]]
        print(f"Total a escanear: {len(resultado)}")
        return resultado

    except Exception as e:
        print(f"Error Finviz: {e} — usando watchlist prioritaria")
        return CONFIG["watchlist_prioritaria"]

# ── Indicadores ───────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def sma(s, p): return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p+1: return None
    d=c.diff(); g=d.where(d>0,0.0); l=-d.where(d<0,0.0)
    ag=g.ewm(com=p-1,min_periods=p).mean()
    al=l.ewm(com=p-1,min_periods=p).mean()
    rs=ag/al
    return float((100-(100/(1+rs))).iloc[-1])

def calc_macd(c):
    if len(c)<35: return None,None,None,"N/A"
    ml=ema(c,12)-ema(c,26); sl=ema(ml,9); hl=ml-sl
    mv,sv,hv=float(ml.iloc[-1]),float(sl.iloc[-1]),float(hl.iloc[-1])
    if   mv>sv and hv>0:  e="Bullish"
    elif mv>sv:           e="Weak Bull"
    elif mv<sv and hv<0:  e="Bearish"
    else:                 e="Weak Bear"
    return mv,sv,hv,e

def calc_adx(h,l,c,p=14):
    if len(h)<p*2: return None,None,None,"N/A"
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    dp=h.diff(); dm=-l.diff()
    dp=dp.where((dp>dm)&(dp>0),0.0); dm=dm.where((dm>dp)&(dm>0),0.0)
    atr=tr.ewm(alpha=1/p,adjust=False).mean()
    dip=100*dp.ewm(alpha=1/p,adjust=False).mean()/atr
    dim=100*dm.ewm(alpha=1/p,adjust=False).mean()/atr
    dx=100*(dip-dim).abs()/(dip+dim).replace(0,np.nan)
    av=float(dx.ewm(alpha=1/p,adjust=False).mean().iloc[-1])
    e="Strong Trend" if av>=25 else "Moderate" if av>=20 else "Weak"
    return av,float(dip.iloc[-1]),float(dim.iloc[-1]),e

# ── Obtener datos con validación anti-datos-malos ─────────────
def obtener_datos(ticker):
    try:
        s    = yf.Ticker(ticker)
        hist = s.history(period="1y", interval="1d", prepost=True)
        if hist.empty or len(hist) < 210:
            return {"ticker": ticker, "error": "Datos insuficientes"}

        c, h, l, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]

        # Precio de cierre real (último día completo)
        precio_cierre = float(c.iloc[-1])

        # ── VALIDACIÓN CRÍTICA: detectar precios corruptos ──
        # El precio no puede haber cambiado más del 30% de un día al otro
        # sin ser un evento real (earnings, split, etc.)
        precio_ant = float(c.iloc[-2])
        cambio_pct = abs(precio_cierre - precio_ant) / precio_ant * 100
        if cambio_pct > 30:
            return {"ticker": ticker, "error": f"Cambio extremo {cambio_pct:.0f}% — dato sospechoso"}

        # El precio debe estar en rango razonable
        if precio_cierre < CONFIG["price_min"] or precio_cierre > CONFIG["price_max"]:
            return {"ticker": ticker, "error": f"Precio ${precio_cierre:.2f} fuera de rango"}

        # Intentar precio pre-market
        precio = precio_cierre
        es_pm  = False
        try:
            fi = s.fast_info
            pm = float(getattr(fi, "pre_market_price", None) or 0)
            # Solo usar pre-market si es razonable (±15% del cierre)
            if pm > 0:
                cambio_pm = abs(pm - precio_cierre) / precio_cierre * 100
                if cambio_pm <= 15:
                    precio = pm
                    es_pm  = True
        except: pass

        pct = (precio - precio_ant) / precio_ant * 100
        vh  = float(v.iloc[-1]) if float(v.iloc[-1]) > 0 else float(v.iloc[-2])
        vp  = float(v.iloc[-20:].mean())

        # Validar volumen mínimo
        if vp < CONFIG["min_volume"]:
            return {"ticker": ticker, "error": "Volumen insuficiente"}

        mv,sv,hv_m,me = calc_macd(c)
        av,dip,dim,ae  = calc_adx(h,l,c)

        nombre = ticker
        try: nombre = s.info.get("shortName", ticker) or ticker
        except: pass

        return {
            "ticker": ticker, "nombre": nombre,
            "precio": precio, "precio_cierre": precio_cierre,
            "prev": precio_ant, "pct": pct, "es_pm": es_pm,
            "sma8":  sma(c,8),  "sma20": sma(c,20),
            "sma50": sma(c,50), "sma200":sma(c,200),
            "ema10": float(ema(c,10).iloc[-1]),
            "ema20": float(ema(c,20).iloc[-1]),
            "ema50": float(ema(c,50).iloc[-1]),
            "ema200":float(ema(c,200).iloc[-1]),
            "rsi":  calc_rsi(c),
            "macd": mv, "macd_s": sv, "macd_h": hv_m, "macd_e": me,
            "adx":  av, "dip": dip, "dim": dim, "adx_e": ae,
            "vh": vh, "vp": vp, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)[:60]}

# ── Análisis ──────────────────────────────────────────────────
def analizar(d):
    p    = d["precio"]
    s8,s20,s50,s200 = d["sma8"],d["sma20"],d["sma50"],d["sma200"]

    # Validar que los SMAs son coherentes con el precio actual
    if s8 and abs(p - s8) / p > 0.50:
        return {"fan":0,"estado":"DATOS INCOHERENTES","c1":False,"c2":False,
                "c3":False,"c4":False,"en_rango":False,"rsi_ok":False,
                "vol_r":0,"e10":False,"e20":False,"e50":False,"e200":False}

    c1 = bool(p>s8)    if s8   else False
    c2 = bool(s8>s20)  if s20  else False
    c3 = bool(s20>s50) if s50  else False
    c4 = bool(s50>s200)if s200 else False
    fan      = sum([c1,c2,c3,c4])
    en_rango = CONFIG["price_min"] <= p <= CONFIG["price_max"]
    rsi_ok   = CONFIG["rsi_min"] <= d["rsi"] <= CONFIG["rsi_max"] if d["rsi"] else False
    vol_r    = d["vh"] / d["vp"] if d["vp"] > 0 else 0
    estado   = ("ABANICO COMPLETO" if fan==4 and en_rango
                else "ABANICO PARCIAL" if fan>=2 and en_rango
                else "ABANICO ROTO")
    return {
        "fan":fan,"estado":estado,"c1":c1,"c2":c2,"c3":c3,"c4":c4,
        "en_rango":en_rango,"rsi_ok":rsi_ok,"vol_r":vol_r,
        "e10":p>d["ema10"],"e20":p>d["ema20"],
        "e50":p>d["ema50"],"e200":p>d["ema200"]
    }

def posicion(precio):
    cap  = CONFIG["capital_usd"]
    rp   = CONFIG["risk_per_trade_pct"] / 100
    sp   = CONFIG["stop_loss_pct"] / 100
    stop = round(precio * (1 - sp), 2)
    rx   = precio - stop           # riesgo por acción
    if rx <= 0: rx = precio * 0.06  # fallback
    riesgo_total = cap * rp        # dinero a arriesgar (~$200)
    acc  = max(1, int(riesgo_total / rx))
    tot  = round(acc * precio, 2)
    perd = round(acc * rx, 2)
    t1   = round(precio * 1.12, 2)
    t2   = round(precio * 1.22, 2)
    rr   = round((t1 - precio) / rx, 1)

    # Sanidad: verificar que los targets son mayores al precio
    assert t1 > precio, f"Target 1 {t1} debe ser > precio {precio}"
    assert t2 > precio, f"Target 2 {t2} debe ser > precio {precio}"
    assert stop < precio, f"Stop {stop} debe ser < precio {precio}"

    return {"acc":acc,"tot":tot,"stop":stop,"perd":perd,
            "t1":t1,"t2":t2,"rr":rr,"riesgo_total":round(riesgo_total)}

# ── IA ────────────────────────────────────────────────────────
def analizar_ia(d, a, pos):
    if not CLAUDE_API_KEY:
        return {"prob":0,"señal":"SIN IA","razon":"Sin API key","alerta":""}
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v  = d['rsi'] if d['rsi'] else 0
        prompt = (
            f"Analiza {d['ticker']} ({d.get('nombre','')}) bajo Sistema Maestro v4.\n"
            f"Precio: ${d['precio']:.2f} ({d['pct']:+.2f}%)\n"
            f"Timeframe: DIARIO — swing 5-10 días\n"
            f"Fan SMA: {a['fan']}/4 — {a['estado']}\n"
            f"SMA8 ${d['sma8']:.2f} | SMA20 ${d['sma20']:.2f} | "
            f"SMA50 ${d['sma50']:.2f} | SMA200 ${d['sma200']:.2f}\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | "
            f"ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"Volumen: {a['vol_r']:.1f}x promedio\n"
            f"Stop: ${pos['stop']:.2f} | T1: ${pos['t1']:.2f} | "
            f"T2: ${pos['t2']:.2f} | R/R: {pos['rr']}x\n\n"
            f"Responde EXACTAMENTE en este formato sin variaciones:\n"
            f"PROBABILIDAD: [numero entre 0 y 100]\n"
            f"SENAL: [ENTRAR o ESPERAR o NO APLICA]\n"
            f"RAZON: [exactamente 2 frases]\n"
            f"ALERTA: [un nivel de precio o evento a vigilar]"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=250,
            system=(
                "Eres el analizador del Sistema Maestro v4 de swing trading. "
                "Reglas: Precio>SMA8>SMA20>SMA50>SMA200. "
                "MACD bearish penaliza la señal. ADX<20 indica tendencia débil. "
                "Volumen < 1x promedio penaliza. "
                "Responde SOLO en el formato exacto solicitado, en español. "
                "No agregues texto adicional."
            ),
            messages=[{"role":"user","content":prompt}]
        )
        txt = msg.content[0].text.strip()
        pr,se,ra,al = 0,"ESPERAR","Sin análisis disponible","Revisar manualmente"
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.upper().startswith("PROBABILIDAD:"):
                try: pr=int(''.join(filter(str.isdigit, ln.split(":",1)[1][:5])))
                except: pass
            elif ln.upper().startswith("SENAL:") or ln.upper().startswith("SEÑAL:"):
                se = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:") or ln.upper().startswith("RAZÓN:"):
                ra = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("ALERTA:"):
                al = ln.split(":",1)[1].strip()
        return {"prob":pr,"señal":se,"razon":ra,"alerta":al}
    except Exception as e:
        return {"prob":0,"señal":"ERROR IA","razon":str(e)[:80],"alerta":""}

# ── WhatsApp ──────────────────────────────────────────────────
def send_wa(msg):
    try:
        url=(f"https://api.callmebot.com/whatsapp.php?"
             f"phone={WHATSAPP_NUMBER}&text={quote(msg)}&apikey={CALLMEBOT_APIKEY}")
        r = requests.get(url, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"Error WhatsApp: {e}"); return False

def build_msg(d, a, pos, ia):
    fecha  = datetime.now().strftime("%d/%m/%Y %H:%M")
    pm_tag = " [PRE-MARKET]" if d.get("es_pm") else ""
    emoji  = {"ENTRAR":"🟢","ESPERAR":"🟡","NO APLICA":"🔴"}.get(ia["señal"],"⚪")
    ef     = "✅" if a["fan"]==4 else "⚠️"
    rsi_v  = d['rsi'] if d['rsi'] else 0
    est_rsi = "OK ✅" if a["rsi_ok"] else ("OB ⚠️" if rsi_v>72 else "Bajo ❌")

    # Verificación final de coherencia
    if pos["t1"] <= d["precio"] or pos["t2"] <= d["precio"] or pos["stop"] >= d["precio"]:
        return None  # No enviar mensaje con datos incoherentes

    return (
        f"🤖 *SISTEMA LU — SEÑAL*\n"
        f"{fecha}{pm_tag}\n"
        f"Timeframe: DIARIO (swing 5-10 dias)\n\n"
        f"*{d['ticker']}* — {d.get('nombre','')}\n"
        f"Precio: {d['precio']:.2f} ({d['pct']:+.1f}%)\n\n"
        f"Abanico SMA {a['fan']}/4 — "
        f"{'COMPLETO ✅' if a['fan']==4 else 'PARCIAL ⚠️'}\n"
        f"{'SI' if a['c1'] else 'NO'} Precio > SMA8   {d['sma8']:.2f}\n"
        f"{'SI' if a['c2'] else 'NO'} SMA8  > SMA20   {d['sma20']:.2f}\n"
        f"{'SI' if a['c3'] else 'NO'} SMA20 > SMA50   {d['sma50']:.2f}\n"
        f"{'SI' if a['c4'] else 'NO'} SMA50 > SMA200  {d['sma200']:.2f}\n\n"
        f"Indicadores\n"
        f"MACD: {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f} ({est_rsi})\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        f"Vol:  {a['vol_r']:.1f}x prom "
        f"{'✅' if a['vol_r']>=1.5 else '⚠️ bajo'}\n\n"
        f"Tu posicion (riesgo ~${pos['riesgo_total']})\n"
        f"Comprar: *{pos['acc']} acciones* (${pos['tot']:.0f})\n"
        f"Stop loss:  ${pos['stop']:.2f}  (-6%)\n"
        f"Target 1:  ${pos['t1']:.2f}  (+12%)\n"
        f"Target 2:  ${pos['t2']:.2f}  (+22%)\n"
        f"R/R: {pos['rr']}x\n\n"
        f"IA {ia['prob']}% — {ia['señal']}\n"
        f"{ia['razon']}\n"
        f"Vigilar: {ia['alerta']}\n\n"
        f"Sistema Maestro v4"
    )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"=== SISTEMA LU v3.2  {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    print(f"Capital: ${CONFIG['capital_usd']:,} | Riesgo/trade: ~${CONFIG['capital_usd']*CONFIG['risk_per_trade_pct']/100:.0f}")

    estado = cargar_estado()
    ya     = estado.get("alertados", [])
    count  = estado.get("count", 0)

    if count >= CONFIG["max_alertas_dia"]:
        print(f"Maximo {CONFIG['max_alertas_dia']} alertas alcanzado hoy."); return

    tickers = obtener_universo()
    tickers = [t for t in tickers if t not in ya]
    print(f"Tickers a revisar: {len(tickers)}")

    nuevas = 0

    for ticker in tickers:
        if count + nuevas >= CONFIG["max_alertas_dia"]:
            print("Maximo de alertas diarias alcanzado."); break

        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)

        if d.get("error"):
            print(f"skip: {d['error']}"); continue

        rsi_str = f"{d['rsi']:.0f}" if d['rsi'] else "N/A"
        print(f"${d['precio']:.2f} RSI:{rsi_str} MACD:{d['macd_e']} ADX:{d['adx_e']}")

        a = analizar(d)

        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"]:
            continue

        print(f"  *** FAN {a['fan']}/4 {a['estado']} — calculando posicion...")

        try:
            pos = posicion(d["precio"])
        except AssertionError as e:
            print(f"  Posicion incoherente: {e} — skip"); continue

        ia  = analizar_ia(d, a, pos)
        print(f"  IA: {ia['prob']}% {ia['señal']}")

        msg = build_msg(d, a, pos, ia)
        if msg is None:
            print(f"  Mensaje descartado por datos incoherentes"); continue

        ok = send_wa(msg)
        if ok:
            nuevas += 1
            ya.append(ticker)
            estado["alertados"] = ya
            estado["count"]     = count + nuevas
            guardar_estado(estado)
            print(f"  ✅ WhatsApp enviado ({count+nuevas}/{CONFIG['max_alertas_dia']})")
        else:
            print(f"  ⚠️ Error WhatsApp")

    print(f"\n=== Fin: {nuevas} alertas nuevas ===")

    hora_actual = datetime.now().hour
    if nuevas == 0 and hora_actual == 13 and count == 0:
        send_wa(
            f"Sistema Lu\n"
            f"{datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"Revise {len(tickers)} tickers.\n"
            f"Sin setups validos esta manana.\n"
            f"Estas protegida — esperar es correcto.\n\n"
            f"Sistema Maestro v4"
        )

if __name__ == "__main__":
    main()
