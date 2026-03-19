# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
#  Solares — Sistema Sirio Bot  v6.0  (DEFINITIVO)
#  Trader : Lu Espitia  |  Solares Trading
# ═══════════════════════════════════════════════════════════════
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os, json, time
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
    "score_minimo":        55,
    "earnings_dias_min":   5,
    "max_tickers_scan":    200,
    "max_alertas_dia":     5,
    "state_file":          "/tmp/sirio_alertas_hoy.json",
    "backtest_file":       "/tmp/sirio_backtest.json",
    "diario_file":         "/tmp/sirio_diario_sentiment.json",
    "watchlist_prioritaria": [
        "KGC","OXY","SLB","BTU","PAAS","SQM","FCX","NEM","AEM","WPM",
        "AG","GOLD","CVX","XOM","COP","DVN","FANG","MRO","HAL","BKR",
        "HIMS","EW","DAR","GDX","JEPI","SCHD","IBIT","MARA","RIOT",
    ]
}

def cargar_estado():
    try:
        with open(CONFIG["state_file"],"r",encoding="utf-8") as f:
            e=json.load(f)
        if e.get("fecha")!=str(date.today()):
            return {"fecha":str(date.today()),"alertados":[],"count":0}
        return e
    except:
        return {"fecha":str(date.today()),"alertados":[],"count":0}

def guardar_estado(e):
    try:
        with open(CONFIG["state_file"],"w",encoding="utf-8") as f:
            json.dump(e,f)
    except:
        pass

def registrar_backtest(ticker,precio,stop,t1,t2,t3,score,ia_senal,vela):
    try:
        log=[]
        try:
            with open(CONFIG["backtest_file"],"r") as f: log=json.load(f)
        except: pass
        log.append({"fecha":str(date.today()),"hora":hora_et(),"ticker":ticker,
                    "entrada":precio,"stop":stop,"t1":t1,"t2":t2,"t3":t3,
                    "score":score,"ia":ia_senal,"vela":vela,
                    "resultado":"PENDIENTE","pct":None})
        with open(CONFIG["backtest_file"],"w") as f:
            json.dump(log[-300:],f,indent=2)
        print(f"  [BT] #{len(log)} registrada")
    except Exception as ex:
        print(f"  [BT] Error: {ex}")


# ─────────────────────────────────────────────────────────────
#  CREDENCIALES DE REDES SOCIALES (desde GitHub Secrets)
#  Reddit:     REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET,
#              REDDIT_USERNAME, REDDIT_PASSWORD
#  StockTwits: STOCKTWITS_TOKEN
#  Si no están configurados, el bot sigue funcionando con Yahoo
# ─────────────────────────────────────────────────────────────
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME      = os.environ.get("REDDIT_USERNAME", "")
REDDIT_PASSWORD      = os.environ.get("REDDIT_PASSWORD", "")
STOCKTWITS_TOKEN     = os.environ.get("STOCKTWITS_TOKEN", "")

def _get_reddit_token():
    """
    OAuth2 de Reddit via script app.
    Con token propio, las IPs de GitHub Actions ya no son bloqueadas.
    """
    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        return None
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "password",
                  "username": REDDIT_USERNAME,
                  "password": REDDIT_PASSWORD},
            headers={"User-Agent": "SistemaSirio/1.0"},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("access_token")
    except:
        pass
    return None

# Cache del token Reddit para no pedir uno nuevo por cada ticker
_REDDIT_TOKEN_CACHE = {"token": None, "ts": 0}

def _reddit(ticker):
    """
    Busca menciones del ticker en Reddit.
    - Si hay credenciales: usa OAuth (autenticado, no bloqueado)
    - Si no hay credenciales: intenta acceso anónimo como antes
    """
    global _REDDIT_TOKEN_CACHE

    # Obtener/reusar token (válido 60 min, recachear cada 50 min)
    ahora = time.time()
    if not _REDDIT_TOKEN_CACHE["token"] or (ahora - _REDDIT_TOKEN_CACHE["ts"]) > 3000:
        token = _get_reddit_token()
        _REDDIT_TOKEN_CACHE = {"token": token, "ts": ahora}

    token = _REDDIT_TOKEN_CACHE["token"]

    if token:
        # Modo autenticado — usa oauth.reddit.com, no bloqueado
        hdrs = {
            "Authorization": f"bearer {token}",
            "User-Agent":    "SistemaSirio/1.0"
        }
        base_url = "https://oauth.reddit.com"
    else:
        # Modo anónimo — puede ser bloqueado desde GitHub Actions
        hdrs = {"User-Agent": "SistemaSirio/1.0 (Solares Trading Research)"}
        base_url = "https://www.reddit.com"

    resultados = []
    for sub in ["wallstreetbets","stocks","investing","options"]:
        try:
            url = (f"{base_url}/r/{sub}/search.json"
                   f"?q=%24{ticker}&sort=new&limit=20&t=day&restrict_sr=1")
            r = requests.get(url, headers=hdrs, timeout=10)
            if r.status_code != 200: continue
            for p in r.json().get("data",{}).get("children",[]):
                d2 = p["data"]
                resultados.append({
                    "titulo": d2.get("title","")[:100],
                    "upvote": d2.get("upvote_ratio", 0.5),
                    "score":  d2.get("score", 0)
                })
        except:
            continue

    if not resultados: return None
    n = len(resultados)
    avg_up = sum(r["upvote"] for r in resultados) / n
    sc = int(avg_up * 100)
    lbl = ("Bullish" if sc>=65 else "Lev.Bull" if sc>=55
           else "Neutral" if sc>=45 else "Lev.Bear" if sc>=35 else "Bearish")
    top = [r["titulo"] for r in sorted(resultados, key=lambda x:x["score"], reverse=True)[:3]]
    modo = "🔐 auth" if token else "🔓 anon"
    return {"n":n,"score":sc,"label":lbl,"top":top,
            "detalle":f"{n} posts Reddit ({modo}), upvote {avg_up:.0%}"}

