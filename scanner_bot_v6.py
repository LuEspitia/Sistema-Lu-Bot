# -*- coding: utf-8 -*-
# Solares — Sistema Sirio Bot v6
# Trader: Lu Espitia | Sector: Proprietary Trading + Alternative Data
# v6: web_search IA en tiempo real, earnings filter, score compuesto,
#     backtest log, multi-timeframe, velas japonesas, tildes HTML, aviso legal
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import json
from datetime import datetime, date, timezone, timedelta

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en GitHub Secrets")
    exit(1)

import anthropic

ET = timezone(timedelta(hours=-4))
def hora_et():
    return datetime.now(ET).strftime("%d/%m/%Y  %H:%M ET")

CONFIG = {
    "capital_usd":         33140,
    "riesgo_fijo_usd":     150,
    "stop_loss_pct":       6.0,
    "price_min":           10.0,
    "price_max":           150.0,
    "rsi_min":             45,
    "rsi_max":             72,
    "min_volume_abs":      500_000,
    "min_fan_to_alert":    4,
    "score_minimo":        60,    # MEJORA 3: score compuesto mínimo
    "earnings_dias_min":   5,     # MEJORA 2: días mínimos antes de earnings
    "max_tickers_scan":    200,
    "max_alertas_dia":     5,
    "state_file":          "/tmp/sirio_alertas_hoy.json",
    "backtest_file":       "/tmp/sirio_backtest.json",
    "watchlist_prioritaria": [
        "KGC","OXY","SLB","BTU","PAAS","SQM","FCX",
        "HIMS","EW","DAR","HAL","BKR","GDX","GOAU",
        "JEPI","SCHD","IBIT","MARA","RIOT","HUT",
        "MP","NEM","AEM","WPM","AG","GOLD",
        "CVX","XOM","COP","DVN","FANG","MRO",
    ]
}

# ── Estado del día ────────────────────────────────────────────
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

# ── MEJORA 4: Backtest log ────────────────────────────────────
def registrar_backtest(ticker, precio, stop, t1, t2, t3, score, ia_senal, vela):
    try:
        log = []
        try:
            with open(CONFIG["backtest_file"], "r") as f:
                log = json.load(f)
        except:
            pass
        log.append({
            "fecha":    str(date.today()),
            "hora":     hora_et(),
            "ticker":   ticker,
            "entrada":  precio,
            "stop":     stop,
            "t1":       t1,
            "t2":       t2,
            "t3":       t3,
            "score":    score,
            "ia":       ia_senal,
            "vela":     vela,
            "resultado":"PENDIENTE",
            "pct":      None
        })
        with open(CONFIG["backtest_file"], "w") as f:
            json.dump(log[-300:], f, indent=2)
        print(f"  [BT] Señal registrada en backtest ({len(log)} total)")
    except Exception as ex:
        print(f"  [BT] Error: {ex}")

# ── Universo Finviz ───────────────────────────────────────────
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

# ── Indicadores técnicos ──────────────────────────────────────
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

