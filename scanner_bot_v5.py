# -*- coding: utf-8 -*-
# Solares — Sistema Sirio Bot v5
# Trader: Lu Espitia | Sector: Proprietary Trading + Alternative Data
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date, timezone, timedelta

# ── Claves ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# FIX 1 ERROR: el bot anterior buscaba WHATSAPP_NUMBER/CALLMEBOT.
# Ahora solo usa Telegram. Si ves ese error, subiste el archivo equivocado.
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en GitHub Secrets")
    print("  Solucion: Settings -> Secrets -> Actions -> agregar ambos")
    exit(1)

import anthropic

ET = timezone(timedelta(hours=-4))
def hora_et():
    return datetime.now(ET).strftime("%d/%m/%Y  %H:%M ET")

# ── Configuracion ─────────────────────────────────────────────
CONFIG = {
    "capital_usd":       33140,
    "riesgo_fijo_usd":   150,
    "stop_loss_pct":     6.0,
    # FIX 6: precio desde $10 (no $20)
    # $10-$150: rango correcto para swing con tu capital.
    # Bajo $10 = penny stocks (manipulacion, spread alto, sin institucionales).
    # Sobre $150 = 1R demasiado grande: $150 riesgo / (precio*0.06) = muy pocas acciones.
    "price_min":         10.0,
    "price_max":         150.0,
    "rsi_min":           45,
    "rsi_max":           72,
    # FIX 5: volumen
    # vol_r = volumen hoy proyectado al cierre / promedio 20 dias.
    # Para swing (5-10d) el prom.20d es el benchmark correcto.
    # La proyeccion evita comparar 11am vs cierre completo.
    # min_volume_abs: filtro duro — ninguna accion bajo 500K acciones/dia.
    "min_volume_abs":    500_000,
    "min_fan_to_alert":  3,
    "max_tickers_scan":  200,
    "max_alertas_dia":   5,
    "state_file":        "/tmp/sirio_alertas_hoy.json",
    "watchlist_prioritaria": [
        "KGC","OXY","SLB","BTU","PAAS","SQM","FCX",
        "HIMS","EW","DAR","HAL","BKR","GDX","GOAU",
        "JEPI","SCHD","IBIT","MARA","RIOT","HUT",
        "MP","NEM","AEM","WPM","AG","GOLD",
        "CVX","XOM","COP","DVN","FANG","MRO",
    ]
}

# ── Estado del dia (anti-spam) ────────────────────────────────
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

# ── Universo Finviz (paginado) ────────────────────────────────
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
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for r0 in [1, 21, 41, 61, 81, 101]:
        try:
            url = (
                "https://finviz.com/screener.ashx?v=111"
                "&f=sh_price_o10,sh_price_u150,sh_avgvol_o500"
                ",ta_sma200_pa,ta_sma50_pa"
                f"&ft=4&o=-volume&r={r0}"
            )
            resp = requests.get(url, headers=hdrs, timeout=30)
            if resp.status_code != 200: break
            fp = FP(); fp.feed(resp.text)
            nuevos = [t for t in fp.tickers if t not in tickers_fv]
            if not nuevos: break
            tickers_fv.extend(nuevos)
            print(f"  Finviz p{r0}: +{len(nuevos)} (total:{len(tickers_fv)})")
        except Exception as ex:
            print(f"  Error Finviz p{r0}: {ex}"); break

    if len(tickers_fv) < 5:
        print("  Sin resultados Finviz — watchlist prioritaria")
        return CONFIG["watchlist_prioritaria"]

    combinados = list(CONFIG["watchlist_prioritaria"])
    for t in tickers_fv:
        if t not in combinados:
            combinados.append(t)
    resultado = combinados[:CONFIG["max_tickers_scan"]]
    print(f"Universo total: {len(resultado)} tickers")
    return resultado

# ── Indicadores tecnicos ──────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def sma(s, p): return float(s.iloc[-p:].mean()) if len(s) >= p else None

