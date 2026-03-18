# -*- coding: utf-8 -*-
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date, timezone, timedelta

# ── Claves ────────────────────────────────────────────────────
WHATSAPP_NUMBER  = os.environ.get("WHATSAPP_NUMBER", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

if not WHATSAPP_NUMBER or not CALLMEBOT_APIKEY:
    print("ERROR: Falta WHATSAPP_NUMBER o CALLMEBOT_APIKEY en GitHub Secrets")
    exit(1)

import anthropic

# ── Hora ET ───────────────────────────────────────────────────
ET = timezone(timedelta(hours=-4))
def hora_et():
    return datetime.now(ET).strftime("%d/%m/%Y  %H:%M ET")

# ── Configuracion ─────────────────────────────────────────────
CONFIG = {
    "capital_usd":        33140,
    "riesgo_fijo_usd":    150,
    "stop_loss_pct":      6.0,
    "price_min":          10.0,
    "price_max":          150.0,
    "rsi_min":            45,
    "rsi_max":            72,
    "min_volume":         1_000_000,
    "min_fan_to_alert":   3,
    "adx_min_trend":      20,
    "max_tickers_scan":   200,
    "max_alertas_dia":    5,
    "state_file":         "/tmp/lu_alertas_hoy.json",
    "watchlist_prioritaria": [
        "KGC","OXY","SLB","BTU","PAAS","SQM","FCX",
        "HIMS","EW","DAR","HAL","BKR","GDX","GOAU",
        "JEPI","SCHD","IBIT","MARA","RIOT","HUT",
        "MP","NEM","AEM","WPM","AG","GOLD",
        "CVX","XOM","COP","MRO","DVN","FANG",
    ]
}

# ── Anti-spam (FIX 6: el YAML cachea /tmp entre corridas del mismo dia) ──
def cargar_estado():
    try:
        with open(CONFIG["state_file"], "r", encoding="utf-8") as f:
            e = json.load(f)
        if e.get("fecha") != str(date.today()):
            return {"fecha": str(date.today()), "alertados": [], "count": 0}
        return e
    except:
        return {"fecha": str(date.today()), "alertados": [], "count": 0}

def guardar_estado(e):
    try:
        with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
            json.dump(e, f)
    except:
        pass

# ── Universo Finviz (FIX 5: paginado — hasta 5 paginas = ~100 tickers) ──
def obtener_universo():
    print("Obteniendo universo Finviz...")
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
                if t and t.replace("-","").isalpha() and len(t) <= 5:
                    self.tickers.append(t)
                self.capture = False

    tickers_fv = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # Paginas: r=1 (1-20), r=21 (21-40), etc. hasta r=101 (101-120)
    for page_start in [1, 21, 41, 61, 81, 101]:
        try:
            url = (
                "https://finviz.com/screener.ashx?v=111"
                "&f=sh_price_o10,sh_price_u150"
                ",sh_avgvol_o1000"
                ",ta_sma200_pa,ta_sma50_pa"
                f"&ft=4&o=-volume&r={page_start}"
            )
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"  Finviz pagina {page_start}: status {resp.status_code}")
                break
            fp = FP(); fp.feed(resp.text)
            nuevos = [t for t in fp.tickers if t not in tickers_fv]
            if not nuevos:
                break  # No hay mas paginas
            tickers_fv.extend(nuevos)
            print(f"  Finviz pagina {page_start}: +{len(nuevos)} tickers (total: {len(tickers_fv)})")
        except Exception as e:
            print(f"  Error Finviz pagina {page_start}: {e}")
            break

    if len(tickers_fv) < 5:
        print("  Finviz sin resultados, usando watchlist prioritaria")
        return CONFIG["watchlist_prioritaria"]

    # Combinar: watchlist primero (prioridad) + Finviz
    combinados = list(CONFIG["watchlist_prioritaria"])
    for t in tickers_fv:
        if t not in combinados:
            combinados.append(t)

    resultado = combinados[:CONFIG["max_tickers_scan"]]
    print(f"Universo total: {len(resultado)} tickers")
    return resultado

