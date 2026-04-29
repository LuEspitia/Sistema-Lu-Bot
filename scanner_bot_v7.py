# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
#  Solares — Sistema Sirio Bot  v8.0
#  Trader : Lu Espitia  |  Solares Trading
#  Cambios v8.0: Filtros simplificados — 5 obligatorios (precio>SMA200,
#                SMA20 pendiente alcista, vol>500K, ATR>$1, Beta>1)
#                Stop = low de vela confirmación (no ATR fijo)
#                Salidas: 15%@1.5R | 50%@2.5R | 35% trailing SMA20
#                Stop mgmt: BE tras T1 | +0.3R/+0.5R tras T2
#                Zona entrada: pullback SMA20 (max +5%, min -2%)
#                Filtro zombie (3 scans sin vela → silencio 5d)
#                ETFs commodities excluidos del scan
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

# ── TIMEZONE — DST automático (EDT/EST) via zoneinfo ─────────────────────
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = timezone(timedelta(hours=-4))  # fallback Python <3.9

def hora_et():
    return datetime.now(ET).strftime("%d/%m/%Y  %H:%M ET")

def es_dia_festivo_nyse():
    """Retorna (bool, nombre) si NYSE está cerrado hoy. Lista 2025-2027."""
    from datetime import date as _d
    hoy = _d.today()
    festivos = {
        _d(2025,1,1):"New Year's Day",_d(2025,1,20):"MLK Day",
        _d(2025,2,17):"Presidents Day",_d(2025,4,18):"Good Friday",
        _d(2025,5,26):"Memorial Day",_d(2025,6,19):"Juneteenth",
        _d(2025,7,4):"Independence Day",_d(2025,9,1):"Labor Day",
        _d(2025,11,27):"Thanksgiving",_d(2025,12,25):"Christmas",
        _d(2026,1,1):"New Year's Day",_d(2026,1,19):"MLK Day",
        _d(2026,2,16):"Presidents Day",_d(2026,4,3):"Good Friday",
        _d(2026,5,25):"Memorial Day",_d(2026,6,19):"Juneteenth",
        _d(2026,7,3):"Independence Day",_d(2026,9,7):"Labor Day",
        _d(2026,11,26):"Thanksgiving",_d(2026,12,25):"Christmas",
        _d(2027,1,1):"New Year's Day",_d(2027,1,18):"MLK Day",
        _d(2027,2,15):"Presidents Day",_d(2027,3,26):"Good Friday",
        _d(2027,5,31):"Memorial Day",_d(2027,6,18):"Juneteenth",
        _d(2027,7,5):"Independence Day",_d(2027,9,6):"Labor Day",
        _d(2027,11,25):"Thanksgiving",_d(2027,12,24):"Christmas",
    }
    nombre = festivos.get(hoy)
    return (nombre is not None, nombre or "")