def calc_rsi(c, p=14):
    if len(c) < p+1: return None
    d = c.diff()
    g = d.where(d>0,0.0); l = -d.where(d<0,0.0)
    ag = g.ewm(com=p-1,min_periods=p).mean()
    al = l.ewm(com=p-1,min_periods=p).mean()
    return float((100-(100/(1+ag/al))).iloc[-1])

def calc_macd(c):
    if len(c) < 35: return None,None,None,"N/A"
    ml=ema(c,12)-ema(c,26); sl=ema(ml,9); hl=ml-sl
    mv,sv,hv=float(ml.iloc[-1]),float(sl.iloc[-1]),float(hl.iloc[-1])
    if mv>sv and hv>0: e="Bullish"
    elif mv>sv:        e="Weak Bull"
    elif mv<sv and hv<0: e="Bearish"
    else:              e="Weak Bear"
    return mv,sv,hv,e

def calc_adx(h,l,c,p=14):
    if len(h)<p*2: return None,None,None,"N/A"
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    dp=h.diff(); dm=-l.diff()
    dp=dp.where((dp>dm)&(dp>0),0.0); dm=dm.where((dm>dp)&(dm>0),0.0)
    as_=tr.ewm(alpha=1/p,adjust=False).mean()
    dip=100*dp.ewm(alpha=1/p,adjust=False).mean()/as_
    dim=100*dm.ewm(alpha=1/p,adjust=False).mean()/as_
    dx=100*(dip-dim).abs()/(dip+dim).replace(0,np.nan)
    av=float(dx.ewm(alpha=1/p,adjust=False).mean().iloc[-1])
    e="Strong Trend" if av>=25 else "Moderate" if av>=20 else "Weak"
    return av,float(dip.iloc[-1]),float(dim.iloc[-1]),e

def calc_atr(h,l,c,p=14):
    if len(h)<p+1: return None
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/p,adjust=False).mean().iloc[-1])

def proyectar_volumen(vh, vp):
    try:
        ahora    = datetime.now(ET)
        apertura = ahora.replace(hour=9,  minute=30, second=0, microsecond=0)
        cierre   = ahora.replace(hour=16, minute=0,  second=0, microsecond=0)
        if ahora < apertura or ahora >= cierre:
            return (vh/vp if vp>0 else 0), vh
        mins_total = 390.0
        mins_trans = max(1, (ahora-apertura).seconds/60)
        factor     = mins_total/mins_trans
        vh_proy    = vh*factor
        return (vh_proy/vp if vp>0 else 0), vh_proy
    except:
        return (vh/vp if vp>0 else 0), vh

# ── Sentiment redes sociales ──────────────────────────────────
# ── Sentiment via noticias yfinance (no requiere API externa) ─
# GitHub Actions bloquea StockTwits/Reddit. Usamos las noticias
# de yfinance (Yahoo Finance) que siempre funcionan, y dejamos
# que Claude interprete el tono y tema relevante.

def get_noticias_ticker(ticker):
    """Obtiene titulares recientes de Yahoo Finance via yfinance."""
    try:
        noticias = yf.Ticker(ticker).news
        if not noticias:
            return []
        # Tomar las 5 mas recientes
        titulares = []
        for n in noticias[:5]:
            t = n.get("title", "")
            if t:
                titulares.append(t)
        return titulares
    except:
        return []

def get_sentiment_resumen(ticker):
    """Sentiment basado en titulares recientes de Yahoo Finance."""
    titulares = get_noticias_ticker(ticker)
    if not titulares:
        return None, "Sin noticias recientes en Yahoo Finance"
    texto_noticias = " | ".join(titulares[:3])
    # Score simple por palabras clave (luego la IA da el analisis profundo)
    positivas = ["beat","surge","jump","rise","higher","buy","upgrade","growth",
                 "strong","record","profit","rally","up","gain","outperform"]
    negativas = ["miss","fall","drop","decline","lower","sell","downgrade","loss",
                 "weak","cut","down","risk","concern","warn","below"]
    text_lower = texto_noticias.lower()
    pos = sum(1 for w in positivas if w in text_lower)
    neg = sum(1 for w in negativas if w in text_lower)
    total = pos + neg
    if total == 0:
        score = 50; etiq = "Neutral"
    else:
        score = int(pos / total * 100)
        etiq = ("Bullish" if score >= 65 else "Lev.Bullish" if score >= 55
                else "Neutral" if score >= 45 else "Lev.Bearish" if score >= 35
                else "Bearish")
    resumen = f"{etiq} ({score}% bullish — {len(titulares)} noticias)\n"
    for t in titulares[:3]:
        resumen += f"- {t[:80]}\n"
    return score, resumen.strip()