# ── MEJORA 6: Velas japonesas ─────────────────────────────────
# Detecta las formaciones que Lu usa para confirmar entradas en SMA8:
# Hammer, Bullish Engulfing, Morning Star, Doji de soporte, Piercing Line
def detectar_velas(hist):
    """
    Analiza las últimas 3 velas diarias para detectar patrones de reversión alcista.
    Estas son exactamente las formaciones que Lu busca como confirmación en SMA8.
    """
    try:
        if len(hist) < 3:
            return "Sin datos", 0

        o  = hist["Open"].values
        h  = hist["High"].values
        c  = hist["Close"].values
        l  = hist["Low"].values

        # Vela de hoy (índice -1) y anteriores
        o1,h1,c1,l1 = o[-1],h[-1],c[-1],l[-1]   # hoy
        o2,h2,c2,l2 = o[-2],h[-2],c[-2],l[-2]   # ayer
        o3,h3,c3,l3 = o[-3],h[-3],c[-3],l[-3]   # anteayer

        cuerpo1     = abs(c1-o1)
        rango1      = h1-l1
        mecha_inf1  = min(o1,c1)-l1
        mecha_sup1  = h1-max(o1,c1)
        cuerpo2     = abs(c2-o2)
        alcista1    = c1 > o1
        bajista2    = c2 < o2

        fuerza = 0
        patron = "Sin patron claro"

        # 1. HAMMER (martillo) — La más importante para entradas en SMA8
        # Mecha inferior > 2x el cuerpo, mecha superior pequeña, cuerpo en parte alta
        if (rango1 > 0 and
            mecha_inf1 >= 2 * cuerpo1 and
            mecha_sup1 <= cuerpo1 * 0.5 and
            cuerpo1 >= rango1 * 0.15):
            patron = "Hammer (Martillo) — confirmacion fuerte en soporte"
            fuerza = 90

        # 2. BULLISH ENGULFING (envolvente alcista)
        # Vela verde de hoy envuelve completamente la vela roja de ayer
        elif (alcista1 and bajista2 and
              o1 <= c2 and c1 >= o2 and
              cuerpo1 > cuerpo2):
            patron = "Bullish Engulfing (Envolvente Alcista) — señal de compra fuerte"
            fuerza = 85

        # 3. PIERCING LINE (línea penetrante)
        # Vela verde que abre bajo el cierre de ayer y cierra sobre su mitad
        elif (alcista1 and bajista2 and
              o1 < l2 and
              c1 > (o2 + c2) / 2 and
              c1 < o2):
            patron = "Piercing Line — rebote alcista moderado"
            fuerza = 70

        # 4. MORNING STAR (estrella de la mañana) — 3 velas
        # Vela roja grande, vela pequeña (doji o spinning), vela verde grande
        elif (c3 < o3 and                          # anteayer: roja
              abs(c2-o2) < abs(c3-o3)*0.4 and      # ayer: cuerpo pequeño
              c1 > o1 and                           # hoy: verde
              c1 > (o3+c3)/2):                      # hoy cierra sobre mitad de hace 2 días
            patron = "Morning Star (Estrella de la Manana) — reversal de 3 velas"
            fuerza = 85

        # 5. DOJI de soporte (indecisión en soporte = señal alcista)
        # Cuerpo muy pequeño, mechas simétricas, necesita contexto de caída previa
        elif (rango1 > 0 and
              cuerpo1 <= rango1 * 0.1 and
              bajista2 and
              c2 < c3):                             # venía de 2 días bajistas
            patron = "Doji en soporte — indecision, esperar confirmacion manana"
            fuerza = 55

        # 6. VELA VERDE FUERTE (sin patrón específico pero alcista)
        elif (alcista1 and
              cuerpo1 >= rango1 * 0.6 and
              mecha_inf1 <= cuerpo1 * 0.3):
            patron = "Vela verde fuerte — momentum alcista"
            fuerza = 65

        # 7. DOJI en zona neutral
        elif rango1 > 0 and cuerpo1 <= rango1 * 0.1:
            patron = "Doji — mercado indeciso, sin confirmacion"
            fuerza = 30

        return patron, fuerza

    except:
        return "Error calculando velas", 0

# ── MEJORA 2: Earnings filter ─────────────────────────────────
def check_earnings_proximos(ticker):
    """
    Retorna True si hay earnings en menos de N días (riesgo de trampa).
    Muchas señales falsas ocurren justo antes de earnings.
    """
    try:
        tk = yf.Ticker(ticker)
        cal = tk.calendar
        if cal is None or cal.empty:
            return False, None
        # El calendario tiene fechas de earnings
        for col in cal.columns:
            fechas = cal[col].dropna().tolist()
            for f in fechas:
                if isinstance(f, (datetime, pd.Timestamp)):
                    dias = (f.date() - date.today()).days
                    if 0 <= dias <= CONFIG["earnings_dias_min"]:
                        return True, dias
        return False, None
    except:
        return False, None

# ── MEJORA 5: Multi-timeframe (semanal) ──────────────────────
def check_tendencia_semanal(ticker):
    """
    Verifica que la tendencia SEMANAL es alcista.
    En swing trading: si el semanal va en contra, no entrar aunque el diario señale.
    Retorna: (tendencia_ok, descripcion)
    """
    try:
        s    = yf.Ticker(ticker)
        hist = s.history(period="1y", interval="1wk")
        if hist.empty or len(hist) < 20:
            return True, "Sin datos semanales (OK por defecto)"
        c = hist["Close"]
        sma8w  = float(c.iloc[-8:].mean())
        sma20w = float(c.iloc[-20:].mean())
        precio = float(c.iloc[-1])

        if precio > sma8w > sma20w:
            return True, f"Semanal alcista (P>{sma8w:.1f}>SMA20w {sma20w:.1f})"
        elif precio > sma20w:
            return True, f"Semanal neutral-alcista (P sobre SMA20w {sma20w:.1f})"
        else:
            return False, f"Semanal bajista — precio bajo SMA20w {sma20w:.1f}"
    except:
        return True, "Error timeframe semanal (OK por defecto)"