def _stocktwits(ticker):
    """
    Sentiment de StockTwits.
    - Si hay STOCKTWITS_TOKEN: usa autenticación → más datos, no bloqueado
    - Si no: intenta acceso público
    """
    try:
        if STOCKTWITS_TOKEN:
            url = (f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
                   f"?access_token={STOCKTWITS_TOKEN}")
            hdrs = {"User-Agent": "SistemaSirio/1.0"}
        else:
            url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
            hdrs = {"User-Agent": "Mozilla/5.0"}

        r = requests.get(url, timeout=10, headers=hdrs)
        if r.status_code != 200: return None
        msgs = r.json().get("messages", [])
        if not msgs: return None

        bulls = sum(1 for m in msgs if m.get("entities",{}).get("sentiment",{}).get("basic")=="Bullish")
        bears = sum(1 for m in msgs if m.get("entities",{}).get("sentiment",{}).get("basic")=="Bearish")
        tot = bulls + bears
        if tot == 0: sc=50; lbl="Neutral"
        else:
            sc = int(bulls/tot*100)
            lbl = ("Bullish" if sc>=65 else "Lev.Bull" if sc>=55
                   else "Neutral" if sc>=45 else "Lev.Bear" if sc>=35 else "Bearish")
        modo = "🔐 auth" if STOCKTWITS_TOKEN else "🔓 anon"
        return {"n":len(msgs),"bulls":bulls,"bears":bears,"score":sc,"label":lbl,
                "detalle":f"{len(msgs)} msgs StockTwits ({modo}) — {bulls}🐂 {bears}🐻"}
    except:
        return None


def _yahoo_news(ticker):
    try:
        news=yf.Ticker(ticker).news
        if not news: return None
        titulares=[n.get("title","") for n in news[:5] if n.get("title","")]
        if not titulares: return None
        txt=" ".join(titulares).lower()
        pos=sum(1 for w in BULL_KW if w in txt)
        neg=sum(1 for w in BEAR_KW if w in txt)
        tot=pos+neg
        if tot==0: sc=50; lbl="Neutral"
        else:
            sc=int(pos/tot*100)
            lbl=("Bullish" if sc>=65 else "Lev.Bull" if sc>=55
                 else "Neutral" if sc>=45 else "Lev.Bear" if sc>=35 else "Bearish")
        return {"n":len(titulares),"score":sc,"label":lbl,"titulares":titulares[:3]}
    except: return None

def get_sentiment_completo(ticker):
    """
    Reddit y StockTwits a veces bloquean IPs de datacenter (GitHub Actions).
    Cuando eso pasa, usamos solo Yahoo Finance que siempre funciona.
    El score se ajusta para reflejar la fuente disponible.
    """
    print(f"    Sentiment {ticker}...",end=" ",flush=True)
    rd=_reddit(ticker); st=_stocktwits(ticker); yh=_yahoo_news(ticker)
    scores=[]; pesos=[]; secciones=[]
    if rd:
        scores.append(rd["score"]); pesos.append(35)
        tops="\n".join(f"  · {t}" for t in rd["top"]) if rd["top"] else ""
        secciones.append(f"📱 <b>Reddit</b>: {rd['label']} ({rd['score']}%) — {rd['detalle']}\n{tops}")
    if st:
        scores.append(st["score"]); pesos.append(40)
        secciones.append(f"💬 <b>StockTwits</b>: {st['label']} ({st['score']}%) — {st['detalle']}")
    if yh:
        scores.append(yh["score"]); pesos.append(25 if (rd or st) else 100)
        nots="\n".join(f"  · {t[:80]}" for t in yh["titulares"])
        secciones.append(f"📰 <b>Yahoo Finance</b>: {yh['label']} ({yh['score']}%)\n{nots}")

    fuentes=sum(1 for x in [rd,st,yh] if x)

    if not scores:
        # Sin ninguna fuente — neutral por defecto, no bloquea la señal
        print("sin datos (neutral)")
        texto="⚪ <b>Sentiment: Neutral</b> — sin datos de redes disponibles\n<i>(Reddit/StockTwits bloqueados desde servidor — normal en GitHub Actions)</i>"
        datos={"score_final":50,"label":"⚪ Neutral","reddit":None,"stocktwits":None,"yahoo":None,"fuentes":0}
        return 50, texto, datos

    peso_total=sum(pesos)
    sc_final=int(sum(s*p for s,p in zip(scores,pesos))/peso_total)
    etiq=("🟢 BULLISH" if sc_final>=65 else "🟡 Lev.Bull" if sc_final>=55
          else "⚪ Neutral" if sc_final>=45 else "🟠 Lev.Bear" if sc_final>=35 else "🔴 BEARISH")

    aviso=""
    if fuentes==1 and not rd and not st:
        aviso="\n<i>(Solo Yahoo Finance disponible — Reddit/StockTwits bloqueados desde servidor)</i>"

    print(f"{sc_final}% {etiq} ({fuentes} fuentes)")
    texto=(f"<b>Score Solares: {sc_final}% bullish — {etiq}</b>\n"
           f"<i>({fuentes} fuentes activas)</i>{aviso}\n\n"
           +"\n\n".join(secciones))
    datos={"score_final":sc_final,"label":etiq,"reddit":rd,"stocktwits":st,"yahoo":yh,"fuentes":fuentes}
    return sc_final,texto,datos