# ── Datos ─────────────────────────────────────────────────────
def obtener_datos(ticker):
    try:
        s    = yf.Ticker(ticker)
        hist = s.history(period="1y",interval="1d",prepost=True)
        if hist.empty or len(hist)<210:
            return {"ticker":ticker,"error":"Datos insuficientes"}
        c,h,l,v=hist["Close"],hist["High"],hist["Low"],hist["Volume"]
        prev=float(c.iloc[-2]); es_pm=False; precio=float(c.iloc[-1])
        try:
            fi=s.fast_info
            lp=float(getattr(fi,"last_price",None) or 0)
            pm=float(getattr(fi,"pre_market_price",None) or 0)
            if lp>0:   precio=lp
            elif pm>0: precio=pm; es_pm=True
        except: pass
        pct=(precio-prev)/prev*100
        vh=float(v.iloc[-1]) if float(v.iloc[-1])>0 else float(v.iloc[-2])
        vp=float(v.iloc[-20:].mean())
        vol_r,vh_proy=proyectar_volumen(vh,vp)
        ath_52w=float(h.iloc[-252:].max()) if len(h)>=252 else float(h.max())
        mv,sv,hv_m,me=calc_macd(c)
        av,dip,dim,ae=calc_adx(h,l,c)
        atr_val=calc_atr(h,l,c)
        nombre=ticker; sector="N/A"
        try:
            info=s.info
            nombre=info.get("shortName",ticker)
            sector=info.get("sector","N/A")
        except: pass
        return {
            "ticker":ticker,"nombre":nombre,"sector":sector,
            "precio":precio,"prev":prev,"pct":pct,"es_pm":es_pm,
            "sma8":sma(c,8),"sma20":sma(c,20),
            "sma50":sma(c,50),"sma200":sma(c,200),
            "ema8":float(ema(c,8).iloc[-1]),
            "ema10":float(ema(c,10).iloc[-1]),
            "ema20":float(ema(c,20).iloc[-1]),
            "ema50":float(ema(c,50).iloc[-1]),
            "ema200":float(ema(c,200).iloc[-1]),
            "rsi":calc_rsi(c),
            "macd":mv,"macd_s":sv,"macd_h":hv_m,"macd_e":me,
            "adx":av,"dip":dip,"dim":dim,"adx_e":ae,
            "atr":atr_val,
            "vh":vh,"vh_proy":vh_proy,"vp":vp,"vol_r":vol_r,
            "ath_52w":ath_52w,"error":None
        }
    except Exception as ex:
        return {"ticker":ticker,"error":str(ex)}

# ── Analisis ──────────────────────────────────────────────────
def analizar(d):
    p=d["precio"]
    s8,s20,s50,s200=d["sma8"],d["sma20"],d["sma50"],d["sma200"]
    c1=bool(p>s8)    if s8   else False
    c2=bool(s8>s20)  if s20  else False
    c3=bool(s20>s50) if s50  else False
    c4=bool(s50>s200)if s200 else False
    fan=sum([c1,c2,c3,c4])
    en_rango=CONFIG["price_min"]<=p<=CONFIG["price_max"]
    vol_r=d["vol_r"]
    vol_ok=d["vh"]>=CONFIG["min_volume_abs"]
    tardia=bool(s8 and p>s8*1.02)
    estado=("ABANICO COMPLETO" if fan==4 and en_rango
            else "ABANICO PARCIAL" if fan>=2 and en_rango
            else "ABANICO ROTO")
    return {
        "fan":fan,"estado":estado,
        "c1":c1,"c2":c2,"c3":c3,"c4":c4,
        "en_rango":en_rango,"vol_r":vol_r,"vol_ok":vol_ok,
        "senal_tardia":tardia,
        "e10":p>d["ema10"],"e20":p>d["ema20"],
        "e50":p>d["ema50"],"e200":p>d["ema200"]
    }