# ── Sentiment via noticias Yahoo Finance ──────────────────────
def get_noticias_ticker(ticker):
    try:
        noticias = yf.Ticker(ticker).news
        if not noticias:
            return []
        return [n.get("title","") for n in noticias[:5] if n.get("title","")]
    except:
        return []

def get_sentiment_resumen(ticker):
    titulares = get_noticias_ticker(ticker)
    if not titulares:
        return None, "Sin noticias recientes en Yahoo Finance"
    texto = " | ".join(titulares[:3])
    positivas = ["beat","surge","jump","rise","higher","buy","upgrade","growth",
                 "strong","record","profit","rally","up","gain","outperform"]
    negativas = ["miss","fall","drop","decline","lower","sell","downgrade","loss",
                 "weak","cut","down","risk","concern","warn","below"]
    text_lower = texto.lower()
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
        vela_patron, vela_fuerza = detectar_velas(hist)   # MEJORA 6
        nombre=ticker; sector="N/A"
        try:
            info=s.info
            nombre=info.get("shortName",ticker)
            sector=info.get("sector","N/A")
        except: pass
        return {
            "ticker":ticker,"nombre":nombre,"sector":sector,
            "precio":precio,"prev":prev,"pct":pct,"es_pm":es_pm,
            "hist":hist,
            "sma8":sma(c,8),"sma20":sma(c,20),
            "sma50":sma(c,50),"sma200":sma(c,200),
            "ema8":float(ema(c,8).iloc[-1]),
            "ema20":float(ema(c,20).iloc[-1]),
            "ema50":float(ema(c,50).iloc[-1]),
            "ema200":float(ema(c,200).iloc[-1]),
            "rsi":calc_rsi(c),
            "macd":mv,"macd_s":sv,"macd_h":hv_m,"macd_e":me,
            "adx":av,"dip":dip,"dim":dim,"adx_e":ae,
            "atr":atr_val,
            "vh":vh,"vh_proy":vh_proy,"vp":vp,"vol_r":vol_r,
            "ath_52w":ath_52w,
            "vela_patron":vela_patron,"vela_fuerza":vela_fuerza,
            "error":None
        }
    except Exception as ex:
        return {"ticker":ticker,"error":str(ex)}

# ── Análisis + MEJORA 3: Score compuesto ─────────────────────
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

    # MEJORA 3: Score compuesto 0-100
    # Fan SMA:    0-40 pts (10 pts por cada condición)
    # RSI:        0-15 pts (zona óptima 50-65)
    # ADX:        0-15 pts (tendencia confirmada)
    # Volumen:    0-15 pts (participación del mercado)
    # Vela:       0-15 pts (confirmación de entrada)
    rsi_v = d["rsi"] if d["rsi"] else 0
    adx_v = d["adx"] if d["adx"] else 0

    pts_fan = fan * 10                           # max 40

    if 55 <= rsi_v <= 65:   pts_rsi = 15
    elif 50 <= rsi_v <= 70: pts_rsi = 10
    elif 45 <= rsi_v <= 72: pts_rsi = 5
    else:                    pts_rsi = 0

    if adx_v >= 30:  pts_adx = 15
    elif adx_v >= 25: pts_adx = 12
    elif adx_v >= 20: pts_adx = 8
    else:             pts_adx = 0

    if vol_r >= 2.0:  pts_vol = 15
    elif vol_r >= 1.5: pts_vol = 12
    elif vol_r >= 1.0: pts_vol = 8
    elif vol_r >= 0.7: pts_vol = 4
    else:              pts_vol = 0

    vela_fuerza = d.get("vela_fuerza", 0)
    pts_vela = int(vela_fuerza * 0.15)          # max 15

    score = pts_fan + pts_rsi + pts_adx + pts_vol + pts_vela
    score = min(100, score)

    return {
        "fan":fan,"c1":c1,"c2":c2,"c3":c3,"c4":c4,
        "en_rango":en_rango,"vol_r":vol_r,"vol_ok":vol_ok,
        "senal_tardia":tardia,
        "score":score,
        "score_detalle": f"Fan:{pts_fan}+RSI:{pts_rsi}+ADX:{pts_adx}+Vol:{pts_vol}+Vela:{pts_vela}",
        "e20":p>d["ema20"],"e50":p>d["ema50"],"e200":p>d["ema200"]
    }