def guardar_en_diario(ticker,precio,fan,sent_datos,ia_senal,vela):
    try:
        diario=[]
        try:
            with open(CONFIG["diario_file"],"r") as f: diario=json.load(f)
        except: pass
        rd=sent_datos.get("reddit") or {}
        st=sent_datos.get("stocktwits") or {}
        yh=sent_datos.get("yahoo") or {}
        diario.append({
            "fecha":str(date.today()),"ticker":ticker,"precio":precio,
            "fan_sma":fan,"ia_senal":ia_senal,"vela":vela,
            "score_final":sent_datos.get("score_final"),
            "reddit_sc":rd.get("score"),"reddit_n":rd.get("n",0),
            "st_sc":st.get("score"),"st_n":st.get("n",0),
            "yahoo_sc":yh.get("score"),
            "resultado":"PENDIENTE","pct_max":None
        })
        with open(CONFIG["diario_file"],"w") as f:
            json.dump(diario[-500:],f,indent=2,ensure_ascii=False)
        print(f"  [DIARIO] {len(diario)} entradas")
    except Exception as ex:
        print(f"  [DIARIO] Error: {ex}")

def obtener_universo():
    print("Obteniendo universo Finviz...")
    from html.parser import HTMLParser
    class FP(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tickers=[]; self.capture=False
        def handle_starttag(self,tag,attrs):
            d=dict(attrs)
            if tag=="a" and d.get("class")=="screener-link-primary": self.capture=True
        def handle_data(self,data):
            if self.capture:
                t=data.strip()
                if t and t.replace("-","").isalpha() and len(t)<=5: self.tickers.append(t)
                self.capture=False
    tickers_fv=[]; hdrs={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for r0 in [1,21,41,61,81,101]:
        try:
            url=("https://finviz.com/screener.ashx?v=111"
                 "&f=sh_price_o10,sh_price_u150,sh_avgvol_o500,ta_sma200_pa,ta_sma50_pa"
                 f"&ft=4&o=-volume&r={r0}")
            resp=requests.get(url,headers=hdrs,timeout=30)
            if resp.status_code!=200: break
            fp=FP(); fp.feed(resp.text)
            nuevos=[t for t in fp.tickers if t not in tickers_fv]
            if not nuevos: break
            tickers_fv.extend(nuevos)
            print(f"  Finviz p{r0}: +{len(nuevos)} (total:{len(tickers_fv)})")
        except Exception as ex:
            print(f"  Error Finviz p{r0}: {ex}"); break
    combinados=list(CONFIG["watchlist_prioritaria"])
    for t in tickers_fv:
        if t not in combinados: combinados.append(t)
    resultado=combinados[:CONFIG["max_tickers_scan"]]
    print(f"Universo total: {len(resultado)} tickers")
    return resultado

def ema(s,p): return s.ewm(span=p,adjust=False).mean()
def sma(s,p): return float(s.iloc[-p:].mean()) if len(s)>=p else None

def calc_rsi(c,p=14):
    if len(c)<p+1: return None
    d=c.diff(); g=d.where(d>0,0.0); l=-d.where(d<0,0.0)
    ag=g.ewm(com=p-1,min_periods=p).mean(); al=l.ewm(com=p-1,min_periods=p).mean()
    return float((100-(100/(1+ag/al))).iloc[-1])

def calc_macd(c):
    if len(c)<35: return None,None,None,"N/A"
    ml=ema(c,12)-ema(c,26); sl=ema(ml,9); hl=ml-sl
    mv,sv,hv=float(ml.iloc[-1]),float(sl.iloc[-1]),float(hl.iloc[-1])
    if mv>sv and hv>0: e="Bullish"
    elif mv>sv: e="Weak Bull"
    elif mv<sv and hv<0: e="Bearish"
    else: e="Weak Bear"
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

def proyectar_volumen(vh,vp):
    try:
        ahora=datetime.now(ET)
        apertura=ahora.replace(hour=9,minute=30,second=0,microsecond=0)
        cierre=ahora.replace(hour=16,minute=0,second=0,microsecond=0)
        if ahora<apertura or ahora>=cierre: return (vh/vp if vp>0 else 0),vh
        mins_total=390.0; mins_trans=max(1,(ahora-apertura).seconds/60)
        factor=mins_total/mins_trans; vh_proy=vh*factor
        return (vh_proy/vp if vp>0 else 0),vh_proy
    except:
        return (vh/vp if vp>0 else 0),vh

def detectar_velas(hist):
    try:
        if len(hist)<3: return "Sin datos suficientes",0
        o=hist["Open"].values; h=hist["High"].values
        c=hist["Close"].values; l=hist["Low"].values
        o1,h1,c1,l1=o[-1],h[-1],c[-1],l[-1]
        o2,h2,c2,l2=o[-2],h[-2],c[-2],l[-2]
        o3,h3,c3,l3=o[-3],h[-3],c[-3],l[-3]
        cuerpo1=abs(c1-o1); rango1=h1-l1 if h1-l1>0 else 0.0001
        mecha_inf=min(o1,c1)-l1; mecha_sup=h1-max(o1,c1)
        cuerpo2=abs(c2-o2); alcista1=c1>o1; bajista2=c2<o2
        if mecha_inf>=2*cuerpo1 and mecha_sup<=cuerpo1*0.5 and cuerpo1>=rango1*0.15:
            return "🔨 Hammer (Martillo) — confirmación fuerte en soporte",90
        if alcista1 and bajista2 and o1<=c2 and c1>=o2 and cuerpo1>cuerpo2:
            return "🟢 Bullish Engulfing — señal de compra fuerte",85
        if c3<o3 and abs(c2-o2)<abs(c3-o3)*0.4 and c1>o1 and c1>(o3+c3)/2:
            return "⭐ Morning Star — reversión de 3 velas",85
        if alcista1 and bajista2 and o1<l2 and c1>(o2+c2)/2 and c1<o2:
            return "📈 Piercing Line — rebote alcista moderado",70
        if cuerpo1<=rango1*0.1 and bajista2 and c2<c3:
            return "⚖️ Doji en soporte — esperar confirmación mañana",55
        if alcista1 and cuerpo1>=rango1*0.6 and mecha_inf<=cuerpo1*0.3:
            return "💚 Vela verde fuerte — momentum alcista",65
        if cuerpo1<=rango1*0.1:
            return "↔️ Doji neutro — mercado indeciso",30
        return "Vela sin patrón específico",40
    except:
        return "Error calculando velas",0

def check_earnings_proximos(ticker):
    try:
        cal=yf.Ticker(ticker).calendar
        if cal is None or cal.empty: return False,None
        for col in cal.columns:
            for f in cal[col].dropna().tolist():
                if isinstance(f,(datetime,pd.Timestamp)):
                    dias=(f.date()-date.today()).days
                    if 0<=dias<=CONFIG["earnings_dias_min"]: return True,dias
        return False,None
    except:
        return False,None

def check_tendencia_semanal(ticker):
    try:
        hist=yf.Ticker(ticker).history(period="1y",interval="1wk")
        if hist.empty or len(hist)<20: return True,"Sin datos semanales (OK)"
        c=hist["Close"]
        sma8w=float(c.iloc[-8:].mean()); sma20w=float(c.iloc[-20:].mean()); precio=float(c.iloc[-1])
        if precio>sma8w>sma20w:
            return True,f"✅ Semanal alcista (P &gt; SMA8w {sma8w:.1f} &gt; SMA20w {sma20w:.1f})"
        elif precio>sma20w:
            return True,f"⚠️ Semanal neutral-alcista (sobre SMA20w {sma20w:.1f})"
        else:
            return False,f"❌ Semanal bajista — bajo SMA20w {sma20w:.1f}"
    except:
        return True,"Error timeframe semanal (OK)"

def obtener_datos(ticker):
    try:
        s=yf.Ticker(ticker)
        hist=s.history(period="1y",interval="1d",prepost=True)
        if hist.empty or len(hist)<210: return {"ticker":ticker,"error":"Datos insuficientes"}
        c,h,l,v=hist["Close"],hist["High"],hist["Low"],hist["Volume"]
        prev=float(c.iloc[-2]); es_pm=False; precio=float(c.iloc[-1])
        try:
            fi=s.fast_info
            lp=float(getattr(fi,"last_price",None) or 0)
            pm=float(getattr(fi,"pre_market_price",None) or 0)
            if lp>0: precio=lp
            elif pm>0: precio=pm; es_pm=True
        except: pass
        pct=(precio-prev)/prev*100
        vh=float(v.iloc[-1]) if float(v.iloc[-1])>0 else float(v.iloc[-2])
        vp=float(v.iloc[-20:].mean())
        vol_r,vh_proy=proyectar_volumen(vh,vp)
        ath_52w=float(h.iloc[-252:].max()) if len(h)>=252 else float(h.max())
        mv,sv,hv_m,me=calc_macd(c); av,dip,dim,ae=calc_adx(h,l,c)
        atr_val=calc_atr(h,l,c); vela_patron,vela_fuerza=detectar_velas(hist)
        nombre=ticker; sector="N/A"
        try:
            info=s.info; nombre=info.get("shortName",ticker); sector=info.get("sector","N/A")
        except: pass
        return {"ticker":ticker,"nombre":nombre,"sector":sector,
                "precio":precio,"prev":prev,"pct":pct,"es_pm":es_pm,
                "sma8":sma(c,8),"sma20":sma(c,20),"sma50":sma(c,50),"sma200":sma(c,200),
                "ema8":float(ema(c,8).iloc[-1]),
                "rsi":calc_rsi(c),"macd":mv,"macd_s":sv,"macd_h":hv_m,"macd_e":me,
                "adx":av,"dip":dip,"dim":dim,"adx_e":ae,"atr":atr_val,
                "vh":vh,"vh_proy":vh_proy,"vp":vp,"vol_r":vol_r,"ath_52w":ath_52w,
                "vela_patron":vela_patron,"vela_fuerza":vela_fuerza,"error":None}
    except Exception as ex:
        return {"ticker":ticker,"error":str(ex)}

def analizar(d):
    p=d["precio"]; s8,s20,s50,s200=d["sma8"],d["sma20"],d["sma50"],d["sma200"]
    c1=bool(p>s8) if s8 else False; c2=bool(s8>s20) if s20 else False
    c3=bool(s20>s50) if s50 else False; c4=bool(s50>s200) if s200 else False
    fan=sum([c1,c2,c3,c4]); en_rango=CONFIG["price_min"]<=p<=CONFIG["price_max"]
    vol_r=d["vol_r"]; vol_ok=d["vh"]>=CONFIG["min_volume_abs"]
    tardia=bool(s8 and p>s8*1.02)
    rsi_v=d["rsi"] if d["rsi"] else 0; adx_v=d["adx"] if d["adx"] else 0
    pts_fan=fan*10
    pts_rsi=(15 if 55<=rsi_v<=65 else 10 if 50<=rsi_v<=70 else 5 if 45<=rsi_v<=72 else 0)
    pts_adx=(15 if adx_v>=30 else 12 if adx_v>=25 else 8 if adx_v>=20 else 0)
    pts_vol=(15 if vol_r>=2.0 else 12 if vol_r>=1.5 else 8 if vol_r>=1.0 else 4 if vol_r>=0.7 else 0)
    pts_vela=int(d.get("vela_fuerza",0)*0.15)
    score=min(100,pts_fan+pts_rsi+pts_adx+pts_vol+pts_vela)
    return {"fan":fan,"c1":c1,"c2":c2,"c3":c3,"c4":c4,
            "en_rango":en_rango,"vol_r":vol_r,"vol_ok":vol_ok,"senal_tardia":tardia,
            "score":score,"score_det":f"Fan:{pts_fan}+RSI:{pts_rsi}+ADX:{pts_adx}+Vol:{pts_vol}+Vela:{pts_vela}"}

def posicion(precio,ath_52w=None):
    sp=CONFIG["stop_loss_pct"]/100; stop=round(precio*(1-sp),2); rx=precio-stop
    acc=max(1,int(CONFIG["riesgo_fijo_usd"]/rx)); tot=round(acc*precio,2); perd=round(acc*rx,2)
    t1=round(precio+1*rx,2); t2=round(precio+2*rx,2); t3=round(precio+3*rx,2)
    if ath_52w and ath_52w>precio:
        techo=round(ath_52w*0.98,2)
        if techo>t1:
            if t2>techo: t2=techo
            if t3>techo: t3=techo
    rr=round((t1-precio)/rx,2)
    return {"acc":acc,"tot":tot,"stop":stop,"perd":perd,"t1":t1,"t2":t2,"t3":t3,"rx":rx,"rr":rr}

def calc_probabilidades(fan,adx,rsi,vol_r):
    base={4:(68,42,25),3:(52,32,18),2:(38,20,10)}
    p1,p2,p3=base.get(fan,(38,20,10))
    if adx and adx>=25: p1+=10;p2+=8;p3+=5
    elif adx and adx<20: p1-=10;p2-=8;p3-=5
    if rsi:
        if rsi>70: p1-=10;p2-=8;p3-=5
        elif rsi<50: p1-=6;p2-=5;p3-=3
    if vol_r<0.5: p1-=10;p2-=8;p3-=5
    elif vol_r>=1.5: p1+=5;p2+=4;p3+=3
    return max(5,min(82,p1)),max(5,min(65,p2)),max(5,min(50,p3))

SECTOR_ETF={"energy":"XLE","technology":"QQQ","healthcare":"XLV","financial":"XLF",
            "basic materials":"XLB","basic mate":"XLB","consumer cyclical":"XLY",
            "consumer d":"XLY","consumer defensive":"XLP","industrials":"XLI",
            "real estate":"XLRE","utilities":"XLU","communication":"XLC"}
def get_sector_etf(sector):
    s=sector.lower()
    for k,v in SECTOR_ETF.items():
        if k in s: return v
    return "SPY"

def get_vix():
    try:
        fi=yf.Ticker("^VIX").fast_info; v=float(getattr(fi,"last_price",None) or 0)
        if v>0:
            nivel="ALTO/MIEDO" if v>25 else "MODERADO" if v>18 else "BAJO/CALMA"
            return round(v,1),nivel
    except: pass
    return None,"N/D"

def analizar_ia(d,a,pos,sentiment_score,tendencia_semanal):
    if not CLAUDE_API_KEY:
        return {"prob":0,"senal":"SIN IA","contexto":"","razon":"Sin API key","alerta":""}
    try:
        client=anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v=d["rsi"] if d["rsi"] else 0
        atr_v=d["atr"] if d["atr"] else 0
        atr_pct=round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
        sector=d.get("sector","N/A"); etf_ref=get_sector_etf(sector)
        vix_v,vix_nivel=get_vix()
        vix_str=f"{vix_v} ({vix_nivel})" if vix_v else "N/D"
        prompt=(
            f"Analiza {d['ticker']} ({d.get('nombre','')}) — swing 5-10 días.\n\n"
            f"TÉCNICOS: Precio {d['precio']:.2f} ({d['pct']:+.2f}%) | Fan {a['fan']}/4\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | ADX: {d['adx_e']} ({d['adx']:.0f})\n"
            f"ATR: {atr_pct}% | Vol: {a['vol_r']:.1f}x | Score: {a['score']}/100\n"
            f"Vela: {d.get('vela_patron','N/D')} (fuerza {d.get('vela_fuerza',0)}%)\n"
            f"Tendencia semanal: {tendencia_semanal}\n"
            f"VIX: {vix_str} | ETF sector: {etf_ref} | Sentiment: {sentiment_score}%\n"
            f"Stop: {pos['stop']:.2f} | T1: {pos['t1']:.2f} | R/R: {pos['rr']:.1f}x\n\n"
            f"Usa web_search para noticias de HOY sobre {d['ticker']} y {sector}.\n"
            f"Responde en español con tildes. Formato EXACTO:\n"
            f"PROBABILIDAD: [0-100]\nSIGNAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"CONTEXTO: [1 frase: VIX + {etf_ref} + catalizador actual]\n"
            f"RAZON: [1 frase sobre setup técnico y vela]\nALERTA: [nivel o evento clave]"
        )
        # Pausa antes de llamar a la API — evita rate limit 429
        # cuando el bot procesa varios tickers seguidos
        time.sleep(4)
        msg=client.messages.create(
            model="claude-sonnet-4-20250514",max_tokens=350,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            system=("Eres el analizador del Sistema Sirio de Solares Trading. "
                    "SIEMPRE usa web_search para noticias actuales. "
                    "Responde en español con tildes. Formato exacto sin texto extra."),
            messages=[{"role":"user","content":prompt}]
        )
        txt=""
        for block in msg.content:
            if hasattr(block,"text"): txt+=block.text+"\n"
        pr,se,ctx,ra,al=0,"ESPERAR","","",""
        for ln in txt.splitlines():
            ln=ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr=int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SIGNAL:"): se=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("CONTEXTO:"): ctx=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:"): ra=ln.split(":",1)[1].strip()
            elif ln.startswith("ALERTA:"): al=ln.split(":",1)[1].strip()
        return {"prob":pr,"senal":se,"contexto":ctx,"razon":ra,"alerta":al}
    except Exception as ex:
        return {"prob":0,"senal":"ERROR","contexto":"","razon":str(ex)[:80],"alerta":""}

def send_telegram(msg):
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r=requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=30)
        data=r.json()
        if r.status_code==200 and data.get("ok"):
            print(f"  [TG] OK id:{data['result']['message_id']}")
            return True
        print(f"  [TG] Reintentando ({data.get('description','')})")
        r2=requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=30)
        if r2.status_code==200 and r2.json().get("ok"):
            print("  [TG] OK sin formato"); return True
        print(f"  [TG] Error: {r2.json().get('description','')}"); return False
    except Exception as ex:
        print(f"  [TG] Excepción: {ex}"); return False