def posicion(precio, ath_52w=None):
    sp=CONFIG["stop_loss_pct"]/100
    stop=round(precio*(1-sp),2); rx=precio-stop
    acc=max(1,int(CONFIG["riesgo_fijo_usd"]/rx))
    tot=round(acc*precio,2); perd=round(acc*rx,2)
    t1=round(precio+1*rx,2); t2=round(precio+2*rx,2); t3=round(precio+3*rx,2)
    # BUG FIX: el techo (98% ATH) NUNCA puede bajar T2/T3 por debajo de T1
    # Ejemplo HAL: ATH=$36.85, techo=$36.11, T1=$38.39 -> antes T2=T3=$36.11 (absurdo)
    # Ahora: si techo < T1, no se aplica el cap
    if ath_52w and ath_52w > precio:
        techo = round(ath_52w * 0.98, 2)
        if techo > t1:
            if t2 > techo: t2 = techo
            if t3 > techo: t3 = techo
    rr=round((t1-precio)/rx,2)
    return {"acc":acc,"tot":tot,"stop":stop,"perd":perd,
            "t1":t1,"t2":t2,"t3":t3,"rx":rx,"rr":rr}

# Mapa sector -> ETF de referencia para contexto de IA
SECTOR_ETF = {
    "energy":               "XLE",
    "technology":           "QQQ",
    "healthcare":           "XLV",
    "financial":            "XLF",
    "basic materials":      "XLB",
    "basic mate":           "XLB",
    "consumer cyclical":    "XLY",
    "consumer d":           "XLY",
    "consumer defensive":   "XLP",
    "industrials":          "XLI",
    "real estate":          "XLRE",
    "utilities":            "XLU",
    "communication":        "XLC",
}

def get_sector_etf(sector):
    s = sector.lower()
    for k, v in SECTOR_ETF.items():
        if k in s:
            return v
    return "SPY"

def get_vix():
    try:
        fi = yf.Ticker("^VIX").fast_info
        v  = float(getattr(fi, "last_price", None) or 0)
        if v > 0:
            nivel = "ALTO/MIEDO" if v > 25 else "MODERADO" if v > 18 else "BAJO/CALMA"
            return round(v, 1), nivel
    except:
        pass
    return None, "N/D"

def calc_probabilidades(fan,adx,rsi,vol_r):
    base={4:(68,42,25),3:(52,32,18),2:(38,20,10)}
    p1,p2,p3=base.get(fan,(38,20,10))
    if adx and adx>=25:  p1+=10;p2+=8;p3+=5
    elif adx and adx<20: p1-=10;p2-=8;p3-=5
    if rsi:
        if rsi>70:       p1-=10;p2-=8;p3-=5
        elif rsi<50:     p1-=6;p2-=5;p3-=3
    if vol_r<0.5:        p1-=10;p2-=8;p3-=5
    elif vol_r>=1.5:     p1+=5;p2+=4;p3+=3
    return max(5,min(82,p1)),max(5,min(65,p2)),max(5,min(50,p3))