def posicion(precio, ath_52w=None):
    sp=CONFIG["stop_loss_pct"]/100
    stop=round(precio*(1-sp),2); rx=precio-stop
    acc=max(1,int(CONFIG["riesgo_fijo_usd"]/rx))
    tot=round(acc*precio,2); perd=round(acc*rx,2)
    t1=round(precio+1*rx,2); t2=round(precio+2*rx,2); t3=round(precio+3*rx,2)
    if ath_52w and ath_52w > precio:
        techo=round(ath_52w*0.98,2)
        if techo > t1:
            if t2>techo: t2=techo
            if t3>techo: t3=techo
    rr=round((t1-precio)/rx,2)
    return {"acc":acc,"tot":tot,"stop":stop,"perd":perd,
            "t1":t1,"t2":t2,"t3":t3,"rx":rx,"rr":rr}

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

SECTOR_ETF = {
    "energy":"XLE","technology":"QQQ","healthcare":"XLV",
    "financial":"XLF","basic materials":"XLB","basic mate":"XLB",
    "consumer cyclical":"XLY","consumer d":"XLY",
    "consumer defensive":"XLP","industrials":"XLI",
    "real estate":"XLRE","utilities":"XLU","communication":"XLC",
}
def get_sector_etf(sector):
    s=sector.lower()
    for k,v in SECTOR_ETF.items():
        if k in s: return v
    return "SPY"

def get_vix():
    try:
        fi=yf.Ticker("^VIX").fast_info
        v=float(getattr(fi,"last_price",None) or 0)
        if v>0:
            nivel="ALTO/MIEDO" if v>25 else "MODERADO" if v>18 else "BAJO/CALMA"
            return round(v,1),nivel
    except: pass
    return None,"N/D"

# ── MEJORA 1: IA con web_search en tiempo real ────────────────
# Claude busca noticias actuales del ticker y su sector antes de analizar.
# Esto es el "alternative data" que diferencia a Solares de otros sistemas.

def analizar_ia(d, a, pos, sentiment_score, tendencia_semanal):
    if not CLAUDE_API_KEY:
        return {"prob":0,"senal":"SIN IA","contexto":"","razon":"Sin API key","alerta":""}
    try:
        client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v   = d["rsi"] if d["rsi"] else 0
        atr_v   = d["atr"] if d["atr"] else 0
        atr_pct = round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
        sent_s  = f"{sentiment_score}%" if sentiment_score else "N/D"
        sector  = d.get("sector","N/A")
        etf_ref = get_sector_etf(sector)
        vix_v, vix_nivel = get_vix()
        vix_str = f"{vix_v} ({vix_nivel})" if vix_v else "N/D"
        tardia  = "SI" if a.get("senal_tardia") else "NO"

        prompt = (
            f"Analiza {d['ticker']} ({d['nombre']}) — swing trading 5-10 días.\n\n"
            f"TÉCNICOS DIARIOS:\n"
            f"Precio: {d['precio']:.2f} USD ({d['pct']:+.2f}%) | Fan SMA: {a['fan']}/4\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"ATR: {atr_pct}% | Volumen: {a['vol_r']:.1f}x promedio 20d\n"
            f"Entrada tardía (>2% SMA8): {tardia}\n"
            f"Score Sistema Sirio: {a['score']}/100 ({a['score_detalle']})\n\n"
            f"VELA JAPONESA HOY: {d.get('vela_patron','N/D')} "
            f"(fuerza: {d.get('vela_fuerza',0)}%)\n\n"
            f"CONTEXTO:\n"
            f"VIX: {vix_str} | ETF sector: {etf_ref}\n"
            f"Tendencia semanal: {tendencia_semanal}\n"
            f"Sentiment noticias: {sent_s}\n"
            f"Stop: {pos['stop']:.2f} | T1: {pos['t1']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Usa web_search para buscar noticias de HOY sobre {d['ticker']} y {sector}.\n"
            f"Responde en español con tildes y acentos correctos, en formato EXACTO:\n"
            f"PROBABILIDAD: [0-100]\n"
            f"SIGNAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"CONTEXTO: [1 frase: VIX + {etf_ref} + catalizador actual del sector]\n"
            f"RAZON: [1 frase sobre setup técnico y vela]\n"
            f"ALERTA: [nivel o evento clave a vigilar]"
        )

        # MEJORA 1: web_search tool para noticias en tiempo real
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search"
            }],
            system=(
                "Eres el analizador del Sistema Sirio de Solares Trading. "
                "SIEMPRE usa web_search para buscar noticias actuales del ticker antes de responder. "
                "Conoces correlaciones sector/ETF y contexto macro. "
                "Responde en español correcto con tildes. Formato exacto."
            ),
            messages=[{"role":"user","content":prompt}]
        )

        # Extraer texto de la respuesta (puede haber tool_use blocks)
        txt = ""
        for block in msg.content:
            if hasattr(block, "text"):
                txt += block.text + "\n"

        pr,se,ctx,ra,al = 0,"ESPERAR","","",""
        for ln in txt.splitlines():
            ln=ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr=int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SIGNAL:"):
                se=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("CONTEXTO:"):
                ctx=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:"):
                ra=ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"):
                al=ln.split(":",1)[1].strip()
        return {"prob":pr,"senal":se,"contexto":ctx,"razon":ra,"alerta":al}
    except Exception as ex:
        return {"prob":0,"senal":"ERROR","contexto":"","razon":str(ex)[:80],"alerta":""}