# ── Indicadores ───────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def sma(s, p): return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p+1: return None
    d = c.diff()
    g = d.where(d>0, 0.0); l = -d.where(d<0, 0.0)
    ag = g.ewm(com=p-1, min_periods=p).mean()
    al = l.ewm(com=p-1, min_periods=p).mean()
    rs = ag / al
    return float((100 - (100 / (1 + rs))).iloc[-1])

def calc_macd(c):
    if len(c) < 35: return None, None, None, "N/A"
    ml = ema(c,12) - ema(c,26); sl = ema(ml,9); hl = ml - sl
    mv,sv,hv = float(ml.iloc[-1]), float(sl.iloc[-1]), float(hl.iloc[-1])
    if   mv>sv and hv>0:  e="Bullish"
    elif mv>sv:           e="Weak Bull"
    elif mv<sv and hv<0:  e="Bearish"
    else:                 e="Weak Bear"
    return mv, sv, hv, e

def calc_adx(h, l, c, p=14):
    if len(h) < p*2: return None, None, None, "N/A"
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    dp = h.diff(); dm = -l.diff()
    dp = dp.where((dp>dm)&(dp>0), 0.0); dm = dm.where((dm>dp)&(dm>0), 0.0)
    atr_s = tr.ewm(alpha=1/p, adjust=False).mean()
    dip = 100 * dp.ewm(alpha=1/p, adjust=False).mean() / atr_s
    dim = 100 * dm.ewm(alpha=1/p, adjust=False).mean() / atr_s
    dx  = 100 * (dip-dim).abs() / (dip+dim).replace(0, np.nan)
    av  = float(dx.ewm(alpha=1/p, adjust=False).mean().iloc[-1])
    e   = "Strong Trend" if av>=25 else "Moderate" if av>=20 else "Weak"
    return av, float(dip.iloc[-1]), float(dim.iloc[-1]), e

# FIX 4: ATR(14)
def calc_atr(h, l, c, p=14):
    if len(h) < p+1: return None
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/p, adjust=False).mean().iloc[-1])

# FIX 2: Proyeccion de volumen intradiario al cierre del dia
def proyectar_volumen(vh, vp):
    """
    Ajusta el volumen actual (parcial) proyectandolo al cierre del dia.
    Si es pre/post market o el mercado ya cerro, devuelve el ratio sin ajuste.
    """
    try:
        ahora = datetime.now(ET)
        apertura  = ahora.replace(hour=9,  minute=30, second=0, microsecond=0)
        cierre_m  = ahora.replace(hour=16, minute=0,  second=0, microsecond=0)

        if ahora < apertura or ahora >= cierre_m:
            # Fuera de horario: no proyectar
            ratio = vh / vp if vp > 0 else 0
            return ratio, vh

        minutos_total       = 390.0  # 9:30 a 16:00
        minutos_transcurridos = max(1, (ahora - apertura).seconds / 60)
        factor              = minutos_total / minutos_transcurridos
        vh_proyectado       = vh * factor
        ratio               = vh_proyectado / vp if vp > 0 else 0
        return ratio, vh_proyectado
    except:
        ratio = vh / vp if vp > 0 else 0
        return ratio, vh