# ── IA ────────────────────────────────────────────────────────
def analizar_ia(d, a, pos, sentiment_score):
    if not CLAUDE_API_KEY:
        return {"prob":0,"senal":"SIN IA","razon":"Sin API key","alerta":""}
    try:
        client     = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v      = d["rsi"] if d["rsi"] else 0
        atr_v      = d["atr"] if d["atr"] else 0
        atr_pct    = round(atr_v / d["precio"] * 100, 1) if d["precio"] > 0 else 0
        sent_s     = f"{sentiment_score}%" if sentiment_score else "N/D"
        sector     = d.get("sector", "N/A")
        etf_ref    = get_sector_etf(sector)
        vix_v, vix_nivel = get_vix()
        vix_str    = f"{vix_v} ({vix_nivel})" if vix_v else "N/D"
        tardia_str = "SI - precio >2% sobre SMA8" if a.get("senal_tardia") else "NO"

        prompt = (
            f"Analiza esta oportunidad de swing trading (5-10 dias).\n\n"
            f"TICKER: {d['ticker']} | Sector: {sector} | ETF referencia: {etf_ref}\n"
            f"Precio: {d['precio']:.2f} USD ({d['pct']:+.2f}%)\n"
            f"Entrada tardia (>2% sobre SMA8): {tardia_str}\n\n"
            f"TECNICOS:\n"
            f"Abanico SMA: {a['fan']}/4 | MACD: {d['macd_e']}\n"
            f"RSI: {rsi_v:.0f} | ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"ATR: {atr_v:.2f} USD ({atr_pct}% del precio — volatilidad diaria esperada)\n"
            f"Volumen hoy vs prom.20d: {a['vol_r']:.1f}x\n\n"
            f"CONTEXTO MACRO:\n"
            f"VIX actual: {vix_str}\n"
            f"Sentiment redes (% bullish): {sent_s}\n"
            f"Stop -1R: {pos['stop']:.2f} | T1 +1R: {pos['t1']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Responde SIN tildes, SIN acentos, en formato EXACTO:\n"
            f"PROBABILIDAD: [0-100]\n"
            f"SIGNAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"CONTEXTO: [1 frase sobre VIX + como se mueve {etf_ref} afecta a {d['ticker']} "
            f"+ tema o catalizador relevante para este sector ahora mismo]\n"
            f"RAZON: [1 frase sobre el setup tecnico especifico]\n"
            f"ALERTA: [nivel de precio o evento clave a vigilar]"
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=300,
            system=(
                "Eres el analizador del Sistema Sirio de Solares Trading. "
                "Conoces correlaciones entre sectores, ETFs y macro. "
                "Eres experto en contexto de mercado: VIX, rotacion sectorial, noticias que mueven sectores. "
                "Responde SOLO en el formato indicado. SIN tildes, SIN acentos, SIN caracteres especiales."
            ),
            messages=[{"role": "user", "content": prompt}]
        )
        txt = msg.content[0].text
        pr, se, ctx, ra, al = 0, "ESPERAR", "", "", ""
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr = int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SIGNAL:"):
                se = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("CONTEXTO:"):
                ctx = ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:"):
                ra = ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"):
                al = ln.split(":",1)[1].strip()
        return {"prob":pr,"senal":se,"contexto":ctx,"razon":ra,"alerta":al}
    except Exception as ex:
        return {"prob":0,"senal":"ERROR","contexto":"","razon":str(ex)[:80],"alerta":""}

# ── Telegram ──────────────────────────────────────────────────
def send_telegram(msg):
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        msg_safe=msg.replace("_","-").replace("[","(").replace("]",")")
        r=requests.post(url,json={
            "chat_id":TELEGRAM_CHAT_ID,
            "text":msg_safe,
            "parse_mode":"Markdown"
        },timeout=30)
        data=r.json()
        if r.status_code==200 and data.get("ok"):
            print(f"  [TG] OK id:{data['result']['message_id']}")
            return True
        # Reintento sin Markdown si falla por caracteres
        print(f"  [TG] Reintentando sin formato ({data.get('description','')})")
        r2=requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=30)
        if r2.status_code==200 and r2.json().get("ok"):
            print(f"  [TG] OK (sin formato)")
            return True
        print(f"  [TG] Error: {r2.json().get('description','')}")
        return False
    except Exception as ex:
        print(f"  [TG] Excepcion: {ex}")
        return False