# ── MEJORA 7: Telegram con HTML (tildes perfectas) ────────────
def send_telegram(msg, modo="html"):
    """
    Usa parse_mode HTML en vez de Markdown.
    HTML maneja tildes, acentos y ñ perfectamente.
    En el mensaje: usa <b>texto</b> para negrita, <i>texto</i> para cursiva.
    """
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=30)
        data = r.json()
        if r.status_code==200 and data.get("ok"):
            print(f"  [TG] OK id:{data['result']['message_id']}")
            return True
        # Reintento sin formato
        print(f"  [TG] Reintentando sin formato ({data.get('description','')})")
        r2 = requests.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":msg}, timeout=30)
        if r2.status_code==200 and r2.json().get("ok"):
            print(f"  [TG] OK (sin formato)")
            return True
        print(f"  [TG] Error: {r2.json().get('description','')}")
        return False
    except Exception as ex:
        print(f"  [TG] Excepción: {ex}")
        return False

def build_msg(d, a, pos, ia, sent_texto, tendencia_semanal, earnings_aviso=""):
    """
    MEJORA 7: Usa HTML en vez de Markdown (* → <b>, _ → <i>).
    HTML soporta tildes, ñ y acentos perfectamente.
    """
    hora   = hora_et()
    pm_tag = " [PRE-MARKET]" if d.get("es_pm") else ""
    rsi_v  = d["rsi"] if d["rsi"] else 0
    atr_v  = d["atr"] if d["atr"] else 0
    atr_pct= round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0

    if rsi_v < CONFIG["rsi_min"]:    rsi_tag = "débil"
    elif rsi_v > CONFIG["rsi_max"]:  rsi_tag = "sobrecomprado"
    else:                             rsi_tag = "OK"

    vol_r   = a["vol_r"]
    vol_tag = ("🔥 ALTO — {:.1f}x".format(vol_r) if vol_r>=1.5
               else "✅ normal — {:.1f}x".format(vol_r) if vol_r>=0.8
               else "⚠️ BAJO — {:.1f}x".format(vol_r))

    tardia_v = "\n⚠️ AVISO: precio extendido &gt;2% sobre SMA8" if a.get("senal_tardia") else ""

    p1,p2,p3 = calc_probabilidades(a["fan"], d["adx"], rsi_v, vol_r)
    acc = pos["acc"]
    s25 = max(1,round(acc*0.25)); s30=max(1,round(acc*0.30))
    s20 = max(1,round(acc*0.20)); s25b=max(0,acc-s25-s30-s20)

    ath = d.get("ath_52w",0)
    dist_ath = (f"ATH 52s: {ath:.2f}  (-{((ath-d['precio'])/ath*100):.1f}%)"
                if ath>d["precio"] else f"ATH 52s: {ath:.2f}  (zona ATH)")

    etf_ref  = get_sector_etf(d.get("sector","N/A"))
    macd_ico = "🟢" if "Bull" in d["macd_e"] else "🔴"
    ia_ico   = "🚀" if ia["prob"]>=70 else "⚡" if ia["prob"]>=55 else "⏸"

    # Score visual
    score    = a.get("score",0)
    score_bar= "█"*int(score/10) + "░"*(10-int(score/10))
    vela_ico = "🕯️" if d.get("vela_fuerza",0)>=70 else "📊"

    # HTML: & → &amp;  < → &lt;  > → &gt;
    ctx  = ia.get("contexto","N/D").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    razon= ia.get("razon","").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    alerta=ia.get("alerta","").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    return (
        f"🌟 <b>SISTEMA SIRIO — Solares</b>\n"
        f"🕐 {hora}{pm_tag}\n"
        f"📈 Swing DIARIO 5-10 días\n\n"

        f"<b>{d['ticker']}</b> {d.get('nombre','')}\n"
        f"🏭 Sector: {d.get('sector','N/A')} | Ref: <b>{etf_ref}</b>\n"
        f"💲 {d['precio']:.2f} USD ({d['pct']:+.1f}%){tardia_v}\n"
        f"🏔 {dist_ath}\n"
        + (f"⚠️ {earnings_aviso}\n" if earnings_aviso else "") +
        f"\n"

        f"<b>📊 Abanico SMA OK 4/4</b>\n"
        f"{'✅' if a['c1'] else '❌'} Precio &gt; SMA8    {d['sma8']:.2f}\n"
        f"{'✅' if a['c2'] else '❌'} SMA8   &gt; SMA20   {d['sma20']:.2f}\n"
        f"{'✅' if a['c3'] else '❌'} SMA20  &gt; SMA50   {d['sma50']:.2f}\n"
        f"{'✅' if a['c4'] else '❌'} SMA50  &gt; SMA200  {d['sma200']:.2f}\n\n"

        f"<b>🔬 Indicadores</b>\n"
        f"MACD: {macd_ico} {d['macd_e']}\n"
        f"RSI: {rsi_v:.0f} ({rsi_tag})\n"
        f"ADX: {d['adx_e']} ({d['adx']:.0f})\n"
        f"ATR: {atr_pct}% diario esperado\n"
        f"Vol: {vol_tag}\n"
        f"<i>(vs promedio últimos 20 días)</i>\n\n"

        f"<b>{vela_ico} Vela japonesa detectada</b>\n"
        f"{d.get('vela_patron','Sin patrón')} ({d.get('vela_fuerza',0)}% fuerza)\n\n"

        f"<b>📈 Tendencia semanal</b>\n"
        f"{tendencia_semanal}\n\n"

        f"<b>📰 Noticias</b>\n"
        f"{sent_texto}\n\n"

        f"<b>🎯 Score Sistema Sirio: {score}/100</b>\n"
        f"{score_bar}\n"
        f"<i>{a.get('score_detalle','')}</i>\n\n"

        f"<b>💰 Posición</b>\n"
        f"Entrada: {d['precio']:.2f} USD | <b>{pos['acc']} acc</b> | Capital: {pos['tot']:.0f} USD\n"
        f"🛑 Stop: {pos['stop']:.2f} (-6% / -1R)\n"
        f"🎯 T1: {pos['t1']:.2f} (+{((pos['t1']/d['precio'])-1)*100:.1f}%)\n"
        f"🎯 T2: {pos['t2']:.2f} (+{((pos['t2']/d['precio'])-1)*100:.1f}%)\n"
        f"🎯 T3: {pos['t3']:.2f} (+{((pos['t3']/d['precio'])-1)*100:.1f}%)\n"
        f"🚀 Runner: trail EMA8\n"
        f"R/R: {pos['rr']:.1f}x | Riesgo: {pos['perd']:.0f} USD\n\n"

        f"<b>🗓 Gestión de Salida — Sistema Sirio</b>\n"
        f"25%({s25}) T1 | 30%({s30}) T2 | 20%({s20}) T3 | 25%({s25b}) trail\n"
        f"⏱ Time-stop: 7 días sin T1 → salida total\n\n"

        f"<b>📊 Probabilidades</b>\n"
        f"T1: {p1}% | T2: {p2}% | T3: {p3}%\n\n"

        f"<b>{ia_ico} IA {ia['prob']}% — {ia['senal']}</b>\n"
        f"🌍 {ctx}\n"
        f"📐 {razon}\n"
        f"👁 Vigilar: {alerta}\n\n"

        f"<i>Sistema Sirio v6 — Solares</i> 🌟\n"
        f"─────────────────────────\n"
        f"⚠️ <i>AVISO LEGAL: Esta información tiene carácter "
        f"exclusivamente educativo e informativo. No constituye "
        f"asesoramiento financiero ni recomendación de inversión. "
        f"Los mercados implican riesgo de pérdida de capital. "
        f"Cada persona es responsable de sus propias decisiones. "
        f"Solares no gestiona fondos de terceros.</i>"
    )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"=== SISTEMA SIRIO — Solares v6  {hora_et()} ===")
    estado    = cargar_estado()
    ya        = estado.get("alertados",[])
    count     = estado.get("count",0)

    if count >= CONFIG["max_alertas_dia"]:
        print(f"Máximo {CONFIG['max_alertas_dia']} alertas alcanzado."); return

    tickers    = obtener_universo()
    pendientes = [t for t in tickers if t not in ya]
    print(f"A revisar: {len(pendientes)}  (ya alertados hoy: {len(ya)})")

    nuevas = 0
    for ticker in pendientes:
        if count+nuevas >= CONFIG["max_alertas_dia"]: break
        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)
        if d.get("error"):
            print(f"skip ({d['error'][:40]})"); continue

        rsi_s = f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        print(f"{d['precio']:.2f} RSI:{rsi_s} MACD:{d['macd_e']} [{d.get('sector','?')[:10]}]")

        a = analizar(d)

        # Filtros base
        if a["fan"] < CONFIG["min_fan_to_alert"] or not a["en_rango"] or not a["vol_ok"]:
            continue

        # Señal tardía sin fan completo
        if a.get("senal_tardia") and a["fan"] < 4:
            print(f"  skip (tardía >2% SMA8 con fan {a['fan']}/4)")
            continue

        # MEJORA 3: Score compuesto mínimo
        if a["score"] < CONFIG["score_minimo"]:
            print(f"  skip (score {a['score']}/100 < mínimo {CONFIG['score_minimo']})")
            continue

        # MEJORA 2: Earnings filter
        hay_earnings, dias_earn = check_earnings_proximos(ticker)
        if hay_earnings:
            print(f"  skip (earnings en {dias_earn} días — riesgo trampa)")
            continue
        earnings_aviso = ""

        # MEJORA 5: Multi-timeframe semanal
        semanal_ok, tendencia_semanal = check_tendencia_semanal(ticker)
        if not semanal_ok:
            print(f"  skip ({tendencia_semanal})")
            continue

        print(f"  *** FAN 4/4 | Score {a['score']}/100 | Vela: {d.get('vela_patron','?')[:40]}")
        print(f"  Semanal: {tendencia_semanal[:50]}")

        sent_score, sent_texto = get_sentiment_resumen(ticker)
        pos = posicion(d["precio"], d.get("ath_52w"))
        ia  = analizar_ia(d, a, pos, sent_score, tendencia_semanal)
        print(f"  IA:{ia['prob']}% {ia['senal']}")

        msg = build_msg(d, a, pos, ia, sent_texto, tendencia_semanal, earnings_aviso)
        ok  = send_telegram(msg)

        if ok:
            nuevas += 1; ya.append(ticker)
            estado["alertados"] = ya; estado["count"] = count+nuevas
            guardar_estado(estado)
            # MEJORA 4: registrar en backtest
            registrar_backtest(
                ticker, d["precio"], pos["stop"],
                pos["t1"], pos["t2"], pos["t3"],
                a["score"], ia["senal"], d.get("vela_patron","")
            )
            print(f"  Enviado ({count+nuevas}/{CONFIG['max_alertas_dia']} hoy)")
        else:
            print(f"  Error Telegram")

    print(f"=== Fin: {nuevas} alertas nuevas ===")

    if nuevas == 0 and count < CONFIG["max_alertas_dia"]:
        send_telegram(
            f"🔭 <b>SISTEMA SIRIO — Solares</b>\n"
            f"{hora_et()}\n\n"
            f"⚙️ Universo escaneado: {len(pendientes)} tickers\n"
            f"📭 Sin coincidencias esta pasada\n"
            f"✅ Alertas enviadas hoy: {count}/{CONFIG['max_alertas_dia']}\n\n"
            f"<i>Esperar es la posición. Estás protegida.</i>\n\n"
            f"<i>Sistema Sirio v6 — Solares</i> 🌟"
        )

if __name__ == "__main__":
    main()