# ── Datos ─────────────────────────────────────────────────────
def obtener_datos(ticker):
    try:
        s    = yf.Ticker(ticker)
        hist = s.history(period="1y", interval="1d", prepost=True)
        if hist.empty or len(hist) < 210:
            return {"ticker": ticker, "error": "Datos insuficientes"}
        c,h,l,v = hist["Close"],hist["High"],hist["Low"],hist["Volume"]
        prev   = float(c.iloc[-2])
        es_pm  = False
        precio = float(c.iloc[-1])

        # Precio fresco: last_price (tiempo real) tiene prioridad
        try:
            fi = s.fast_info
            lp = float(getattr(fi, "last_price", None) or 0)
            pm = float(getattr(fi, "pre_market_price", None) or 0)
            if lp > 0:
                precio = lp
            elif pm > 0:
                precio = pm; es_pm = True
        except: pass

        pct = (precio - prev) / prev * 100
        vh  = float(v.iloc[-1]) if float(v.iloc[-1]) > 0 else float(v.iloc[-2])
        vp  = float(v.iloc[-20:].mean())

        # FIX 2: Volumen proyectado
        vol_r, vh_proy = proyectar_volumen(vh, vp)

        # ATH 52 semanas
        ath_52w = float(h.iloc[-252:].max()) if len(h) >= 252 else float(h.max())

        mv,sv,hv_m,me = calc_macd(c)
        av,dip,dim,ae = calc_adx(h,l,c)
        atr_val       = calc_atr(h,l,c)

        nombre = ticker
        try: nombre = s.info.get("shortName", ticker)
        except: pass

        return {
            "ticker": ticker, "nombre": nombre,
            "precio": precio, "prev": prev, "pct": pct, "es_pm": es_pm,
            "sma8":  sma(c,8),  "sma20": sma(c,20),
            "sma50": sma(c,50), "sma200":sma(c,200),
            "ema8":  float(ema(c,8).iloc[-1]),
            "ema10": float(ema(c,10).iloc[-1]),
            "ema20": float(ema(c,20).iloc[-1]),
            "ema50": float(ema(c,50).iloc[-1]),
            "ema200":float(ema(c,200).iloc[-1]),
            "rsi":   calc_rsi(c),
            "macd":  mv, "macd_s": sv, "macd_h": hv_m, "macd_e": me,
            "adx":   av, "dip": dip, "dim": dim, "adx_e": ae,
            "atr":   atr_val,
            "vh": vh, "vh_proy": vh_proy, "vp": vp, "vol_r": vol_r,
            "ath_52w": ath_52w, "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

# ── Analisis ──────────────────────────────────────────────────
def analizar(d):
    p = d["precio"]
    s8,s20,s50,s200 = d["sma8"],d["sma20"],d["sma50"],d["sma200"]
    c1 = bool(p>s8)     if s8   else False
    c2 = bool(s8>s20)   if s20  else False
    c3 = bool(s20>s50)  if s50  else False
    c4 = bool(s50>s200) if s200 else False
    fan      = sum([c1,c2,c3,c4])
    en_rango = CONFIG["price_min"] <= p <= CONFIG["price_max"]
    rsi_ok   = CONFIG["rsi_min"] <= d["rsi"] <= CONFIG["rsi_max"] if d["rsi"] else False
    vol_r    = d["vol_r"]
    # Alerta si precio ya subio >2% sobre SMA8 (entrada tardia)
    tardia   = bool(s8 and p > s8 * 1.02)
    estado   = ("ABANICO COMPLETO" if fan==4 and en_rango
                else "ABANICO PARCIAL" if fan>=2 and en_rango
                else "ABANICO ROTO")
    return {
        "fan": fan, "estado": estado,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4,
        "en_rango": en_rango, "rsi_ok": rsi_ok, "vol_r": vol_r,
        "senal_tardia": tardia,
        "e10": p>d["ema10"], "e20": p>d["ema20"],
        "e50": p>d["ema50"], "e200": p>d["ema200"]
    }

# FIX 3: Posicion basada en R exactos (1R=stop, 2R=2*stop, 3R=3*stop)
def posicion(precio, ath_52w=None):
    riesgo = CONFIG["riesgo_fijo_usd"]
    sp     = CONFIG["stop_loss_pct"] / 100   # 0.06
    stop   = round(precio * (1 - sp), 2)
    rx     = precio - stop                   # riesgo por accion = precio * 0.06
    acc    = max(1, int(riesgo / rx))
    tot    = round(acc * precio, 2)
    perd   = round(acc * rx, 2)

    # Targets basados en R (con stop del 6%):
    # T1 = +1R = +6%  (salir 25% aqui)
    # T2 = +2R = +12% (salir 30% aqui)
    # T3 = +3R = +18% (salir 20% aqui)
    # Runner = trail libre EMA8 (25% restante)
    t1 = round(precio + 1 * rx, 2)   # +1R  (~+6%)
    t2 = round(precio + 2 * rx, 2)   # +2R  (~+12%)
    t3 = round(precio + 3 * rx, 2)   # +3R  (~+18%)

    # Capear T2 y T3 al 98% del ATH de 52 semanas
    if ath_52w and ath_52w > precio:
        techo = round(ath_52w * 0.98, 2)
        if t2 > techo: t2 = techo
        if t3 > techo: t3 = techo

    rr = round((t1 - precio) / rx, 2)
    return {
        "acc": acc, "tot": tot, "stop": stop,
        "perd": perd, "t1": t1, "t2": t2, "t3": t3,
        "rx": rx, "rr": rr
    }

# ── Probabilidades de R ───────────────────────────────────────
def calc_probabilidades(fan, adx, rsi, vol_r):
    base = {4: (68, 42, 25), 3: (52, 32, 18), 2: (38, 20, 10)}
    p1, p2, p3 = base.get(fan, (38, 20, 10))
    if adx and adx >= 25:   p1+=10; p2+=8;  p3+=5
    elif adx and adx < 20:  p1-=10; p2-=8;  p3-=5
    if rsi:
        if rsi > 70:        p1-=10; p2-=8;  p3-=5
        elif rsi < 50:      p1-=6;  p2-=5;  p3-=3
    if vol_r < 0.5:         p1-=10; p2-=8;  p3-=5
    elif vol_r >= 1.5:      p1+=5;  p2+=4;  p3+=3
    p1 = max(5, min(82, p1))
    p2 = max(5, min(65, p2))
    p3 = max(5, min(50, p3))
    return p1, p2, p3

# ── IA ────────────────────────────────────────────────────────
def analizar_ia(d, a, pos):
    if not CLAUDE_API_KEY:
        return {"prob": 0, "senal": "SIN IA", "razon": "Sin API key", "alerta": ""}
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v  = d["rsi"] if d["rsi"] else 0
        atr_v  = d["atr"] if d["atr"] else 0
        prompt = (
            f"Analiza {d['ticker']} bajo Sistema Maestro v4.\n"
            f"Precio: {d['precio']:.2f} USD ({d['pct']:+.2f}%)\n"
            f"Abanico SMA: {a['fan']}/4\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"ATR(14): {atr_v:.2f} USD | Volumen: {a['vol_r']:.1f}x promedio\n"
            f"Posicion: {pos['acc']} acc | Stop: {pos['stop']:.2f} | T1: {pos['t1']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Responde EXACTAMENTE (sin tildes ni acentos ni caracteres especiales):\n"
            f"PROBABILIDAD: [0-100]\n"
            f"SIGNAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"RAZON: [max 2 frases sin tildes]\n"
            f"ALERTA: [nivel o evento clave]"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=250,
            system="Analizador Sistema Maestro v4. Responde SOLO en el formato exacto indicado. SIN tildes, SIN acentos, SIN caracteres especiales.",
            messages=[{"role": "user", "content": prompt}]
        )
        txt = msg.content[0].text
        pr,se,ra,al = 0,"ESPERAR","",""
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr = int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SIGNAL:") or ln.upper().startswith("SENAL:"):
                se = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:"):
                ra = ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"):
                al = ln.split(":",1)[1].strip()
        return {"prob": pr, "senal": se, "razon": ra, "alerta": al}
    except Exception as e:
        return {"prob": 0, "senal": "ERROR", "razon": str(e)[:80], "alerta": ""}

# ── WhatsApp ──────────────────────────────────────────────────
def send_wa(msg):
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={
                "phone":  WHATSAPP_NUMBER,
                "text":   msg,
                "apikey": CALLMEBOT_APIKEY
            },
            timeout=30
        )
        print(f"WhatsApp status: {r.status_code} | respuesta: {r.text[:80]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Error WhatsApp DETALLE: {type(e).__name__}: {e}")
        return False