# FIX 2: nombre SISTEMA SIRIO — Solares (antes era SISTEMA LU)
def build_msg(d, a, pos, ia, sent_texto):
    hora    = hora_et()
    pm_tag  = " [PRE-MARKET]" if d.get("es_pm") else ""
    ef      = "OK 4/4" if a["fan"]==4 else f"PARCIAL {a['fan']}/4"
    rsi_v   = d["rsi"] if d["rsi"] else 0
    atr_v   = d["atr"] if d["atr"] else 0
    # FIX 3: ATR solo en % (mas util que el valor absoluto en USD)
    atr_pct = round(atr_v / d["precio"] * 100, 1) if d["precio"] > 0 else 0

    if rsi_v < CONFIG["rsi_min"]:    rsi_tag = "debil"
    elif rsi_v > CONFIG["rsi_max"]:  rsi_tag = "sobrecomprado"
    elif rsi_v == CONFIG["rsi_max"]: rsi_tag = "en el limite"
    else:                             rsi_tag = "OK"

    # FIX 4: volumen — explicacion clara de las X
    vol_r   = a["vol_r"]
    if vol_r >= 1.5:
        vol_tag = f"ALTO — {vol_r:.1f}x el promedio"
    elif vol_r >= 0.8:
        vol_tag = f"normal — {vol_r:.1f}x el promedio"
    else:
        vol_tag = f"BAJO — {vol_r:.1f}x el promedio"

    # FIX 3: entrada tardia — si precio >2% sobre SMA8 Y fan<4, ADVERTENCIA fuerte
    if a.get("senal_tardia") and a["fan"] < 4:
        tardia_v = "\nAVISO: precio extendido >2% sobre SMA8 — considera esperar pullback"
    elif a.get("senal_tardia"):
        tardia_v = "  (extendido >2% SMA8)"
    else:
        tardia_v = ""

    p1, p2, p3 = calc_probabilidades(a["fan"], d["adx"], rsi_v, vol_r)

    acc  = pos["acc"]
    s25  = max(1, round(acc * 0.25))
    s30  = max(1, round(acc * 0.30))
    s20  = max(1, round(acc * 0.20))
    s25b = max(0, acc - s25 - s30 - s20)

    ath  = d.get("ath_52w", 0)
    dist_ath = (f"ATH 52s: {ath:.2f}  (-{((ath-d['precio'])/ath*100):.1f}%)"
                if ath > d["precio"] else f"ATH 52s: {ath:.2f}  (en zona ATH)")

    etf_ref = get_sector_etf(d.get("sector","N/A"))
    ctx_ia  = ia.get("contexto", "")

    return (
        f"*SISTEMA SIRIO — Solares*\n"
        f"{hora}{pm_tag}\n"
        f"Swing DIARIO  5-10 dias\n\n"
        f"*{d['ticker']}*  {d.get('nombre','')}\n"
        f"Sector: {d.get('sector','N/A')}  |  Ref: {etf_ref}\n"
        f"Precio: {d['precio']:.2f} USD  ({d['pct']:+.1f}%){tardia_v}\n"
        f"{dist_ath}\n\n"
        f"*Abanico SMA {ef}*\n"
        f"{'SI' if a['c1'] else 'NO'} Precio > SMA8    {d['sma8']:.2f}\n"
        f"{'SI' if a['c2'] else 'NO'} SMA8   > SMA20   {d['sma20']:.2f}\n"
        f"{'SI' if a['c3'] else 'NO'} SMA20  > SMA50   {d['sma50']:.2f}\n"
        f"{'SI' if a['c4'] else 'NO'} SMA50  > SMA200  {d['sma200']:.2f}\n\n"
        f"*Indicadores*\n"
        f"MACD: {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f}  ({rsi_tag})\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        # FIX 3: ATR solo en % del precio
        f"ATR(14): {atr_pct}% del precio  (rango diario esperado)\n"
        # FIX 4: volumen con explicacion clara
        f"Volumen: {vol_tag}\n"
        f"  (comparado con promedio de los ultimos 20 dias)\n\n"
        f"*Sentiment redes*\n"
        f"{sent_texto}\n\n"
        f"*Tu posicion*\n"
        f"Entrada:    {d['precio']:.2f} USD\n"
        f"Comprar:    *{pos['acc']} acciones*\n"
        f"Capital:    {pos['tot']:.0f} USD\n"
        f"Stop -1R:   {pos['stop']:.2f} USD  (-6%)\n"
        f"T1 +1R:     {pos['t1']:.2f} USD  (+{((pos['t1']/d['precio'])-1)*100:.1f}%)\n"
        f"T2 +2R:     {pos['t2']:.2f} USD  (+{((pos['t2']/d['precio'])-1)*100:.1f}%)\n"
        f"T3 +3R:     {pos['t3']:.2f} USD  (+{((pos['t3']/d['precio'])-1)*100:.1f}%)\n"
        f"Runner:     trail EMA8 libre\n"
        f"R/R: {pos['rr']:.1f}x  |  Riesgo: {pos['perd']:.0f} USD\n\n"
        # FIX 5: nombre correcto del plan de salida
        f"*Gestion de Salida — Sistema Sirio*\n"
        f"25%({s25}acc) T1  |  30%({s30}acc) T2\n"
        f"20%({s20}acc) T3  |  25%({s25b}acc) trail EMA8\n"
        f"Time-stop: 7 dias sin T1 salida total\n\n"
        f"*Probabilidades*\n"
        f"T1: {p1}%  |  T2: {p2}%  |  T3: {p3}%\n\n"
        # FIX 6: IA con contexto macro/sector/tema siempre presente
        f"*IA {ia['prob']}% — {ia['senal']}*\n"
        f"Contexto: {ia.get('contexto','N/D')}\n"
        f"Setup: {ia['razon']}\n"
        f"Vigilar: {ia['alerta']}\n\n"
        f"Sistema Sirio v5.1 — Solares"
    )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"=== SISTEMA SIRIO — Solares  {hora_et()} ===")
    estado    = cargar_estado()
    ya        = estado.get("alertados",[])
    count     = estado.get("count",0)

    if count>=CONFIG["max_alertas_dia"]:
        print(f"Maximo {CONFIG['max_alertas_dia']} alertas alcanzado."); return

    tickers    = obtener_universo()
    pendientes = [t for t in tickers if t not in ya]
    print(f"A revisar: {len(pendientes)}  (ya alertados hoy: {len(ya)})")

    nuevas=0
    for ticker in pendientes:
        if count+nuevas>=CONFIG["max_alertas_dia"]: break
        print(f"  {ticker}...", end=" ", flush=True)
        d=obtener_datos(ticker)
        if d.get("error"):
            print(f"skip ({d['error'][:40]})"); continue
        rsi_s=f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        print(f"{d['precio']:.2f} RSI:{rsi_s} MACD:{d['macd_e']} [{d.get('sector','?')[:10]}]")
        a=analizar(d)
        if a["fan"]<CONFIG["min_fan_to_alert"] or not a["en_rango"] or not a["vol_ok"]:
            continue
        # FIX: señal tardia + fan incompleto = NO alertar (esperar pullback)
        # Solo alertar si fan==4 aunque sea tardía, o si no es tardía con fan>=3
        if a.get("senal_tardia") and a["fan"] < 4:
            print(f"  skip (tardia >2% SMA8 con fan {a['fan']}/4 — esperar pullback)")
            continue
        print(f"  *** FAN {a['fan']}/4 — obteniendo sentiment...")
        sent_score,sent_texto=get_sentiment_resumen(ticker)
        pos=posicion(d["precio"],d.get("ath_52w"))
        ia =analizar_ia(d,a,pos,sent_score)
        print(f"  IA:{ia['prob']}% {ia['senal']} | Sent:{sent_texto.split(chr(10))[0]}")
        msg=build_msg(d,a,pos,ia,sent_texto)
        ok =send_telegram(msg)
        if ok:
            nuevas+=1; ya.append(ticker)
            estado["alertados"]=ya; estado["count"]=count+nuevas
            guardar_estado(estado)
            print(f"  Enviado ({count+nuevas}/{CONFIG['max_alertas_dia']} hoy)")
        else:
            print(f"  Error Telegram")

    print(f"=== Fin: {nuevas} alertas nuevas ===")

    hora_utc=datetime.now(timezone.utc).hour
    if nuevas==0 and hora_utc==13 and count==0:
        send_telegram(
            f"*SISTEMA SIRIO — Solares*\n{hora_et()}\n\n"
            f"Universo revisado: {len(pendientes)} tickers.\n"
            f"Sin setups validos esta manana.\n\n"
            f"Estas protegida. Esperar es la posicion.\n\n"
            f"Sistema Sirio v5 — Solares"
        )

if __name__=="__main__":
    main()