def build_msg(d,a,pos,ia,sent_texto,tendencia_semanal):
    hora=hora_et(); pm_tag=" [PRE-MARKET]" if d.get("es_pm") else ""
    rsi_v=d["rsi"] if d["rsi"] else 0; atr_v=d["atr"] if d["atr"] else 0
    atr_pct=round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
    rsi_tag=("débil" if rsi_v<CONFIG["rsi_min"] else "sobrecomprado" if rsi_v>CONFIG["rsi_max"] else "OK")
    vol_r=a["vol_r"]
    vol_tag=(f"🔥 ALTO — {vol_r:.1f}x" if vol_r>=1.5
             else f"✅ normal — {vol_r:.1f}x" if vol_r>=0.8 else f"⚠️ BAJO — {vol_r:.1f}x")
    tardia_v="\n⚠️ precio extendido &gt;2% sobre SMA8" if a.get("senal_tardia") else ""
    p1,p2,p3=calc_probabilidades(a["fan"],d["adx"],rsi_v,vol_r)
    acc=pos["acc"]
    s25=max(1,round(acc*0.25)); s30=max(1,round(acc*0.30))
    s20x=max(1,round(acc*0.20)); s25b=max(0,acc-s25-s30-s20x)
    ath=d.get("ath_52w",0)
    dist_ath=(f"ATH 52s: {ath:.2f}  (-{((ath-d['precio'])/ath*100):.1f}%)"
              if ath>d["precio"] else f"ATH 52s: {ath:.2f}  (zona ATH)")
    etf_ref=get_sector_etf(d.get("sector","N/A"))
    macd_ico="🟢" if "Bull" in d["macd_e"] else "🔴"
    ia_ico="🚀" if ia["prob"]>=70 else "⚡" if ia["prob"]>=55 else "⏸"
    score=a.get("score",0); barra="█"*int(score/10)+"░"*(10-int(score/10))
    def esc(t): return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    ctx=esc(ia.get("contexto","N/D")); razon=esc(ia.get("razon","")); alerta=esc(ia.get("alerta",""))
    return (
        f"🌟 <b>SISTEMA SIRIO — Solares</b>\n"
        f"🕐 {hora}{pm_tag}\n"
        f"📈 Swing DIARIO 5-10 días\n\n"
        f"<b>{d['ticker']}</b>  {d.get('nombre','')}\n"
        f"🏭 Sector: {d.get('sector','N/A')}  |  Ref: <b>{etf_ref}</b>\n"
        f"💲 {d['precio']:.2f} USD  ({d['pct']:+.1f}%){tardia_v}\n"
        f"🏔 {dist_ath}\n\n"
        f"<b>📊 Abanico SMA OK 4/4</b>\n"
        f"{'✅' if a['c1'] else '❌'} Precio &gt; SMA8    {d['sma8']:.2f}\n"
        f"{'✅' if a['c2'] else '❌'} SMA8   &gt; SMA20   {d['sma20']:.2f}\n"
        f"{'✅' if a['c3'] else '❌'} SMA20  &gt; SMA50   {d['sma50']:.2f}\n"
        f"{'✅' if a['c4'] else '❌'} SMA50  &gt; SMA200  {d['sma200']:.2f}\n\n"
        f"<b>🔬 Indicadores</b>\n"
        f"MACD: {macd_ico} {d['macd_e']}\n"
        f"RSI:  {rsi_v:.0f}  ({rsi_tag})\n"
        f"ADX:  {d['adx_e']} ({d['adx']:.0f})\n"
        f"ATR:  {atr_pct}% diario esperado\n"
        f"Vol:  {vol_tag}\n"
        f"<i>(vs promedio últimos 20 días)</i>\n\n"
        f"<b>{d.get('vela_patron','Sin patrón')}</b>\n"
        f"<i>Fuerza de la vela: {d.get('vela_fuerza',0)}%</i>\n\n"
        f"<b>📈 Tendencia semanal</b>\n{tendencia_semanal}\n\n"
        f"<b>📰 Sentiment multi-canal</b>\n{sent_texto}\n\n"
        f"<b>🎯 Score Sistema Sirio: {score}/100</b>\n"
        f"<code>{barra}</code>\n"
        f"<i>{a.get('score_det','')}</i>\n\n"
        f"<b>💰 Posición</b>\n"
        f"Entrada: {d['precio']:.2f} USD  |  <b>{pos['acc']} acc</b>  |  Capital: {pos['tot']:.0f} USD\n"
        f"🛑 Stop:    {pos['stop']:.2f}  (-6% / -1R)\n"
        f"🎯 T1:      {pos['t1']:.2f}  (+{((pos['t1']/d['precio'])-1)*100:.1f}%)\n"
        f"🎯 T2:      {pos['t2']:.2f}  (+{((pos['t2']/d['precio'])-1)*100:.1f}%)\n"
        f"🎯 T3:      {pos['t3']:.2f}  (+{((pos['t3']/d['precio'])-1)*100:.1f}%)\n"
        f"🚀 Runner:  trail EMA8\n"
        f"R/R: {pos['rr']:.1f}x  |  Riesgo: {pos['perd']:.0f} USD\n\n"
        f"<b>🗓 Gestión de Salida — Sistema Sirio</b>\n"
        f"25%({s25}) T1  |  30%({s30}) T2  |  20%({s20x}) T3  |  25%({s25b}) trail\n"
        f"⏱ Time-stop: 7 días sin T1 → salida total\n\n"
        f"<b>📊 Probabilidades</b>\n"
        f"T1: {p1}%  |  T2: {p2}%  |  T3: {p3}%\n\n"
        f"<b>{ia_ico} IA {ia['prob']}% — {ia['senal']}</b>\n"
        f"🌍 {ctx}\n📐 {razon}\n👁 Vigilar: {alerta}\n\n"
        f"<i>Sistema Sirio v6 — Solares</i> 🌟\n"
        f"─────────────────────────────────\n"
        f"⚠️ <i>AVISO LEGAL: Esta información tiene carácter "
        f"exclusivamente educativo e informativo. No constituye "
        f"asesoramiento financiero ni recomendación de inversión. "
        f"Los mercados implican riesgo de pérdida de capital. "
        f"Cada persona es responsable de sus propias decisiones. "
        f"Solares no gestiona fondos de terceros.</i>"
    )