CONFIG = {
    "capital_usd":         20275,   # v7.3 — solo cuenta day/swing
    "riesgo_fijo_usd":     100,     # v7.3 — corregido desde 150
    "stop_loss_pct":       6.0,
    "price_min":           10.0,
    "price_max":           150.0,
    "rsi_min":             35,      # v8 — más flexible, filtro de vela hace el trabajo
    "rsi_max":             65,      # v8 — ampliado, la zona de entrada es SMA20 pullback
    "min_volume_abs":      500_000,
    "min_fan_to_alert":    2,       # mantener fan 2/3 mínimo
    "score_minimo":        58,      # v8 — reducido; filtros concretos reemplazan score alto
    "earnings_dias_min":   5,
    "max_tickers_scan":    500,
    "max_alertas_dia":     99,
    "atr_min_usd":         1.0,     # v8 — obligatorio: ATR diario ≥ $1
    "beta_min":            1.0,     # v8 — obligatorio: Beta ≥ 1.0
    # v8 — Zona de pullback SMA20 (reemplaza zona SMA8)
    "pullback_sma20_max_pct": 5.0,  # precio max +5% sobre SMA20
    "pullback_sma20_min_pct": -2.0, # precio puede estar hasta -2% bajo SMA20 (tocando)
    "sma20_slope_dias":    5,        # días para verificar pendiente SMA20
    # v8 — Filtro señal zombie (ticker sin vela señal real)
    "zombie_max_scans":    3,        # 3 scans sin vela confirmación → silenciar
    "zombie_silencio_dias": 5,       # días de silencio después de zombie
    "zombie_file":         "/tmp/sirio_zombie_tracker.json",
    # v8 — ETFs de commodities/volatilidad excluidos del swing scanner
    "etf_commodities_excluidos": [
        "COPX","XME","GDX","GDXJ","SIL","URA","REMX",
        "XLE","OIH","XOP","AMLP","ICLN","TAN","FAN",
        "VXX","UVXY","SVXY","SOXS","SOXL","TQQQ","SQQQ"
    ],
    # Resumen de vela 4H — 15 min antes del cierre de cada vela
    # Vela 1: 9:30am-1:30pm ET → resumen 1:15pm ET
    # Vela 2: 1:30pm-4:00pm ET → resumen 3:45pm ET
    "resumen_4h_horas":    [13, 15],   # hora UTC-4 para enviar resumen
    "resumen_4h_mins":     [15, 45],   # minuto para cada resumen
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
            return {"fecha":str(date.today()),"alertados":[],"largo_enviado":[],"heartbeat_enviado":False,"primera_alerta_hora":{},
                    "actualizaciones_hoy":{},"sin_coincidencias_enviado":False,"count":0}
        # Compatibilidad con versiones anteriores
        if "largo_enviado" not in e:
            e["largo_enviado"] = list(e.get("alertados",[]))
        if "actualizaciones_hoy" not in e:
            e["actualizaciones_hoy"] = {}
        if "sin_coincidencias_enviado" not in e:
            e["sin_coincidencias_enviado"] = False
        if "heartbeat_enviado" not in e:
            e["heartbeat_enviado"] = False
        if "primera_alerta_hora" not in e:
            e["primera_alerta_hora"] = {}
        if "ts_ultimo_sin_senal" not in e:
            e["ts_ultimo_sin_senal"] = 0
        if "ts_ultimo_mensaje" not in e:
            e["ts_ultimo_mensaje"] = 0
        if "festivo_enviado" not in e:
            e["festivo_enviado"] = False
        if "mensajes_hoy" not in e:
            e["mensajes_hoy"] = 0
        return e
    except:
        return {"fecha":str(date.today()),"alertados":[],"largo_enviado":[],"heartbeat_enviado":False,
                "primera_alerta_hora":{},"ts_ultimo_sin_senal":0,"ts_ultimo_mensaje":0,
                "festivo_enviado":False,"actualizaciones_hoy":{},"sin_coincidencias_enviado":False,"mensajes_hoy":0,"count":0}

def guardar_estado(e):
    try:
        with open(CONFIG["state_file"],"w",encoding="utf-8") as f:
            json.dump(e,f)
    except:
        pass

# ── Zombie tracker — persiste entre días ─────────────────────────────────────
def cargar_zombie():
    """Estado zombie cross-day: contadores y períodos de silencio por ticker."""
    try:
        with open(CONFIG["zombie_file"],"r",encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"contadores":{}, "silenciados":{}}

def guardar_zombie(z):
    try:
        with open(CONFIG["zombie_file"],"w",encoding="utf-8") as f:
            json.dump(z,f)
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
    Obtiene sentiment de las fuentes disponibles.
    Si no hay datos de redes: muestra solo noticias de Yahoo.
    Si tampoco hay Yahoo: sección de sentiment NO aparece en el mensaje.
    """
    print(f"    Sentiment {ticker}...", end=" ", flush=True)
    rd = _reddit(ticker)
    st = _stocktwits(ticker)
    yh = _yahoo_news(ticker)

    scores=[]; pesos=[]; secciones=[]

    if rd:
        scores.append(rd["score"]); pesos.append(35)
        tops = "\n".join(f"  · {t}" for t in rd["top"]) if rd["top"] else ""
        secciones.append(f"📱 <b>Reddit</b>: {rd['label']} ({rd['score']}%) — {rd['detalle']}\n{tops}")

    if st:
        scores.append(st["score"]); pesos.append(40)
        secciones.append(f"💬 <b>StockTwits</b>: {st['label']} ({st['score']}%) — {st['detalle']}")

    if yh:
        scores.append(yh["score"]); pesos.append(25 if (rd or st) else 100)
        nots = "\n".join(f"  · {t[:80]}" for t in yh["titulares"])
        secciones.append(f"📰 <b>Yahoo Finance</b>: {yh['label']} ({yh['score']}%)\n{nots}")

    fuentes = sum(1 for x in [rd, st, yh] if x)

    if not scores:
        # Sin ninguna fuente — retorna None para que el mensaje omita la sección
        print("sin datos")
        datos = {"score_final": None, "label": None,
                 "reddit": None, "stocktwits": None, "yahoo": None, "fuentes": 0}
        return None, None, datos  # texto=None → sección omitida en build_msg

    peso_total  = sum(pesos)
    sc_final    = int(sum(s*p for s,p in zip(scores,pesos)) / peso_total)
    etiq = ("🟢 BULLISH" if sc_final>=65 else "🟡 Lev.Bull" if sc_final>=55
            else "⚪ Neutral" if sc_final>=45 else "🟠 Lev.Bear" if sc_final>=35 else "🔴 BEARISH")

    print(f"{sc_final}% {etiq} ({fuentes} fuentes)")
    texto = (f"<b>Score Solares: {sc_final}% bullish — {etiq}</b>\n"
             f"<i>({fuentes} fuentes activas)</i>\n\n" + "\n\n".join(secciones))

    datos = {"score_final": sc_final, "label": etiq,
             "reddit": rd, "stocktwits": st, "yahoo": yh, "fuentes": fuentes}
    return sc_final, texto, datos

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

# ─────────────────────────────────────────────────────────────
#  UNIVERSO DE TICKERS — Multi-fuente con fallback garantizado
#  Prioridad: TradingView Screener → Finviz CSV → Lista curada
#  La lista curada (~400 tickers) siempre funciona desde GitHub Actions
# ─────────────────────────────────────────────────────────────

# Lista curada por sectores — 400+ tickers relevantes para el sistema
# Ordenados por prioridad: los primeros son los que el sistema detecta mejor
UNIVERSO_CURADO = [
    # ── ENERGÍA (XLE) — sector principal con Brent alto ──────
    "OXY","CVX","XOM","COP","DVN","HAL","SLB","BKR","MRO","FANG",
    "EOG","PXD","APA","HES","CTRA","OVV","SM","MTDR","VTLE","CHRD",
    "BTU","ARCH","AMR","ARLP","NRP","SXC","CEIX","HCC",
    "RIG","VAL","NOV","WHD","PTEN","WTTR","NINE","NR",
    "PSX","VLO","MPC","PBF","DKL","PARR","CAPL",
    # ── MATERIALES Y MINERÍA (XLB / GDX) ─────────────────────
    "NEM","AEM","GOLD","WPM","KGC","AGI","EQX","IAG","BTG","OR",
    "PAAS","AG","MAG","HL","CDE","SILV","SVM","SSRM","AUMN",
    "FCX","SCCO","HBM","TECK","CS","ACH","AA","CENX","CSTM",
    "MP","NOVN","ARNC","ATI","TIE","PLEXY",
    "CLF","STLD","NUE","RS","CMC","ZEUS","MTUS",
    "MOS","CF","NTR","IPI","UAMY","CATO",
    "ALB","LTHM","SQM","LAC","PLL","LIVENT",
    # ── INDUSTRIALES (XLI) ───────────────────────────────────
    "CAT","DE","EMR","ITW","PH","ROK","XYL","GNRC","RBC",
    "GE","HON","MMM","LMT","RTX","NOC","GD","HII","TXT",
    "UPS","FDX","XPO","SAIA","ODFL","JBHT","CHRW","EXPD",
    "URI","HEES","WSC","GATX","AER","AL",
    # ── SALUD (XLV) ──────────────────────────────────────────
    "ABBV","MRK","PFE","JNJ","BMY","AMGN","GILD","BIIB",
    "CVS","UNH","CI","HUM","CNC","MOH","ELV",
    "HIMS","DOCS","ACCD","PHR","ONEM","LFST",
    "EW","BDX","BAX","BSX","ZBH","SYK","ISRG","MDT",
    # ── FINANCIEROS SELECTIVOS (XLF) ─────────────────────────
    "JPM","BAC","WFC","GS","MS","C","USB","PNC","TFC","SCHW",
    "BX","KKR","APO","ARES","CG","OWL","BLUE",
    "AXP","COF","DFS","SYF","OMF","QCRH",
    # ── TECNOLOGÍA MODERADA (QQQ) — solo en rango $10-150 ────
    "AMD","INTC","MU","ON","WOLF","AMAT","LRCX","KLAC","ENTG",
    "CSCO","HPQ","HPE","DELL","NTAP","PSTG","PURE",
    "TWLO","ZS","CRWD","S","TENB","RPD","QLYS",
    "TTD","APPS","DV","IAS","MGNI","PUBM",
    # ── CONSUMO CÍCLICO (XLY) ────────────────────────────────
    "F","GM","STLA","LEA","BWA","APTV","VC","DAN",
    "MGM","WYNN","LVS","PENN","CZR","DKNG","RSI",
    "NKE","LEVI","PVH","HBI","G","VSCO",
    "DAL","UAL","AAL","SAVE","ALK","HA","JBLU",
    # ── CONSUMO DEFENSIVO (XLP) ──────────────────────────────
    "KO","PEP","MO","PM","BTI","LO","VGR",
    "KHC","CPB","CAG","SJM","MKC","THS","SMPL",
    "INGR","ADM","BG","CALM","SAFM",
    # ── ETFs SECTORIALES Y TEMÁTICOS ─────────────────────────
    "XLE","XLB","XLI","XLV","XLF","XLY","XLP","XLU","XLRE",
    "GDX","GDXJ","SIL","PICK","COPX","REMX","LIT","ARKK",
    "JEPI","SCHD","DVY","HDV","VYM","SDY",
    # ── CRYPTO / DIGITAL ASSETS (beta alto) ──────────────────
    "MARA","RIOT","CLSK","BTBT","CIFR","HUT","BTDR",
    "COIN","HOOD","MSTR","SMLR",
    # ── WATCHLIST PERSONAL LU ─────────────────────────────────
    "DAR","GDX","GOAU","HIMS","EW","IBIT","WPM",
]

def _tv_screener(max_tickers=300):
    """
    TradingView screener via tradingview_ta.
    Filtra: precio $10-150, volumen >500K, mercado USA.
    Funciona desde GitHub Actions sin bloqueos de IP.
    """
    try:
        from tradingview_ta import TA_Handler, Interval, Exchange
        from tradingview_ta import get_multiple_analysis
        # Screener de acciones USA con filtros básicos
        # Usamos la API de recomendación para obtener tickers activos
        import urllib.request
        url = ("https://scanner.tradingview.com/america/scan"
               "?markets=america&symbols=&filter="
               "[]&sort=volume,desc&range=0,200")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        tickers = []
        for item in data.get("data", []):
            s = item.get("s", "")
            if ":" in s:
                t = s.split(":")[1]
                # Limpiar: quitar $ al inicio, solo letras mayúsculas, max 5 chars
                t = t.lstrip("$").upper()
                if t and len(t) <= 5 and t.isalpha():
                    tickers.append(t)
        if tickers:
            print(f"  TradingView screener: {len(tickers)} tickers")
        return tickers[:max_tickers]
    except Exception as ex:
        print(f"  TradingView screener no disponible: {ex}")
        return []

def _finviz_csv():
    """
    Intento con Finviz export CSV (más estable que el HTML scraper).
    Funciona si Finviz Elite no bloquea la IP.
    """
    try:
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        url  = ("https://finviz.com/screener.ashx?v=152"
                "&f=sh_price_o10,sh_price_u150,sh_avgvol_o500"
                ",ta_sma200_pa,ta_sma50_pa&ft=4&o=-volume&r=1")
        r = requests.get(url, headers=hdrs, timeout=20)
        if r.status_code != 200:
            return []
        # Intentar extraer tickers del CSV export
        from html.parser import HTMLParser
        class FP(HTMLParser):
            def __init__(self): super().__init__(); self.t=[]; self.c=False
            def handle_starttag(self,tag,attrs):
                d=dict(attrs)
                if tag=="a" and d.get("class")=="screener-link-primary": self.c=True
            def handle_data(self,data):
                if self.c:
                    t=data.strip()
                    if t and t.replace("-","").isalpha() and len(t)<=5: self.t.append(t)
                    self.c=False
        fp=FP(); fp.feed(r.text)
        if fp.t:
            print(f"  Finviz: {len(fp.t)} tickers")
        return fp.t
    except:
        return []

def obtener_universo():
    """
    Construye el universo de tickers desde múltiples fuentes.
    La lista curada es el backbone — siempre disponible.
    TradingView y Finviz agregan tickers del mercado que puedan estar activos hoy.
    """
    print("Construyendo universo de tickers...")

    # Base siempre disponible
    universo = list(dict.fromkeys(UNIVERSO_CURADO))  # deduplicado, orden preservado

    # Fuente dinámica 1: TradingView screener
    tv = _tv_screener(200)
    añadidos_tv = 0
    for t in tv:
        if t not in universo:
            universo.append(t); añadidos_tv += 1

    # Fuente dinámica 2: Finviz (si no está bloqueado)
    fv = _finviz_csv()
    añadidos_fv = 0
    for t in fv:
        if t not in universo:
            universo.append(t); añadidos_fv += 1

    resultado = universo[:CONFIG["max_tickers_scan"]]
    print(f"Universo total: {len(resultado)} tickers "
          f"(curado: {len(UNIVERSO_CURADO)}, "
          f"TV: +{añadidos_tv}, Finviz: +{añadidos_fv})")
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

def sma20_pendiente_alcista(hist, periodos=5):
    """
    v8 — Verifica que SMA20 tiene pendiente ALCISTA.
    Compara SMA20 actual vs SMA20 hace N periodos.
    Es uno de los 5 filtros obligatorios del sistema.
    Retorna (bool, float: pendiente_pct)
    """
    try:
        closes = hist["Close"].values.astype(float)
        if len(closes) < 25:
            return True, 0.0   # datos insuficientes → no bloquear
        sma20_hoy = float(np.mean(closes[-20:]))
        sma20_ant = float(np.mean(closes[-20-periodos:-periodos]))
        if sma20_ant <= 0:
            return True, 0.0
        pendiente_pct = (sma20_hoy - sma20_ant) / sma20_ant * 100
        return (pendiente_pct >= 0), round(pendiente_pct, 3)
    except:
        return True, 0.0

def detectar_velas(hist):
    """
    Detección de velas — Sistema Sirio v7.3
    Formaciones Oliver Velez: RBI (Rally Base Impulse) y GIFT (Gap Impulse For Trade)
    + formaciones clásicas: Hammer, Engulfing, Morning Star, Pin Bar, Piercing Line
    IMPORTANTE: SIEMPRE usar vela CERRADA. El bot corre en horario de cierre.
    """
    try:
        if len(hist)<4: return "Sin datos suficientes",0,0
        o=hist["Open"].values; h=hist["High"].values
        c=hist["Close"].values; l=hist["Low"].values
        o1,h1,c1,l1 = o[-1],h[-1],c[-1],l[-1]   # vela más reciente CERRADA
        candle_low = float(l1)  # v8 — low de la vela de señal para stop loss
        o2,h2,c2,l2 = o[-2],h[-2],c[-2],l[-2]   # vela anterior
        o3,h3,c3,l3 = o[-3],h[-3],c[-3],l[-3]   # 2 velas atrás
        o4,h4,c4,l4 = o[-4],h[-4],c[-4],l[-4]   # 3 velas atrás

        cuerpo1   = abs(c1-o1)
        rango1    = h1-l1 if h1-l1>0 else 0.0001
        mecha_inf = min(o1,c1)-l1
        mecha_sup = h1-max(o1,c1)
        cuerpo2   = abs(c2-o2)
        cuerpo3   = abs(c3-o3)
        rango2    = h2-l2 if h2-l2>0 else 0.0001
        alcista1  = c1>o1; bajista1 = c1<o1
        alcista2  = c2>o2; bajista2 = c2<o2
        alcista3  = c3>o3

        # ── OLIVER VELEZ — RBI (Rally · Base · Impulse) ─────────────────────
        # Patrón de 3 velas: Rally (vela alcista fuerte) → Base (vela pequeña/doji
        # dentro del rango del rally) → Impulse (ruptura alcista del rally)
        # La Base es la zona de acumulación — el precio "descansa" antes de continuar.
        # En Oliver Velez: la Base puede ser 1-3 velas. Aquí usamos el caso de 1 Base.
        # Estructura: o4/c4 grande alcista → o3/c3 pequeña (≤40% del rally) → o1/c1 rompe
        rally_grande  = alcista3 and cuerpo3 >= rango2*0.5
        base_pequeña  = cuerpo2  <= cuerpo3*0.40  # Base ≤ 40% del rally previo
        impulso_arriba= alcista1 and c1>h3        # Rompe sobre el máximo del rally
        if rally_grande and base_pequeña and impulso_arriba:
            return "⚡ RBI (Rally·Base·Impulse) [Oliver Velez] — setup de máxima prioridad",95,candle_low

        # ── OLIVER VELEZ — GIFT (Gap + Impulse For Trade) ───────────────────
        # Gap alcista de apertura + vela alcista fuerte = señal de entrada en la dirección
        # del gap. La vela GIFT es la confirmación de que el gap no se cerró.
        # Condiciones: gap alcista (o1 > c2) + cierre fuerte (c1 > o1 * 1.01)
        gap_alcista   = o1 > c2   # abre sobre el cierre anterior
        cuerpo_fuerte = alcista1 and cuerpo1 >= rango1*0.5
        if gap_alcista and cuerpo_fuerte:
            return "🎁 GIFT (Gap Impulse For Trade) [Oliver Velez] — gap alcista confirmado",92,candle_low

        # ── CLÁSICAS — Hammer / Pin Bar alcista ─────────────────────────────
        # Mecha inferior larga (≥ 2× cuerpo), cuerpo pequeño arriba, poca mecha superior
        if (mecha_inf >= 2*cuerpo1 and mecha_sup <= cuerpo1*0.5
                and cuerpo1 >= rango1*0.15 and alcista1):
            return "🔨 Hammer (Martillo) — soporte fuerte con rechazo",90,candle_low

        # Pin Bar alcista: mecha inferior ≥ 60% del rango total
        if mecha_inf >= rango1*0.60 and alcista1:
            return "📌 Pin Bar alcista — rechazo de zona de venta, comprador mandando",88,candle_low

        # ── CLÁSICAS — Bullish Engulfing ─────────────────────────────────────
        # Vela alcista grande que envuelve completamente la vela roja anterior
        if (alcista1 and bajista2 and o1<=c2 and c1>=o2 and cuerpo1>cuerpo2):
            return "🟢 Bullish Engulfing — absorción total de vendedores",85,candle_low

        # ── CLÁSICAS — Morning Star (3 velas) ───────────────────────────────
        # Vela roja grande → doji/vela pequeña → vela verde que recupera >50% de la roja
        doji_medio   = abs(c3-o3)<abs(c4-o4)*0.4 if len(c)>=5 else cuerpo2<cuerpo3*0.4
        if (bajista2 and doji_medio and alcista1 and c1>(o2+c2)/2):
            return "⭐ Morning Star — reversión de 3 velas confirmada",85,candle_low

        # ── CLÁSICAS — Piercing Line ─────────────────────────────────────────
        # Vela verde que abre bajo el mínimo anterior y cierra sobre la mitad de la roja
        if (alcista1 and bajista2 and o1<l2 and c1>(o2+c2)/2 and c1<o2):
            return "📈 Piercing Line — rebote alcista, cierra sobre 50% de la roja",72,candle_low

        # ── DOJI EN SOPORTE (potencial señal) ───────────────────────────────
        if cuerpo1 <= rango1*0.10 and bajista2 and c2<c3:
            return "⚖️ Doji en soporte — esperar vela de confirmación mañana",50,candle_low

        # ── VELA VERDE FUERTE (confirmación de momentum) ─────────────────────
        if alcista1 and cuerpo1 >= rango1*0.60 and mecha_inf <= cuerpo1*0.30:
            return "💚 Vela verde fuerte — momentum alcista, entrada válida",68,candle_low

        return "Vela sin patrón específico — esperar formación",38,candle_low
    except Exception as ex:
        return f"Error calculando velas: {str(ex)[:30]}",0,0

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
            return True,f"✅ Semanal alcista (P > SMA8w {sma8w:.1f} > SMA20w {sma20w:.1f})"
        elif precio>sma20w:
            return True,f"⚠️ Semanal neutral-alcista (sobre SMA20w {sma20w:.1f})"
        elif precio>sma8w:
            # FIX v7.2: modo recuperacion — precio sobre SMA8w semanal aunque aun bajo SMA20w.
            # Tras 2+ semanas alcistas, acciones en recuperacion valida pasan este filtro.
            return True,f"🔄 Semanal recuperando (sobre SMA8w {sma8w:.1f})"
        else:
            return False,f"❌ Semanal bajista — bajo SMA8w {sma8w:.1f} y SMA20w {sma20w:.1f}"
    except:
        return True,"Error timeframe semanal (OK)"

def obtener_datos(ticker):
    try:
        s=yf.Ticker(ticker)
        hist=s.history(period="1y",interval="1d",prepost=True)
        if hist.empty or len(hist)<60: return {"ticker":ticker,"error":"Datos insuficientes"}
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
        # ATH 52 semanas: priorizar fast_info (dato real del exchange)
        # Si no disponible, caer a history. Esto evita que un nuevo máximo intraday
        # quede == precio_actual y anule el filtro de proximidad ATH.
        try:
            ath_fi = float(getattr(fi, "fifty_two_week_high", None) or 0)
        except:
            ath_fi = 0
        if ath_fi > 0:
            ath_52w = ath_fi
        else:
            ath_52w = float(h.iloc[-252:].max()) if len(h)>=252 else float(h.max())
        mv,sv,hv_m,me=calc_macd(c); av,dip,dim,ae=calc_adx(h,l,c)
        atr_val=calc_atr(h,l,c); vela_patron,vela_fuerza=detectar_velas(hist)
        nombre=ticker; sector="N/A"; beta=None
        try:
            info=s.info
            nombre=info.get("shortName",ticker)
            sector=info.get("sector","N/A")
            beta=info.get("beta", None)
        except: pass
        return {"ticker":ticker,"nombre":nombre,"sector":sector,
                "precio":precio,"prev":prev,"pct":pct,"es_pm":es_pm,
                "sma8":sma(c,8),"sma20":sma(c,20),"sma50":sma(c,50),"sma200":sma(c,200),
                "ema8":float(ema(c,8).iloc[-1]),
                "rsi":calc_rsi(c),"macd":mv,"macd_s":sv,"macd_h":hv_m,"macd_e":me,
                "adx":av,"dip":dip,"dim":dim,"adx_e":ae,"atr":atr_val,
                "beta":beta,
                "vh":vh,"vh_proy":vh_proy,"vp":vp,"vol_r":vol_r,"ath_52w":ath_52w,
                "vela_patron":vela_patron,"vela_fuerza":vela_fuerza,
                "vela_low":vela_low,              # v8 — low de la vela de señal
                "sma20_alcista":sma20_pendiente_alcista(hist, CONFIG["sma20_slope_dias"])[0],
                "sma20_pendiente":sma20_pendiente_alcista(hist, CONFIG["sma20_slope_dias"])[1],
                "error":None}
    except Exception as ex:
        return {"ticker":ticker,"error":str(ex)}

def clasificar_smas(s20, s50, s200, atr):
    """
    v7.3 — Clasificación de SMAs en lugar de exclusión.
    Las SMAs juntas NO se excluyen: son oportunidades tipo TRAMPOLÍN.
    Las SMAs separadas con espacio SON tendencia lucrativa.
    La información se pasa al mensaje de Telegram para que Lu decida.
    """
    if not all([s20, s50, s200, atr]) or atr <= 0:
        return "SIN_DATOS", "📊 SMA: sin datos suficientes"
    dist_20_50  = abs(s20 - s50)
    dist_50_200 = abs(s50 - s200)
    espacio_atr = dist_20_50 / atr  # cuántos ATR de distancia entre SMA20 y SMA50

    if espacio_atr >= 3.0 and s20 > s50 > s200:
        return "TENDENCIA", f"🚀 SMA TENDENCIA — espacio {espacio_atr:.1f}×ATR (> 3×) — runway amplio, momentum establecido"
    elif espacio_atr >= 1.5 and s20 > s50 > s200:
        return "ESPACIO_NORMAL", f"✅ SMA ESPACIO NORMAL — {espacio_atr:.1f}×ATR — setup estándar"
    elif espacio_atr < 1.5 and s20 > s50:
        return "TRAMPOLIN", f"🪜 SMA TRAMPOLÍN — {espacio_atr:.1f}×ATR (comprimidas) — posible catapulta si hay vela señal fuerte"
    elif espacio_atr < 1.5 and s20 <= s50:
        return "COMPRIMIDA_BAJISTA", f"⚠️ SMA COMPRIMIDAS BAJISTAS — {espacio_atr:.1f}×ATR — cautela, posible inicio de distribución"
    else:
        return "MIXTA", f"🔵 SMA MIXTA — {espacio_atr:.1f}×ATR — contexto no determinante"

def analizar(d):
    """
    v8.0 — Filtros simplificados (5 obligatorios):
    Precio>SMA200, SMA20 pendiente alcista, Vol>500K, ATR≥$1, Beta≥1.
    Zona entrada: pullback SMA20 (-2% a +5%).
    SMAs: clasificadas (Trampolín / Tendencia / Comprimidas).
    """
    p=d["precio"]; s8,s20,s50,s200=d["sma8"],d["sma20"],d["sma50"],d["sma200"]
    atr_v = d.get("atr") or 0

    # v8 — SMA20 pendiente alcista (filtro base)
    sma20_alcista = d.get("sma20_alcista", True)
    # v8 — Zona de pullback SMA20
    dist_sma20_pct = ((p - s20) / s20 * 100) if s20 and s20 > 0 else 0
    en_pullback_sma20 = (CONFIG["pullback_sma20_min_pct"] <= dist_sma20_pct <= CONFIG["pullback_sma20_max_pct"])

    # Fan — SMAs ordenadas: SMA200 es el macro, SMA50 el medio, SMA20 el momentum
    c1=bool(p>s200)    if s200             else False  # Paso 1 — veto absoluto
    c2=bool(s50>s200)  if s50 and s200     else False  # SMA50 > SMA200 — estructura
    c3=bool(s20>s50)   if s20 and s50      else False  # SMA20 > SMA50  — impulso
    c4=bool(p>s20)     if s20              else False  # Precio > SMA20 — momentum

    fan=sum([c1,c2,c3,c4]); en_rango=CONFIG["price_min"]<=p<=CONFIG["price_max"]
    vol_r=d["vol_r"]; vol_ok=d["vh"]>=CONFIG["min_volume_abs"]
    tardia=bool(s8 and p>s8*1.02)
    rsi_v=d["rsi"] if d["rsi"] else 0; adx_v=d["adx"] if d["adx"] else 0

    # ── Clasificación SMA — NO exclusión (v7.3) ──────────────────────────────
    sma_tipo, sma_label = clasificar_smas(s20, s50, s200, atr_v)

    # ── Scoring ──────────────────────────────────────────────────────────────
    pts_fan = fan * 10  # max 40

    # RSI v7.3: zona óptima 40-55. >58 = sobrecomprado para Sirio.
    if 45<=rsi_v<=55:     pts_rsi = 15  # zona perfecta — precio acaba de recuperar
    elif 40<=rsi_v<45:    pts_rsi = 10  # recuperando — válido, menos ideal
    elif 55<rsi_v<=58:    pts_rsi = 8   # válido pero en el límite superior
    elif rsi_v<40:        pts_rsi = 0   # demasiado débil aún
    else:                 pts_rsi = 0   # >58 — sobrecomprado para sistema Sirio

    # Bonus SMA: Trampolín puntúa diferente a Tendencia
    pts_sma_bonus = 0
    if sma_tipo == "TENDENCIA":          pts_sma_bonus = 10  # runway amplio
    elif sma_tipo == "ESPACIO_NORMAL":   pts_sma_bonus = 6
    elif sma_tipo == "TRAMPOLIN":        pts_sma_bonus = 8   # alta potencial si vela fuerte

    pts_adx=(15 if adx_v>=30 else 12 if adx_v>=25 else 8 if adx_v>=20 else 0)
    if vol_r>=2.0:        pts_vol = 15
    elif vol_r>=1.5:      pts_vol = 12
    elif vol_r>=1.0:      pts_vol = 8
    elif vol_r>=0.8:      pts_vol = 4
    else:                 pts_vol = 0
    pts_vela=int(d.get("vela_fuerza",0)*0.15)
    score=min(100, pts_fan+pts_rsi+pts_adx+pts_vol+pts_vela+pts_sma_bonus)
    return {"fan":fan,"c1":c1,"c2":c2,"c3":c3,"c4":c4,
            "en_rango":en_rango,"vol_r":vol_r,"vol_ok":vol_ok,"senal_tardia":tardia,
            "sma_tipo":sma_tipo,"sma_label":sma_label,
            "sma20_alcista":sma20_alcista,"en_pullback_sma20":en_pullback_sma20,
            "dist_sma20_pct":round(dist_sma20_pct,2),  # v8
            "score":score,
            "score_det":f"Fan:{pts_fan}+RSI:{pts_rsi}+SMA:{pts_sma_bonus}+ADX:{pts_adx}+Vol:{pts_vol}+Vela:{pts_vela}"}

def posicion(precio, ath_52w=None, atr=None, beta=None, vela_low=None):
    """
    v8.0 — Stop = low de la vela de confirmación (más preciso que ATR fijo).
    Si vela_low no disponible o inválido → ATR como respaldo → 6% fijo como último recurso.

    Sistema de salidas v8:
    ─────────────────────────────────────────────────────────────
    15%  en T1 (1.5R)  → cerrar acc_t1 acciones aquí
    50%  en T2 (2.5R)  → cerrar acc_t2 acciones aquí
    35%  trailing      → por swing lows recientes o cierre < SMA20 diario
    ─────────────────────────────────────────────────────────────
    Gestión de stop:
    · Al llegar a T1 → mover stop del 85% restante a Break Even (precio entrada)
    · Al llegar a T2 → opcional: mover stop del 35% a +0.3R ó +0.5R
    """
    # ── Calcular stop ────────────────────────────────────────────────────────
    metodo_stop = "pct_fijo"
    if vela_low and float(vela_low) > 0 and float(vela_low) < precio:
        # Ligeramente bajo el low de la vela de confirmación (buffer 0.3%)
        stop = round(float(vela_low) * 0.997, 2)
        metodo_stop = "candle_low"
    elif atr and atr > 0 and precio > 0:
        b = beta if beta and beta > 0 else 1.0
        mult = 2.0 if b >= 1.5 else (1.5 if b >= 0.8 else 1.2)
        riesgo_atr = max(precio*0.03, min(precio*0.08, atr * mult))
        stop = round(precio - riesgo_atr, 2)
        metodo_stop = "atr"
    else:
        stop = round(precio * (1 - CONFIG["stop_loss_pct"] / 100), 2)

    rx = precio - stop
    # Validación: stop máximo 8%, mínimo viable
    if rx <= 0 or rx / precio > 0.08:
        stop = round(precio * 0.92, 2)
        rx   = precio - stop
        metodo_stop += "_capped8pct"
    if rx <= 0: rx = precio * 0.06

    # ── Tamaño de posición ───────────────────────────────────────────────────
    acc  = max(1, int(CONFIG["riesgo_fijo_usd"] / rx))
    tot  = round(acc * precio, 2)
    perd = round(acc * rx, 2)

    # ── Targets v8 ───────────────────────────────────────────────────────────
    t1 = round(precio + 1.5 * rx, 2)   # T1 = 1.5R  → cerrar 15%
    t2 = round(precio + 2.5 * rx, 2)   # T2 = 2.5R  → cerrar 50%
    # T3 = trailing SMA20 diario (no nivel fijo)

    # Cap en ATH 52s
    if ath_52w and ath_52w > precio:
        techo = round(ath_52w * 0.98, 2)
        if t2 > techo: t2 = techo

    # ── Partes en acciones ───────────────────────────────────────────────────
    acc_t1 = max(1, round(acc * 0.15))           # 15%
    acc_t2 = max(1, round(acc * 0.50))           # 50%
    acc_t3 = max(1, acc - acc_t1 - acc_t2)       # 35% trailing

    # ── Niveles de gestión de stop ───────────────────────────────────────────
    be              = precio                          # break even
    stop_post_t2_lo = round(precio + 0.3 * rx, 2)   # +0.3R (opción conservadora)
    stop_post_t2_hi = round(precio + 0.5 * rx, 2)   # +0.5R (opción agresiva)

    return {
        "acc":acc, "tot":tot, "stop":stop, "perd":perd,
        "t1":t1, "t2":t2, "rx":rx,
        "rr_t1": round(1.5, 1),
        "rr_t2": round(2.5, 1),
        "acc_t1":acc_t1, "acc_t2":acc_t2, "acc_t3":acc_t3,
        "be":be,
        "stop_post_t2_lo":stop_post_t2_lo,
        "stop_post_t2_hi":stop_post_t2_hi,
        "metodo_stop":metodo_stop,
        # Compatibilidad: los backtest y msg_corto usan t3 → pasar t2 como fallback
        "t3": round(precio + 3.5 * rx, 2),
        "rr":  round(1.5, 1)
    }

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

def analizar_ia(d, a, pos, sentiment_score, tendencia_semanal, noticias_yahoo=None):
    """
    Análisis IA con web_search desactivado — Claude analiza con los datos
    que ya tiene (técnicos + noticias Yahoo que le pasamos en el prompt).
    Esto elimina el rate limit 429 que ocurría con el tool de web_search.
    """
    if not CLAUDE_API_KEY:
        return {"prob":0,"senal":"SIN IA","contexto":"","razon":"Sin API key","alerta":""}
    try:
        client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        rsi_v   = d["rsi"] if d["rsi"] else 0
        atr_v   = d["atr"] if d["atr"] else 0
        atr_pct = round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
        sector  = d.get("sector","N/A")
        etf_ref = get_sector_etf(sector)
        vix_v, vix_nivel = get_vix()
        vix_str = f"{vix_v} ({vix_nivel})" if vix_v else "N/D"
        sent_str = f"{sentiment_score}%" if sentiment_score else "sin datos"

        # Incluir noticias de Yahoo directamente en el prompt
        noticias_txt = ""
        if noticias_yahoo:
            noticias_txt = "\nNOTICIAS RECIENTES (Yahoo Finance):\n"
            noticias_txt += "\n".join(f"- {n}" for n in noticias_yahoo[:3])

        prompt = (
            f"Analiza {d['ticker']} ({d.get('nombre','')}) para swing trading 5-10 días.\n\n"
            f"TÉCNICOS DIARIOS:\n"
            f"Precio: {d['precio']:.2f} USD ({d['pct']:+.2f}%) | Fan SMA: {a['fan']}/4\n"
            f"MACD: {d['macd_e']} | RSI: {rsi_v:.0f} | ADX: {d['adx_e']} ({(d['adx'] or 0):.0f})\n"
            f"ATR: {atr_pct}% | Volumen: {a['vol_r']:.1f}x prom.20d\n"
            f"Score Sistema Sirio: {a['score']}/100\n"
            f"Vela: {d.get('vela_patron','sin patrón')} (fuerza {d.get('vela_fuerza',0)}%)\n"
            f"Señal tardía (>2% SMA8): {'SÍ' if a.get('senal_tardia') else 'NO'}\n\n"
            f"CONTEXTO MACRO:\n"
            f"VIX: {vix_str} | ETF sector referencia: {etf_ref}\n"
            f"Tendencia semanal: {tendencia_semanal}\n"
            f"Sentiment: {sent_str}\n"
            f"Stop: {pos['stop']:.2f} | T1: {pos['t1']:.2f} | R/R: {pos['rr']:.1f}x"
            f"{noticias_txt}\n\n"
            f"Responde ÚNICAMENTE en este formato exacto (en español con tildes):\n"
            f"PROBABILIDAD: [número 0-100]\n"
            f"SIGNAL: [ENTRAR / ESPERAR / NO APLICA]\n"
            f"CONTEXTO: [1 frase sobre VIX + {etf_ref} + catalizador del sector ahora]\n"
            f"RAZON: [1 frase sobre el setup técnico específico y la vela]\n"
            f"ALERTA: [precio exacto o evento específico a vigilar]"
        )

        # Sleep para no saturar la API entre señales consecutivas
        time.sleep(6)

        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            # Sin tools de web_search — evita rate limit y doble llamada interna
            system=(
                "Eres el analizador técnico del Sistema Sirio de Solares Trading. "
                "Conoces correlaciones entre sectores, VIX y macro. "
                "Responde SOLO en el formato exacto solicitado. "
                "En español con tildes y acentos correctos. Sin texto adicional."
            ),
            messages=[{"role":"user","content":prompt}]
        )

        txt = msg.content[0].text if msg.content else ""
        pr,se,ctx,ra,al = 0,"ESPERAR","","",""
        for ln in txt.splitlines():
            ln = ln.strip()
            if ln.startswith("PROBABILIDAD:"):
                try: pr=int(ln.split(":")[1].strip().replace("%",""))
                except: pass
            elif ln.upper().startswith("SIGNAL:"):   se=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("CONTEXTO:"): ctx=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("RAZON:"):    ra=ln.split(":",1)[1].strip()
            elif ln.upper().startswith("ALERTA:"):   al=ln.split(":",1)[1].strip()

        return {"prob":pr,"senal":se,"contexto":ctx,"razon":ra,"alerta":al}

    except Exception as ex:
        err = str(ex)[:60]
        print(f"    IA error: {err}")
        return {"prob":0,"senal":"ERROR","contexto":"","razon":err,"alerta":""}

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

def calcular_prioridad(a, ia, d):
    """
    🔴 ALTA    — Score 75+, ADX≥25, vol≥1.5x, IA=ENTRAR, no tardía, RSI≤68
    🟡 MEDIA   — Score 60-74, buenas condiciones, algún factor marginal
    🟢 INFO    — Score 55-59, señal válida pero esperar confirmación
    """
    score  = a.get("score", 0)
    adx    = d.get("adx", 0) or 0
    vol_r  = a.get("vol_r", 0)
    tardia = a.get("senal_tardia", False)
    ia_ok  = "ENTRAR" in ia.get("senal", "").upper()
    rsi_v  = d.get("rsi", 50) or 50
    if score>=75 and adx>=25 and vol_r>=1.5 and not tardia and ia_ok and rsi_v<=68:
        return "⭐", "ALTA PRIORIDAD — entra"
    elif score>=60 and adx>=20 and vol_r>=1.0:
        return "🔔", "MEDIA PRIORIDAD — revisa"
    else:
        return "🔭", "INFORMATIVA — observa"

def build_msg(d, a, pos, ia, sent_texto, tendencia_semanal):
    hora   = hora_et(); pm_tag=" [PRE-MARKET]" if d.get("es_pm") else ""
    rsi_v  = d["rsi"] if d["rsi"] else 0
    atr_v  = d["atr"] if d["atr"] else 0
    atr_pct= round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
    rsi_tag= ("débil" if rsi_v<CONFIG["rsi_min"]
              else "sobrecomprado — fuera rango Sirio" if rsi_v>CONFIG["rsi_max"] else "OK ✓")
    vol_r   = a["vol_r"]
    tardia_v = "\n⚠️ precio extendido &gt;2% sobre SMA8" if a.get("senal_tardia") else ""
    p1,p2,p3 = calc_probabilidades(a["fan"], d["adx"], rsi_v, vol_r)
    acc = pos["acc"]
    s25 = max(1,round(acc*0.25)); s30=max(1,round(acc*0.30))
    s20x= max(1,round(acc*0.20)); s25b=max(0,acc-s25-s30-s20x)
    ath   = d.get("ath_52w", 0) or 0
    ath_h = d.get("ath_hist", 0) or 0
    dist_ath = (f"ATH 52s: {ath:.2f}  (-{((ath-d['precio'])/ath*100):.1f}%)"
                if ath > d["precio"] else f"ATH 52s: {ath:.2f}  (zona ATH)")
    if ath_h and ath_h > ath:
        dist_ath += f" | Hist: {ath_h:.2f} (-{((ath_h-d['precio'])/ath_h*100):.1f}%)"
    etf_ref  = get_sector_etf(d.get("sector","N/A"))
    ia_ico   = "⭐" if ia["prob"]>=70 else "🔔" if ia["prob"]>=55 else "🔭"
    score    = a.get("score",0)
    barra    = "█"*int(score/10)+"░"*(10-int(score/10))
    prio_ico, prio_txt = calcular_prioridad(a, ia, d)

    def esc(t): return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    ctx   = esc(ia.get("contexto","N/D"))
    razon = esc(ia.get("razon",""))
    alerta= esc(ia.get("alerta",""))

    # ── Semáforos — emoji SIEMPRE al inicio de línea ─────────
    # MACD
    macd_ico = "🟢" if "Bull" in d["macd_e"] else "🔴"

    # RSI — zona óptima 40-58 (v7.3)
    if 45 <= rsi_v <= 55:    rsi_ico = "🟢"  # zona perfecta
    elif 40 <= rsi_v < 45:   rsi_ico = "🟡"  # recuperando
    elif 55 < rsi_v <= 58:   rsi_ico = "🟡"  # limite superior
    else:                     rsi_ico = "🔴"  # fuera de rango

    # ADX — fuerza de tendencia
    adx_v = d["adx"] if d["adx"] else 0
    if adx_v >= 25:    adx_ico = "🟢"
    elif adx_v >= 20:  adx_ico = "🟡"
    else:               adx_ico = "🔴"

    # ATR — volatilidad diaria
    if atr_pct < 2.0:    atr_ico = "🟢"; atr_tag = "baja volatilidad"
    elif atr_pct <= 4.0: atr_ico = "🟡"; atr_tag = "volatilidad media"
    else:                atr_ico = "🔴"; atr_tag = "alta — reduce tamaño"

    # Volumen
    if vol_r >= 1.5:    vol_ico = "🟢"; vol_tag = f"ALTO — {vol_r:.1f}x"
    elif vol_r >= 0.8:  vol_ico = "🟡"; vol_tag = f"normal — {vol_r:.1f}x"
    else:                vol_ico = "🔴"; vol_tag = f"BAJO — {vol_r:.1f}x"

    return (
        f"{prio_ico} <b>SISTEMA SIRIO — {prio_txt}</b>\n"
        f"🌟 <b>Solares Trading</b>\n"
        f"🕐 {hora}{pm_tag}\n"
        f"📈 Swing DIARIO 5-10 días\n\n"

        f"<b>{d['ticker']}</b>  {d.get('nombre','')}\n"
        f"🏭 Sector: {d.get('sector','N/A')}  |  Ref: <b>{etf_ref}</b>\n"
        f"💲 {d['precio']:.2f} USD  ({d['pct']:+.1f}%){tardia_v}\n"
        f"🏔 {dist_ath}\n\n"

        f"<b>📊 Abanico SMA {a['fan']}/4</b>\n"
        f"{'✅' if a['c1'] else '❌'} Precio &gt; SMA200  {d['sma200']:.2f}\n"
        f"{'✅' if a['c2'] else '❌'} SMA50  &gt; SMA200  {d['sma50']:.2f}\n"
        f"{'✅' if a['c3'] else '❌'} SMA20  &gt; SMA50   {d['sma20']:.2f}\n"
        f"{'✅' if a['c4'] else '❌'} Precio &gt; SMA20   {d['sma20']:.2f}\n"
        f"{a.get('sma_label','📊 SMA: sin clasificar')}\n"
        f"{'✅' if d.get('sma20_alcista',True) else '❌'} SMA20 pendiente: {d.get('sma20_pendiente',0):+.2f}%  |  Pullback zona: {a.get('dist_sma20_pct',0):+.1f}% {'✅' if a.get('en_pullback_sma20') else '⚠️'}\n\n"

        f"<b>📏 Indicadores</b>\n"
        f"{macd_ico} MACD: {d['macd_e']}\n"
        f"{rsi_ico} RSI:   {rsi_v:.0f}  ({rsi_tag})\n"
        f"{adx_ico} ADX:   {d['adx_e']} ({adx_v:.0f})\n"
        f"{atr_ico} ATR:   {atr_pct}%  ({atr_tag})\n"
        f"{vol_ico} Vol:   {vol_tag}\n"
        f"<i>(vs promedio últimos 20 días)</i>\n\n"

        f"<b>{d.get('vela_patron','Sin patrón')}</b>\n"
        f"<i>Fuerza de la vela: {d.get('vela_fuerza',0)}%</i>\n\n"

        f"<b>📈 Tendencia semanal</b>\n{tendencia_semanal}\n\n"

        + (f"<b>📰 Noticias y Sentiment</b>\n{sent_texto}\n\n" if sent_texto else "")

        + f"<b>🎯 Score Sistema Sirio: {score}/100</b>\n"
        f"<code>{barra}</code>\n"
        f"<i>{a.get('score_det','')}</i>\n\n"

        f"<b>💰 Posición — Riesgo $100 máximo</b>\n"
        f"📍 Entrada:  <b>{d['precio']:.2f} USD</b>  |  <b>{pos['acc']} acciones</b>  |  Capital: {pos['tot']:.0f} USD\n"
        f"🛑 Stop:     <b>{pos['stop']:.2f}</b>  ({'low vela' if pos.get('metodo_stop','')=='candle_low' else 'ATR/6%'})  |  Riesgo: {pos['perd']:.0f} USD\n"
        f"🎯 T1 (1.5R): <b>{pos['t1']:.2f}</b>  → cerrar {pos['acc_t1']} acc (15%)\n"
        f"🎯 T2 (2.5R): <b>{pos['t2']:.2f}</b>  → cerrar {pos['acc_t2']} acc (50%)\n"
        f"🚀 Trailing:  {pos['acc_t3']} acc (35%) — swing lows / cierre &lt; SMA20\n\n"

        f"<b>🗓 Gestión de Stop — Sistema Sirio v8</b>\n"
        f"✅ Al llegar T1 ({pos['t1']:.2f}) → mover stop del 85% a BE ({pos['be']:.2f})\n"
        f"✅ Al llegar T2 ({pos['t2']:.2f}) → mover stop del 35% a +0.3R ({pos['stop_post_t2_lo']:.2f})\n"
        f"   Opción agresiva: stop a +0.5R ({pos['stop_post_t2_hi']:.2f})\n"
        f"🚀 Trailing 35%: por swing lows o cierre diario bajo SMA20\n\n"

        f"<b>📊 Probabilidades</b>\n"
        f"T1: {p1}%  |  T2: {p2}%  |  T3: {p3}%\n\n"

        # Sección IA — solo si no hubo error
        + (
            f"<b>{ia_ico} IA {ia['prob']}% — {ia['senal']}</b>\n"
            + (f"🌍 {ctx}\n" if ctx else "")
            + (f"📐 {razon}\n" if razon else "")
            + (f"👁 Vigilar: {alerta}\n" if alerta else "")
            + "\n"
            if ia.get("senal") not in ("ERROR", "SIN IA", "")
            else f"<b>🤖 IA</b> — <i>análisis no disponible en esta señal</i>\n\n"
        )

        + f"<i>Sistema Sirio v8 — Solares</i> 🌟\n"
        f"─────────────────────────────────\n"
        f"⚠️ <i>AVISO LEGAL: Esta información tiene carácter "
        f"exclusivamente educativo e informativo. No constituye "
        f"asesoramiento financiero ni recomendación de inversión. "
        f"Los mercados implican riesgo de pérdida de capital. "
        f"Cada persona es responsable de sus propias decisiones. "
        f"Solares no gestiona fondos de terceros.</i>"
    )

def build_msg_corto(d, a, pos, ia, tendencia_semanal, dist_s8=None):
    """
    Mensaje de ACTUALIZACIÓN — para tickers que ya recibieron el mensaje largo hoy.
    Solo se envía si el ticker está EN ZONA o CERCA (≤2% sobre SMA8).
    Si está EXTENDIDA, el main ya lo filtra antes de llegar aquí.
    """
    hora     = hora_et()
    rsi_v    = d["rsi"] if d["rsi"] else 0
    vol_r    = a["vol_r"]
    score    = a.get("score", 0)
    prio_ico, prio_txt = calcular_prioridad(a, ia, d)

    # Zona de entrada
    sma8v   = d["sma8"] or 0
    if dist_s8 is None:
        dist_s8 = ((d["precio"] - sma8v) / sma8v * 100) if sma8v > 0 else 0

    if dist_s8 <= 0.5:
        zona_txt = f"🎯 EN SMA8 — {dist_s8:.1f}% sobre SMA8 ({sma8v:.2f}) — ENTRADA IDEAL"
    elif dist_s8 <= 1.0:
        zona_txt = f"✅ EN ZONA — {dist_s8:.1f}% sobre SMA8 ({sma8v:.2f})"
    else:
        zona_txt = f"⚠️ CERCA del límite — {dist_s8:.1f}% sobre SMA8 ({sma8v:.2f})"

    # RSI semáforo
    rsi_ico = "🟢" if 50<=rsi_v<=65 else "🟡" if rsi_v<=72 else "🔴"

    # Vol semáforo
    vol_ico = "🔥" if vol_r>=1.5 else "✅" if vol_r>=0.8 else "⚠️"

    # IA resumen en 1 línea
    ia_txt = ""
    if ia.get("senal") not in ("ERROR","SIN IA","","ESPERAR"):
        ia_txt = f"\n🤖 IA {ia['prob']}% — {ia['senal']}: {ia.get('alerta','')}"
    elif ia.get("senal") == "ESPERAR":
        ia_txt = f"\n🤖 IA {ia['prob']}% — ESPERAR"

    def esc(t): return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    return (
        f"🔄 <b>ACTUALIZACIÓN — {d['ticker']}</b>  {prio_ico} {prio_txt}\n"
        f"🕐 {hora}\n\n"

        f"<b>¿Sigue válida la señal?</b>\n"
        f"{zona_txt}\n"
        f"{rsi_ico} RSI: {rsi_v:.0f}  |  {vol_ico} Vol: {vol_r:.1f}x  |  Score: {score}/100\n"
        f"{'🟢' if 'Bull' in d['macd_e'] else '🔴'} MACD: {d['macd_e']}\n"
        f"{tendencia_semanal}\n"
        f"{ia_txt}\n\n"

        f"<b>Niveles vigentes</b>\n"
        f"💲 Precio actual: <b>{d['precio']:.2f}</b> ({d['pct']:+.1f}%)\n"
        f"🛑 Stop: {pos['stop']:.2f}  |  🎯 T1: {pos['t1']:.2f}  |  T2: {pos['t2']:.2f}\n\n"

        f"<i>Mensaje completo ya fue enviado hoy — este es el seguimiento (v8).\n"
        f"Sistema Sirio v7 — Solares</i> 🌟"
    )


    """
    🔴 ALTA    — Score 75+, ADX fuerte, vol 1.5x+, IA=ENTRAR, no tardía, RSI<68
    🟡 MEDIA   — Score 60-74, condiciones buenas, algún factor marginal
    🟢 INFO    — Score 55-59, señal válida pero esperar confirmación
    """
    score  = a.get("score", 0)
    adx    = d.get("adx", 0) or 0
    vol_r  = a.get("vol_r", 0)
    tardia = a.get("senal_tardia", False)
    ia_ok  = "ENTRAR" in ia.get("senal", "").upper()
    rsi_v  = d.get("rsi", 50) or 50
    if (score>=75 and adx>=25 and vol_r>=1.5 and not tardia and ia_ok and rsi_v<=68):
        return "⭐", "ALTA PRIORIDAD — entra"
    elif score>=60 and adx>=20 and vol_r>=1.0:
        return "🔔", "MEDIA PRIORIDAD — revisa"
    else:
        return "🔭", "INFORMATIVA — observa"

def verificar_señal_activa(ticker):
    """
    Re-verifica si una señal del día sigue válida para el resumen 4H.
    Activa = fan 4/4 + precio ≤ 3% sobre SMA8 + MACD alcista.
    """
    try:
        hist = yf.Ticker(ticker).history(period="60d", interval="1d")
        if hist.empty or len(hist)<30: return None
        c       = hist["Close"]
        precio  = float(c.iloc[-1])
        sma8v   = sma(c, 8);  sma20v = sma(c, 20)
        sma50v  = sma(c, 50); sma200v= sma(c, 200)
        if not all([sma8v, sma20v, sma50v, sma200v]): return None
        # Fan consistente con analizar() — SMAs clave: 200, 50, 20
        fan     = sum([precio>sma200v, sma50v>sma200v, sma20v>sma50v, precio>sma20v])
        pct_s8  = (precio-sma8v)/sma8v*100
        rsi_v   = calc_rsi(c)
        _,_,_,macd_e = calc_macd(c)
        return {
            "precio": round(precio,2), "sma8": round(sma8v,2),
            "fan": fan, "pct_s8": round(pct_s8,1),
            "macd": macd_e, "rsi": round(rsi_v,0) if rsi_v else 0,
            "activa": fan==4 and pct_s8<=3.0 and "Bull" in macd_e
        }
    except:
        return None

def enviar_resumen_4h(estado):
    """
    Lógica limpia de notificaciones:
    · Solo envía si hay señales activas que siguen vigentes después de 4h.
    · Si no hay nada: silencio total (sin resultados no se reporta aquí,
      el status periódico ya lo maneja con "Sin resultados.").
    Momentos de re-verificación:
    · 1:00pm ET — señales de la mañana ¿siguen activas?
    · 3:30pm ET — señales del mediodía ¿siguen activas?
    """
    ahora    = datetime.now(ET)
    hora_act = ahora.hour
    min_act  = ahora.minute
    enviados = estado.get("resumen_4h", [])

    momentos = [
        (13,  0),
        (15, 30),
    ]

    for (h_res, m_res) in momentos:
        clave = f"{h_res}:{m_res:02d}"
        if clave in enviados: continue
        mins_desde = (hora_act*60 + min_act) - (h_res*60 + m_res)
        if not (0 <= mins_desde <= 12): continue

        alertados = estado.get("alertados", [])
        if not alertados:
            # Nada que re-verificar — silencio
            enviados.append(clave)
            estado["resumen_4h"] = enviados
            guardar_estado(estado)
            print(f"  [4H] {clave} — sin alertados, silencio.")
            continue

        activas = []
        print(f"  [4H] Re-verificando {len(alertados)} señal(es) a las {clave}...")
        for t in alertados:
            v = verificar_señal_activa(t)
            if v and v["activa"]:
                activas.append((t, v))

        if not activas:
            # Señales del día ya no vigentes — silencio
            enviados.append(clave)
            estado["resumen_4h"] = enviados
            guardar_estado(estado)
            print(f"  [4H] {clave} — señales cerradas, silencio.")
            continue

        # Solo llega aquí si hay señales que SIGUEN activas
        lines = []
        for t, v in activas:
            dist = f"+{v['pct_s8']:.1f}% SMA8" if v['pct_s8'] > 0 else "en SMA8"
            lines.append(f"  ✅ <b>{t}</b> · ${v['precio']} · RSI {v['rsi']} · {dist}")

        send_telegram(
            f"🔄 <b>Sirio — señal sigue activa</b>\n"
            f"🕐 {hora_et()}\n\n"
            + "\n".join(lines)
            + f"\n\n<i>Sistema Sirio v7 — Solares</i> 🌟"
        )
        enviados.append(clave)
        estado["resumen_4h"] = enviados
        guardar_estado(estado)
        print(f"  [4H] {clave} — {len(activas)} señal(es) activa(s), alerta enviada.")



def main():
    print(f"\n{'='*55}")
    print(f"  SISTEMA SIRIO — Solares v7   {hora_et()}")
    print(f"{'='*55}\n")

    estado  = cargar_estado()
    ya      = estado.get("alertados", [])
    count   = estado.get("count", 0)

    ahora_et  = datetime.now(ET)
    hora_act  = ahora_et.hour
    min_act   = ahora_et.minute

    # ── GUARDIA: FESTIVO NYSE ────────────────────────────────
    es_festivo, nombre_festivo = es_dia_festivo_nyse()
    if es_festivo:
        if not estado.get("festivo_enviado", False):
            ok_f = send_telegram(
                f"📅 <b>SISTEMA SIRIO — Festivo NYSE</b>\n"
                f"🕐 {hora_et()}\n\n"
                f"🏛 <b>{nombre_festivo}</b> — NYSE cerrado hoy\n"
                f"Sirio no escaneará. Próximo día hábil activo.\n\n"
                f"<i>Sistema Sirio v8 — Solares</i> 🌟"
            )
            if ok_f:
                estado["festivo_enviado"] = True
                guardar_estado(estado)
        print(f"📅 Festivo NYSE: {nombre_festivo} — saliendo.")
        return

    # ── GUARDIA: FUERA DE HORARIO ────────────────────────────
    # Antes de 7am ET: demasiado temprano, salir silencioso
    if hora_act < 7:
        print(f"⏸ Muy temprano ({hora_act}:{min_act:02d} ET) — esperando 7:00am ET.")
        return

    post_cierre = (hora_act > 16) or (hora_act == 16 and min_act > 0)
    if post_cierre:
        print("⏹ Después del cierre — solo resumen 4H.")
        enviar_resumen_4h(estado)
        return

    # ── HEARTBEAT — se envía DESPUÉS del scan, solo si no hubo señales ──
    # (bloque movido al final de main(), antes de STATUS PERIÓDICO)

    # Verificar si toca enviar resumen de 4H (independiente de señales)
    enviar_resumen_4h(estado)

    tickers       = obtener_universo()
    largo_enviado = estado.get("largo_enviado", [])
    actualizaciones_hoy = estado.get("actualizaciones_hoy", {})
    primera_alerta_hora = estado.get("primera_alerta_hora", {})

    pendientes = tickers
    print(f"\nUniverso a revisar: {len(pendientes)} | "
          f"Señales nuevas posibles: {len(pendientes)-len(largo_enviado)} | "
          f"Actualizaciones posibles: {len(largo_enviado)}\n")

    nuevas = 0; razones = {}

    for ticker in pendientes:
        print(f"  {ticker}...", end=" ", flush=True)
        d = obtener_datos(ticker)
        if d.get("error"):
            print(f"skip ({d['error'][:35]})")
            razones["error"] = razones.get("error", 0) + 1; continue
        rsi_s = f"{d['rsi']:.0f}" if d["rsi"] else "N/A"
        print(f"{d['precio']:.2f} RSI:{rsi_s} MACD:{d['macd_e']} [{d.get('sector','?')[:10]}]")
        a = analizar(d)

        # ── v8: ETF commodities — excluidos del swing scanner ─────────────
        if ticker in CONFIG.get("etf_commodities_excluidos", []):
            print(f"    skip: ETF commodity excluido — {ticker}")
            razones["etf_commodity"] = razones.get("etf_commodity", 0) + 1; continue

        # ── v8: FILTRO ZOMBIE — ticker sin vela real repetido N scans ────────
        zombie = cargar_zombie()
        hoy_str = str(date.today())
        # Si está en período de silencio
        silenciados = zombie.get("silenciados", {})
        if ticker in silenciados:
            desde = silenciados[ticker]
            try:
                dias_silencio = (date.today() - date.fromisoformat(desde)).days
                if dias_silencio < CONFIG["zombie_silencio_dias"]:
                    print(f"    skip: zombie silenciado ({dias_silencio}d/{CONFIG['zombie_silencio_dias']}d)")
                    razones["zombie"] = razones.get("zombie", 0) + 1; continue
                else:
                    del silenciados[ticker]   # expiró el silencio
                    zombie["contadores"].pop(ticker, None)
                    guardar_zombie(zombie)
            except: pass

        # ── PASO 1 OBLIGATORIO: precio SOBRE SMA200 — veto absoluto ──────
        # Regla Lu: sin esta condición NO hay entrada. Gate previo a cualquier score.
        if d.get("sma200") and d["precio"] < d["sma200"]:
            print(f"    skip: precio {d['precio']:.2f} < SMA200 {d['sma200']:.2f} — Paso 1 violado")
            razones["sma200"] = razones.get("sma200", 0) + 1; continue

        # ── Filtros técnicos v7.3 ──────────────────────────────────────────────
        # Fan mínimo: 2/4. Fan 2 = SMA200 OK + SMA50>SMA200.
        # La clasificación SMA (Trampolín/Tendencia) ya está en analizar().
        fan_minimo_observar = CONFIG["min_fan_to_alert"]  # = 2
        if a["fan"] < fan_minimo_observar or not a["en_rango"] or not a["vol_ok"]:
            razones["fan"] = razones.get("fan", 0) + 1; continue
        es_fan2 = (a["fan"] == 2); es_fan3 = (a["fan"] == 3)
        # Fan 2: necesita score más alto y vela señal fuerte para compensar
        score_minimo_efectivo = (72 if es_fan2 else 68 if es_fan3 else CONFIG["score_minimo"])

        # ── FILTRO ATR MÍNIMO $1 (v7.3) ───────────────────────────────────────
        atr_check = d.get("atr", 0) or 0
        if atr_check < CONFIG.get("atr_min_usd", 1.0):
            print(f"    skip: ATR ${atr_check:.2f} < $1.00 — poco rango diario para swing")
            razones["atr_bajo"] = razones.get("atr_bajo", 0) + 1; continue

        # ── v8: SMA20 PENDIENTE ALCISTA — filtro obligatorio ─────────────────
        if not d.get("sma20_alcista", True):
            pend = d.get("sma20_pendiente", 0)
            print(f"    skip: SMA20 sin pendiente alcista ({pend:+.3f}%) — filtro base v8")
            razones["sma20_bajista"] = razones.get("sma20_bajista", 0) + 1; continue

        # ── FILTRO BETA MÍNIMO 1.0 (v7.3) ─────────────────────────────────────
        beta_check = d.get("beta", 0) or 0
        if 0 < beta_check < CONFIG.get("beta_min", 1.0):
            print(f"    skip: Beta {beta_check:.2f} < 1.0 — se mueve menos que el mercado")
            razones["beta_bajo"] = razones.get("beta_bajo", 0) + 1; continue
        # Volumen ratio mínimo 0.7x
        if a["vol_r"] < 0.7:
            print(f"    skip: vol_r {a['vol_r']:.1f}x < 0.7x mínimo")
            razones["vol_bajo"] = razones.get("vol_bajo", 0) + 1; continue
        # ── FILTRO RSI 40-58 (v7.3) ────────────────────────────────────────────
        # Entrar cuando el precio acaba de recuperar, no cuando ya corrió.
        # RSI < 40: mercado aún débil, esperar. RSI > 58: ya corrió mucho.
        rsi_v_check = d.get("rsi", 0) or 0
        if rsi_v_check > CONFIG["rsi_max"]:  # > 58
            print(f"    skip: RSI {rsi_v_check:.0f} > {CONFIG['rsi_max']} — sobrecomprado para Sirio v7.3")
            razones["rsi_alto"] = razones.get("rsi_alto", 0) + 1; continue
        if rsi_v_check > 0 and rsi_v_check < CONFIG["rsi_min"]:  # < 40
            print(f"    skip: RSI {rsi_v_check:.0f} < {CONFIG['rsi_min']} — precio aún débil")
            razones["rsi_bajo"] = razones.get("rsi_bajo", 0) + 1; continue
        # ETFs de renta fija
        nombre_check = d.get("nombre","").lower()
        if d.get("sector","") in ("","N/A") and any(w in nombre_check for w in
                ["treasury","bond","rate","fixed","floating","ultra short","t-bill"]):
            print(f"    skip: ETF renta fija ({d.get('nombre','')[:25]})")
            razones["renta_fija"] = razones.get("renta_fija", 0) + 1; continue
        if a.get("senal_tardia") and a["fan"] < 4:
            print(f"    skip: tardío >2% SMA8")
            razones["tardia"] = razones.get("tardia", 0) + 1; continue
        if a["score"] < score_minimo_efectivo:
            print(f"    skip: score {a['score']}/100 (min {score_minimo_efectivo}{'—fan3' if es_fan3 else ''})")
            razones["score"] = razones.get("score", 0) + 1; continue
        hay_earn, dias_earn = check_earnings_proximos(ticker)
        if hay_earn:
            print(f"    skip: earnings en {dias_earn}d")
            razones["earnings"] = razones.get("earnings", 0) + 1; continue
        semanal_ok, tend_sem = check_tendencia_semanal(ticker)
        if not semanal_ok:
            print(f"    skip: {tend_sem}")
            razones["semanal"] = razones.get("semanal", 0) + 1; continue

        # ── FILTRO ATH 52s — 25% MÍNIMO ────────────────────
        # Swing 5-10 días necesita ≥25% de espacio bajo el ATH 52s.
        # Con menos margen el precio topa resistencia antes de completar el swing.
        # Regla Lu: precio debe estar al menos 25% bajo el máximo histórico de 52s.
        # FIX v7.1: eliminada la excepción de "ruptura" (dist_ath_pct < 0).
        # Razón: yfinance.fast_info entrega el ATH con delay — un precio que supera
        # levemente el ATH cacheado NO es una ruptura confirmada, es lag de datos.
        # Resultado: acciones en/sobre ATH ahora quedan filtradas correctamente.
        ath_v = d.get("ath_52w", 0)
        if ath_v and ath_v > 0 and d["precio"] > 0:
            dist_ath_pct = (ath_v - d["precio"]) / ath_v * 100
            if dist_ath_pct < 20.0:   # incluye negativo (en ATH o por encima)
                print(f"    skip: ATH 52s — {dist_ath_pct:.1f}% del maximo 52s "
                      f"({ath_v:.2f}) — necesita >=20% de espacio")
                razones["cerca_ath"] = razones.get("cerca_ath", 0) + 1
                continue

        # ── Verificar zona de entrada ─────────────────────────
        largo_enviado = estado.get("largo_enviado", [])
        es_repetido   = ticker in largo_enviado

        sma8v      = d.get("sma8") or 0
        dist_s8    = ((d["precio"] - sma8v) / sma8v * 100) if sma8v > 0 else 0
        sma20v     = d.get("sma20") or 0
        dist_sma20 = a.get("dist_sma20_pct", ((d["precio"] - sma20v) / sma20v * 100) if sma20v > 0 else 0)

        # v8 — Zona de entrada: pullback a SMA20 (no zona SMA8)
        # Precio válido entre -2% bajo SMA20 y +5% sobre SMA20
        if dist_sma20 > CONFIG["pullback_sma20_max_pct"]:
            print(f"    skip: {dist_sma20:.1f}% sobre SMA20 — fuera de zona pullback (max +{CONFIG['pullback_sma20_max_pct']}%)")
            # Registrar en zombie counter — ticker pasa filtros pero no tiene setup
            zombie = cargar_zombie()
            cnt = zombie.get("contadores", {})
            cnt[ticker] = cnt.get(ticker, 0) + 1
            zombie["contadores"] = cnt
            if cnt[ticker] >= CONFIG["zombie_max_scans"]:
                zombie.setdefault("silenciados", {})[ticker] = str(date.today())
                print(f"    ⚠️ zombie: {ticker} silenciado por {CONFIG['zombie_silencio_dias']} días")
            guardar_zombie(zombie)
            razones["fuera_pullback"] = razones.get("fuera_pullback", 0) + 1
            continue
        # Limpiar contador zombie si el ticker está en zona válida
        zombie = cargar_zombie()
        if ticker in zombie.get("contadores", {}):
            del zombie["contadores"][ticker]
            guardar_zombie(zombie)

        # ── COOLDOWN ACTUALIZACIÓN — mínimo 2h entre señal inicial y update ──
        # Reglas:
        #  1. Máx 1 actualización por ticker por día
        #  2. Mínimo 120 min desde la señal inicial
        #  3. Fail-safe: si no hay timestamp (cache miss), NO enviar update — evitar spam
        if es_repetido:
            if actualizaciones_hoy.get(ticker, False):
                print(f"    skip: actualización de {ticker} ya enviada hoy")
                continue
            ts_inicial = primera_alerta_hora.get(ticker)
            if not ts_inicial:
                # Sin timestamp = estado perdido por cache miss → skip update (fail-safe)
                print(f"    skip: {ticker} sin timestamp de señal inicial (cache miss) — no update")
                razones["cooldown"] = razones.get("cooldown", 0) + 1
                continue
            mins_desde_alerta = (time.time() - ts_inicial) / 60
            if mins_desde_alerta < 120:
                print(f"    skip: cooling-off {ticker} — {mins_desde_alerta:.0f}min < 120min mínimo")
                razones["cooldown"] = razones.get("cooldown", 0) + 1
                continue

        print(f"  ⭐ FAN 4/4 | Score {a['score']}/100 | "
              f"{'ACTUALIZACIÓN' if es_repetido else 'NUEVA SEÑAL'} | "
              f"Zona: {dist_s8:.1f}% sobre SMA8")

        sc_sent, txt_sent, datos_sent = get_sentiment_completo(ticker)
        noticias_yh = []
        if datos_sent.get("yahoo"):
            noticias_yh = datos_sent["yahoo"].get("titulares", [])
        pos = posicion(d["precio"], d.get("ath_52w"), d.get("atr"), d.get("beta"), d.get("vela_low"))
        ia  = analizar_ia(d, a, pos, sc_sent, tend_sem, noticias_yh)
        print(f"     IA: {ia['prob']}% — {ia['senal']}")

        if es_repetido:
            msg = build_msg_corto(d, a, pos, ia, tend_sem, dist_s8)
        elif es_fan3:
            # Fan 3/4: señal OBSERVAR — misma estructura pero con aviso claro
            msg = build_msg(d, a, pos, ia, txt_sent, tend_sem)
            aviso = "\U0001f52d <b>[OBSERVAR \u2014 Fan 3/4]</b> Estructura incompleta. Esperar cierre vela y confirmar SMA faltante.\n\n"
            msg = aviso + msg
        else:
            msg = build_msg(d, a, pos, ia, txt_sent, tend_sem)

        ok = send_telegram(msg)

        if ok:
            nuevas += 1
            if ticker not in ya:
                ya.append(ticker)
            if not es_repetido:
                largo_enviado.append(ticker)
                primera_alerta_hora[ticker] = time.time()  # timestamp para cooldown
            else:
                # Marcar actualización enviada para no repetirla hoy
                actualizaciones_hoy[ticker] = True
            estado["alertados"]          = ya
            estado["largo_enviado"]      = largo_enviado
            estado["actualizaciones_hoy"] = actualizaciones_hoy
            estado["primera_alerta_hora"]  = primera_alerta_hora
            estado["count"]              = count + nuevas
            estado["ts_ultimo_mensaje"]  = time.time()  # reset reloj de silencio
            guardar_estado(estado)
            registrar_backtest(ticker, d["precio"], pos["stop"],
                               pos["t1"], pos["t2"], pos["t3"],
                               a["score"], ia["senal"], d.get("vela_patron",""))
            guardar_en_diario(ticker, d["precio"], a["fan"],
                              datos_sent, ia["senal"], d.get("vela_patron",""))
            estado["ts_ultimo_mensaje"] = time.time()
            estado["mensajes_hoy"]      = estado.get("mensajes_hoy", 0) + 1
            tipo = "actualización" if es_repetido else "nueva señal"
            print(f"  ✅ Enviado como {tipo} (total hoy: {count+nuevas})")
        else:
            print(f"  ❌ Error Telegram")

    print(f"\nFin: {nuevas} señales nuevas | Total hoy: {count+nuevas} | Skips: {razones}")

    # ── HEARTBEAT — primer mensaje del día ──────────────────────────────────
    # Solo se envía si este run NO encontró señales nuevas.
    # Si hay señal → la señal ya es suficiente, el heartbeat sería ruido.
    # Si no hay señal → se envía para confirmar que Sirio está vivo y escaneando.
    if not estado.get("heartbeat_enviado", False) and nuevas == 0:
        vix_hb, vix_nivel_hb = get_vix()
        vix_str_hb = f"{vix_hb} ({vix_nivel_hb})" if vix_hb else "N/D"
        label_m = "🌅 Pre-Market" if hora_act < 9 else ("🔔 Apertura" if hora_act < 10 else "📡 En curso")
        try:
            off_h = ahora_et.utcoffset().total_seconds() / 3600
            zona_txt = "EDT (UTC-4)" if off_h == -4 else "EST (UTC-5)"
        except Exception:
            zona_txt = "ET"
        hb_ok = send_telegram(
            f"{label_m} <b>SISTEMA SIRIO — Solares</b>\n"
            f"🕐 {hora_et()} · {zona_txt}\n\n"
            f"✅ Sirio activo · primer escaneo del día\n"
            f"⚙️ ~500 tickers · 5 filtros base · SMA20↑ · pullback SMA20 · ATR>$1 · Beta>1\n"
            f"📊 VIX: <b>{vix_str_hb}</b>\n\n"
            f"<i>Señal nueva → aviso inmediato.</i>\n"
            f"<i>Sin señales → status cada 2h + cierre 3:30pm.</i>\n\n"
            f"<i>Sistema Sirio v8 — Solares</i> 🌟"
        )
        if hb_ok:
            estado["heartbeat_enviado"] = True
            estado["ts_ultimo_mensaje"] = time.time()
            estado["mensajes_hoy"]      = estado.get("mensajes_hoy", 0) + 1
            guardar_estado(estado)
            print("  [HB] Heartbeat enviado OK ✅")
        else:
            print("⚠️⚠️⚠️  TELEGRAM NO RESPONDE — verificar TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en Secrets  ⚠️⚠️⚠️")

    # ── STATUS PERIÓDICO — lógica de notificación ───────────────────────────
    # Reglas:
    #  1. Señal nueva en este run → ya se notificó, no duplicar
    #  2. ≥60 min desde cualquier mensaje → status breve
    #  3. Último scan del día (15:25+) → forzar siempre
    #  4. mensajes_hoy==0 al final del run → safety net (nunca día vacío)
    es_ultimo_scan  = (hora_act == 15 and min_act >= 25) or (hora_act > 15)
    ts_ultimo_msg   = estado.get("ts_ultimo_mensaje", 0)
    mins_silencio   = (time.time() - ts_ultimo_msg) / 60 if ts_ultimo_msg else 9999
    mensajes_hoy    = estado.get("mensajes_hoy", 0)
    safety_net      = (mensajes_hoy == 0)
    debe_status     = (nuevas == 0) and ((mins_silencio >= 60) or es_ultimo_scan or safety_net)

    if debe_status:
        mapa = {"fan":f"Fan<{CONFIG['min_fan_to_alert']}/4","sma200":"Precio<SMA200","score":"Score<65","earnings":"Earnings",
                "atr_bajo":"ATR<$1","beta_bajo":"Beta<1","rsi_alto":f"RSI>{CONFIG['rsi_max']}","rsi_bajo":f"RSI<{CONFIG['rsi_min']}",
                "semanal":"Semanal baj.","tardia":"Tardía","error":"Error datos",
                "cerca_ath":"ATH<25%","extendida":">2%SMA8",
                "cooldown":"Cooling","vol_bajo":"Vol bajo","rsi_extremo":"RSI>80"}
        top5 = sorted(razones.items(), key=lambda x:x[1], reverse=True)[:5]
        skips_txt = " · ".join(f"{mapa.get(k,k)}:{v}" for k,v in top5) if top5 else "sin datos"
        señales_hoy = count + nuevas
        if es_ultimo_scan and señales_hoy == 0:
            msg_status = "⚪ Sin resultados."
        elif es_ultimo_scan:
            msg_status = f"🔔 Cierre · {señales_hoy} señal(es) hoy."
        elif señales_hoy > 0:
            msg_status = f"📡 {señales_hoy} señal(es) hoy · sin nuevas ahora."
        else:
            msg_status = "⚪ Sin resultados."

        ok_sc = send_telegram(
            f"<b>Sirio v7</b> · {hora_et()}\n"
            f"{msg_status}"
        )
        if ok_sc:
            estado["ts_ultimo_mensaje"]         = time.time()
            estado["ts_ultimo_sin_senal"]       = time.time()
            estado["sin_coincidencias_enviado"] = True
            estado["mensajes_hoy"]              = estado.get("mensajes_hoy", 0) + 1
            guardar_estado(estado)

if __name__ == "__main__":
    try:
        main()
    except Exception as _ex_fatal:
        import traceback as _tb
        _err_txt = str(_ex_fatal)[:200]
        _tb_txt  = _tb.format_exc()[-300:]
        print(f"FATAL: {_ex_fatal}")
        try:
            send_telegram(
                f"🚨 <b>SISTEMA SIRIO — ERROR CRÍTICO</b>\n"
                f"🕐 {hora_et()}\n\n"
                f"<code>{_err_txt}</code>\n\n"
                f"<i>El bot se detuvo inesperadamente.\n"
                f"Revisar GitHub Actions → pestaña Runs.</i>\n\n"
                f"<i>Sistema Sirio v7 — Solares</i>"
            )
        except Exception:
            pass