def build_msg(d, a, pos, ia):
    hora   = hora_et()
    pm_tag = " [PRE-MARKET]" if d.get("es_pm") else ""
    ef     = "OK 4/4" if a["fan"]==4 else f"PARCIAL {a['fan']}/4"
    rsi_v  = d["rsi"] if d["rsi"] else 0
    atr_v  = d["atr"] if d["atr"] else 0
    atr_pct= (atr_v / d["precio"] * 100) if d["precio"] > 0 else 0

    # FIX 1: sin Ñ en ningun lugar del mensaje
    if rsi_v < CONFIG["rsi_min"]:    rsi_tag = "debil"
    elif rsi_v > CONFIG["rsi_max"]:  rsi_tag = "sobrecomprado"
    elif rsi_v == CONFIG["rsi_max"]: rsi_tag = "en el limite"
    else:                             rsi_tag = "OK"

    # FIX 2: Volumen proyectado
    vol_r     = a["vol_r"]
    vol_tag   = "OK" if vol_r >= 1.5 else "bajo" if vol_r < 0.8 else "moderado"
    tardia_av = "  AVISO precio >2% sobre SMA8 - verifica entrada" if a.get("senal_tardia") else ""

    p1, p2, p3 = calc_probabilidades(a["fan"], d["adx"], rsi_v, vol_r)

    # Plan de salida Sistema Hibrido Lu
    acc  = pos["acc"]
    s25  = max(1, round(acc * 0.25))
    s30  = max(1, round(acc * 0.30))
    s20  = max(1, round(acc * 0.20))
    s25b = max(0, acc - s25 - s30 - s20)

    # Distancia al ATH
    ath = d.get("ath_52w", 0)
    dist_ath = f"  (ATH 52s: {ath:.2f}, -{((ath - d['precio'])/ath*100):.1f}%)" if ath > d["precio"] else "  (cerca del ATH)"

    return (
        f"*SISTEMA LU - SIGNAL*\n"                                              # FIX 1
        f"{hora}{pm_tag}\n"
        f"Timeframe: DIARIO (swing 5-10 dias)\n\n"
        f"*{d['ticker']}*  {d.get('nombre','')}\n"
        f"Precio: {d['precio']:.2f} USD  ({d['pct']:+.1f}%){tardia_av}\n"
        f"ATH 52s: {ath:.2f}{dist_ath}\n\n"
        f"*Abanico SMA {ef}*\n"
        f"{'SI' if a['c1'] else 'NO'} Precio > SMA8    {d['sma8']:.2f}\n"
        f"{'SI' if a['c2'] else 'NO'} SMA8   > SMA20   {d['sma20']:.2f}\n"
        f"{'SI' if a['c3'] else 'NO'} SMA20  > SMA50   {d['sma50']:.2f}\n"
        f"{'SI' if a['c4'] else 'NO'} SMA50  > SMA200  {d['sma200']:.2f}\n\n"
        f"*Indicadores*\n"
        f"MACD: {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f}  ({rsi_tag})\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        f"ATR(14): {atr_v:.2f} USD  ({atr_pct:.1f}% del precio)\n"            # FIX 4
        f"Vol (proy.): {vol_r:.1f}x promedio ({vol_tag})\n\n"                  # FIX 2
        f"*Tu posicion*\n"
        f"Entrada:         {d['precio']:.2f} USD\n"
        f"Comprar:         *{pos['acc']} acciones*\n"
        f"Capital usado:   {pos['tot']:.0f} USD\n"
        f"Stop loss:       {pos['stop']:.2f} USD  (-6% / -1R)\n"
        f"Target 1 (+1R):  {pos['t1']:.2f} USD  (+{((pos['t1']/d['precio'])-1)*100:.1f}%)\n"   # FIX 3
        f"Target 2 (+2R):  {pos['t2']:.2f} USD  (+{((pos['t2']/d['precio'])-1)*100:.1f}%)\n"
        f"Target 3 (+3R):  {pos['t3']:.2f} USD  (+{((pos['t3']/d['precio'])-1)*100:.1f}%)\n"
        f"Runner:          trail libre EMA8\n"
        f"R/R:             {pos['rr']:.1f}x\n"
        f"Riesgo maximo:   {pos['perd']:.0f} USD\n\n"
        f"*Plan salida - Sistema Hibrido Lu*\n"
        f"25% ({s25} acc)  -> T1 | 30% ({s30} acc) -> T2\n"
        f"20% ({s20} acc)  -> T3 | 25% ({s25b} acc) -> trail EMA8\n"
        f"Time-stop: 7 dias sin T1 -> salida total\n\n"
        f"*Probabilidades*\n"
        f"Llegar T1 (+1R):  {p1}%\n"
        f"Llegar T2 (+2R):  {p2}%\n"
        f"Llegar T3 (+3R):  {p3}%\n\n"
        f"*IA {ia['prob']}%  -  {ia['senal']}*\n"
        f"{ia['razon']}\n"
        f"Vigilar: {ia['alerta']}\n\n"
        f"Sistema Maestro v4"
    )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"=== SISTEMA LU  {hora_et()} ===")
    estado = cargar_estado()
    ya     = estado.get("alertados", [])
    count  = estado.get("count", 0)

    if count >= CONFIG["max_alertas_dia"]:
        print(f"Maximo {CONFIG['max_alertas_dia']} alertas del dia alcanzado."); return

    tickers = obtener_universo()
    # FIX 6: tickers ya alertados se excluyen (el YAML persiste /tmp entre corridas)
    pendientes = [t for t in tickers if t not in ya]
    print(f"Tickers a revisar: {len(pendientes)} (ya alertados hoy: {len(ya)})")

    nuevas = 0
    for ticker in pendientes:
        if count + nuevas >= CONFIG["max_alertas_dia"]: break

        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)
        if d.get("error"):
            print(f"skip ({d['error'][:40]})"); continue

        rsi_str = f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        atr_str = f"{d['atr']:.2f}" if d["atr"] else "N/A"
        print(f"{d['precio']:.2f} RSI:{rsi_str} MACD:{d['macd_e']} ATR:{atr_str}")

        a = analizar(d)
        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"]:
            continue

        print(f"  *** FAN {a['fan']}/4 detectado!")
        pos = posicion(d["precio"], d.get("ath_52w"))
        ia  = analizar_ia(d, a, pos)
        print(f"  IA: {ia['prob']}%  {ia['senal']}")

        msg = build_msg(d, a, pos, ia)
        ok  = send_wa(msg)

        if ok:
            nuevas += 1
            ya.append(ticker)
            estado["alertados"] = ya
            estado["count"]     = count + nuevas
            guardar_estado(estado)
            print(f"  WhatsApp enviado ({count+nuevas}/{CONFIG['max_alertas_dia']} hoy)")
        else:
            print(f"  Error WhatsApp")

    print(f"=== Fin: {nuevas} alertas nuevas ===")

    # Mensaje de apertura solo si no hay nada en la primera corrida
    hora_utc = datetime.now(timezone.utc).hour
    if nuevas == 0 and hora_utc == 13 and count == 0:
        send_wa(
            f"SISTEMA LU - SIGNAL\n{hora_et()}\n\n"
            f"Universo revisado: {len(pendientes)} tickers.\n"
            f"Sin setups validos esta manana.\n\n"
            f"Estás protegida - esperar es correcto.\n"
            f"Sistema Maestro v4"
        )

if __name__ == "__main__":
    main()