def main():
    print(f"\n{'='*55}")
    print(f"  SISTEMA SIRIO — Solares v6   {hora_et()}")
    print(f"{'='*55}\n")
    estado=cargar_estado(); ya=estado.get("alertados",[]); count=estado.get("count",0)
    if count>=CONFIG["max_alertas_dia"]:
        print(f"Máximo {CONFIG['max_alertas_dia']} alertas hoy."); return
    tickers=obtener_universo(); pendientes=[t for t in tickers if t not in ya]
    print(f"\nA revisar: {len(pendientes)}  (alertados hoy: {len(ya)})\n")
    nuevas=0; razones={}

    for ticker in pendientes:
        if count+nuevas>=CONFIG["max_alertas_dia"]: break
        print(f"  {ticker}...",end=" ",flush=True)
        d=obtener_datos(ticker)
        if d.get("error"):
            print(f"skip ({d['error'][:35]})"); razones["error"]=razones.get("error",0)+1; continue
        rsi_s=f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        print(f"{d['precio']:.2f} RSI:{rsi_s} MACD:{d['macd_e']} [{d.get('sector','?')[:10]}]")
        a=analizar(d)
        if a["fan"]<CONFIG["min_fan_to_alert"] or not a["en_rango"] or not a["vol_ok"]:
            razones["fan"]=razones.get("fan",0)+1; continue
        if a.get("senal_tardia") and a["fan"]<4:
            print(f"    skip: tardío >2% SMA8"); razones["tardia"]=razones.get("tardia",0)+1; continue
        if a["score"]<CONFIG["score_minimo"]:
            print(f"    skip: score {a['score']}/100"); razones["score"]=razones.get("score",0)+1; continue
        hay_earn,dias_earn=check_earnings_proximos(ticker)
        if hay_earn:
            print(f"    skip: earnings en {dias_earn}d"); razones["earnings"]=razones.get("earnings",0)+1; continue
        semanal_ok,tend_sem=check_tendencia_semanal(ticker)
        if not semanal_ok:
            print(f"    skip: {tend_sem}"); razones["semanal"]=razones.get("semanal",0)+1; continue
        print(f"  ⭐ FAN 4/4 | Score {a['score']}/100 | {d.get('vela_patron','?')[:40]}")
        sc_sent,txt_sent,datos_sent=get_sentiment_completo(ticker)
        pos=posicion(d["precio"],d.get("ath_52w"))
        ia=analizar_ia(d,a,pos,sc_sent,tend_sem)
        print(f"     IA: {ia['prob']}% — {ia['senal']}")
        msg=build_msg(d,a,pos,ia,txt_sent,tend_sem)
        ok=send_telegram(msg)
        if ok:
            nuevas+=1; ya.append(ticker)
            estado["alertados"]=ya; estado["count"]=count+nuevas; guardar_estado(estado)
            registrar_backtest(ticker,d["precio"],pos["stop"],pos["t1"],pos["t2"],pos["t3"],
                               a["score"],ia["senal"],d.get("vela_patron",""))
            guardar_en_diario(ticker,d["precio"],a["fan"],datos_sent,ia["senal"],d.get("vela_patron",""))
            print(f"  ✅ Enviado ({count+nuevas}/{CONFIG['max_alertas_dia']} hoy)")
        else:
            print(f"  ❌ Error Telegram")

    print(f"\nFin: {nuevas} alertas | Total hoy: {count+nuevas} | Skips: {razones}")

    # SIEMPRE enviar mensaje cuando no hay señales nuevas
    if nuevas==0 and count<CONFIG["max_alertas_dia"]:
        mapa={"fan":"Fan SMA incompleto","score":"Score bajo mínimo",
              "earnings":"Earnings próximos","semanal":"Semanal bajista",
              "tardia":"Señal tardía","error":"Error de datos"}
        skips_txt="\n".join(f"  · {mapa.get(k,k)}: {v}" for k,v in razones.items()) if razones else ""
        send_telegram(
            f"🔭 <b>SISTEMA SIRIO — Solares</b>\n"
            f"🕐 {hora_et()}\n\n"
            f"⚙️ Universo escaneado: <b>{len(pendientes)} tickers</b>\n"
            f"📭 <b>Sin coincidencias</b> en esta pasada\n"
            f"✅ Alertas hoy: {count}/{CONFIG['max_alertas_dia']}\n"
            +(f"\n<b>Motivos de filtrado:</b>\n{skips_txt}\n" if skips_txt else "")
            +f"\n<i>Esperar es la posición. Estás protegida.</i>\n\n"
            f"<i>Sistema Sirio v6 — Solares</i> 🌟"
        )

if __name__=="__main__":
    main()
