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

# v9: modulos nuevos — regimen de mercado y score compuesto de 5 factores.
# Deben estar en la raiz del repo, junto a este archivo.
from detectar_regimen import detectar_regimen
from composite_score import composite_score
from carta_natal_transitos import tránsitos_de_hoy

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
AV_KEY           = os.environ.get("ALPHAVANTAGE_KEY", "")

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
    "capital_usd":         20275,
    "riesgo_fijo_usd":     100,
    "stop_loss_pct":       6.0,
    "price_min":           10.0,
    "price_max":           150.0,
    "rsi_min":             40,    # v9: corregido — era 45, canon dice 40 (Sistema 6 Pasos)
    "rsi_max":             58,    # v9: corregido — era 65, canon dice 58
    "min_volume_abs":      500_000,
    "min_fan_to_alert":    2,     # v9: corregido — era 4 hardcoded en 2 lugares distintos
                                   # (CONFIG decia 4, pero el loop tenia fan_minimo_observar=3
                                   # hardcoded sin leer de CONFIG). Ahora es UNA sola fuente de
                                   # verdad y el loop la lee de aqui. Score compensa el fan bajo
                                   # (ver score_minimo_efectivo mas abajo) — LU: confirmame si
                                   # preferis mantener el piso en 3 en vez de 2, es tu llamada.
    "score_minimo":        65,    # 65/100 acordado originalmente
    "earnings_dias_min":   5,
    "max_tickers_scan":    900,  # v8: S&P500 + curado + movers  # ahora tenemos universo grande
    # Sin límite de señales — el bot informa, Lu decide
    # El score compuesto (fan+RSI+ADX+vol+vela) ya filtra lo irrelevante
    "max_alertas_dia":     99,
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

# v9: CICLO — aritmetica de fechas pura, sin API externa.
# v9.1 (30-jul-2026): activado por defecto — Lu pidio dejar de decidir por
# ella. El repo es privado y es su dato, su llamada, no la mia.
CICLO_MOSTRAR_EN_BOT = True
CICLO_ANCLA_FECHA = date(2026, 7, 11)   # dia 1 del ciclo confirmado 30-jul-2026
CICLO_DURACION_DIAS = 27

def fase_ciclo_hoy():
    dias = (date.today() - CICLO_ANCLA_FECHA).days % CICLO_DURACION_DIAS + 1
    if dias <= 5:                  return dias, "🔴 Menstrual", "SOLO revisar, NO entrar"
    elif dias <= 13:               return dias, "🟢 Folicular", "score normal"
    elif dias == 14 or dias == 15: return dias, "⭐ Ovulación", "mejor ventana del mes"
    else:                          return dias, "🟡 Lútea", "reducir tamaño, no abrir nuevas"

# v9: TRANSITOS REALES (ephem, calculado grado por grado, no rangos de fecha
# aproximados). Activado por defecto — Lu pidió dejar de decidir por ella;
# el repo es privado y es su dato, su llamada.
TRANSITOS_MOSTRAR_EN_BOT = True

def resumen_astrologico_hoy(max_aspectos=3):
    """Devuelve un bloque corto con los aspectos natales activos hoy (via
    Swiss Ephemeris, carta_natal_transitos.py), o cadena vacia si no hay
    ninguno dentro de orbe. No falla el bot si algo sale mal (no bloqueante)."""
    try:
        aspectos = tránsitos_de_hoy()[:max_aspectos]
        if not aspectos:
            return ""
        lineas = "\n".join(f"  • {a.texto()}" for a in aspectos)
        return f"✨ <b>Tránsitos de hoy:</b>\n{lineas}\n"
    except Exception as ex:
        print(f"  [ASTRO] Error (no bloqueante): {ex}")
        return ""


def cargar_estado():
    try:
        with open(CONFIG["state_file"],"r",encoding="utf-8") as f:
            e=json.load(f)
        if e.get("fecha")!=str(date.today()):
            return {"fecha":str(date.today()),"alertados":[],"largo_enviado":[],"heartbeat_enviado":False,"primera_alerta_hora":{},
                    "actualizaciones_hoy":{},"sin_coincidencias_enviado":False,
                "fondito_chequeado_hoy":False,"count":0}
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
        if "fondito_chequeado_hoy" not in e:
            e["fondito_chequeado_hoy"] = False
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
    # v9: BULL_KW/BEAR_KW estaban referenciadas pero nunca definidas — el
    # try/except silencioso alrededor de esta funcion ocultaba el crash,
    # asi que esto SIEMPRE devolvia None sin que se notara en los logs.
    BULL_KW = ["surge","soars","rally","upgrade","beat","record","breakout",
               "bullish","gains","jumps","outperform","buy rating","raises target"]
    BEAR_KW = ["plunge","crash","downgrade","miss","selloff","bearish","falls",
               "slides","underperform","sell rating","cuts target","warns","lawsuit"]
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
    # ── v8: ESPACIAL / SpaceX proxy ───────────────────────────
    "RKLB","ASTS","LUNR","MNTS","RDW","AXON",
    # ── v8: NUCLEAR / Uranio ──────────────────────────────────
    "CCJ","NNE","UEC","DNN","LEU","UUUU","OKLO","VST","CEG",
    # ── v8: HOMEBUILDERS ──────────────────────────────────────
    "DHI","LEN","PHM","TOL","KBH","MDC","NVR","SKY","MHO","TMHC",
    # ── v8: AI INFRAESTRUCTURA ────────────────────────────────
    "VRT","CDNS","ANET","COHR","BE","POWL",
    # ── v8: DEFENSA MODERNA ───────────────────────────────────
    "LDOS","CACI","BAH","SAIC","TDG","HWM","CW","BWXT",
    # ── v8: GLP-1 / VIDA NUEVA ────────────────────────────────
    "VKTX","RVNC","CELH","CAVA",
    # ── v8: CONSUMO RESILIENTE ────────────────────────────────
    "BROS","SG","WING","TXRH",
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

    # Fuente dinámica 3: S&P 500 (auto-descarga de Wikipedia cada semana)
    añadidos_sp = 0
    try:
        sp500_df = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"}
        )[0]
        sp500_tickers = sp500_df["Symbol"].str.replace(".", "-").tolist()
        for t in sp500_tickers:
            if t not in universo and len(t) <= 5:
                universo.append(t); añadidos_sp += 1
        print(f"  S&P 500 auto: +{añadidos_sp} tickers")
    except Exception as ex:
        print(f"  S&P 500 no disponible: {ex}")

    # Fuente 4: top movers del día (gappers y activos)
    añadidos_movers = 0
    try:
        trending_tickers = [
            "GME","AMC","BBBY","SOUN","NVTS","WULF","APLD","LUNR",
            "RKLB","ASTS","SMR","OKLO","NNE","KTOS","CELH","CAVA",
            "HOOD","MSTR","COIN","RIOT","MARA",
        ]
        for t in trending_tickers:
            if t not in universo:
                universo.append(t); añadidos_movers += 1
    except:
        pass

    # v9: TICKERS_NUEVOS_V8 estaba definida (espacial, nuclear, homebuilders,
    # AI-infra, defensa, GLP-1, cripto-infra, agua, consumo, energia alt. —
    # ~45 tickers) pero NUNCA conectada aca — confirmado con grep, cero usos
    # en todo el archivo. El universo v8 llevaba corriendo sin ese bloque
    # completo desde que se creo.
    añadidos_v8 = 0
    for t in TICKERS_NUEVOS_V8:
        if t not in universo:
            universo.append(t); añadidos_v8 += 1

    resultado = universo[:CONFIG["max_tickers_scan"]]
    print(f"Universo total: {len(resultado)} tickers "
          f"(curado: {len(UNIVERSO_CURADO)}, "
          f"TV: +{añadidos_tv}, Finviz: +{añadidos_fv}, "
          f"SP500: +{añadidos_sp}, movers: +{añadidos_movers}, "
          f"v9_nuevos: +{añadidos_v8})")
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
                "vela_patron":vela_patron,"vela_fuerza":vela_fuerza,"error":None}
    except Exception as ex:
        return {"ticker":ticker,"error":str(ex)}

def analizar(d):
    p=d["precio"]; s8,s20,s50,s200=d["sma8"],d["sma20"],d["sma50"],d["sma200"]
    # Fan redefinido — SMAs clave: 200, 50, 20 (jerarquía de mayor a menor timeframe)
    # c1: Paso 1 del sistema — precio debe estar SOBRE SMA200
    c1=bool(p>s200) if s200 else False        # Precio > SMA200 (PASO 1 — veto si falla)
    c2=bool(s50>s200) if s50 and s200 else False  # SMA50 > SMA200 (tendencia estructural)
    c3=bool(s20>s50) if s20 and s50 else False    # SMA20 > SMA50  (impulso medio)
    c4=bool(p>s20) if s20 else False              # Precio > SMA20  (momentum inmediato)
    fan=sum([c1,c2,c3,c4]); en_rango=CONFIG["price_min"]<=p<=CONFIG["price_max"]
    vol_r=d["vol_r"]; vol_ok=d["vh"]>=CONFIG["min_volume_abs"]
    tardia=bool(s8 and p>s8*1.02)
    rsi_v=d["rsi"] if d["rsi"] else 0; adx_v=d["adx"] if d["adx"] else 0
    pts_fan = fan*10
    # v9: corregido — RSI optimo es el CENTRO de la banda canon 40-58 (~49),
    # no 55-65. La version anterior premiaba RSI cerca de sobrecomprado,
    # que es justo lo que el Paso 4 del sistema busca EVITAR ("llegamos tarde").
    if 45<=rsi_v<=53:      pts_rsi = 15   # centro de la banda
    elif 40<=rsi_v<45 or 53<rsi_v<=58: pts_rsi = 10   # dentro de banda, no centrado
    elif 35<=rsi_v<40 or 58<rsi_v<=63: pts_rsi = 4    # borde, tolerancia chica
    else:                  pts_rsi = 0    # fuera de banda — sobrecomprado o debil
    pts_adx=(15 if adx_v>=30 else 12 if adx_v>=25 else 8 if adx_v>=20 else 0)
    # Volumen: 0.6x = 0 puntos. Necesita al menos 0.8x para sumar.
    if vol_r>=2.0:        pts_vol = 15
    elif vol_r>=1.5:      pts_vol = 12
    elif vol_r>=1.0:      pts_vol = 8
    elif vol_r>=0.8:      pts_vol = 4
    else:                 pts_vol = 0   # vol bajo — sin puntos
    pts_vela=int(d.get("vela_fuerza",0)*0.15)
    score=min(100,pts_fan+pts_rsi+pts_adx+pts_vol+pts_vela)
    return {"fan":fan,"c1":c1,"c2":c2,"c3":c3,"c4":c4,
            "en_rango":en_rango,"vol_r":vol_r,"vol_ok":vol_ok,"senal_tardia":tardia,
            "score":score,"score_det":f"Fan:{pts_fan}+RSI:{pts_rsi}+ADX:{pts_adx}+Vol:{pts_vol}+Vela:{pts_vela}"}

def posicion(precio, ath_52w=None, atr=None, beta=None):
    """
    Stop calculado con ATR + Beta — más preciso que el % fijo.
    - Beta alta (>1.5): stop más amplio (2.0x ATR) — acción volátil
    - Beta normal (0.8-1.5): stop estándar (1.5x ATR)
    - Beta baja (<0.8): stop más ajustado (1.2x ATR) — acción estable
    - Si no hay ATR disponible: usa 6% fijo como respaldo
    """
    if atr and atr > 0 and precio > 0:
        b = beta if beta and beta > 0 else 1.0
        if b >= 1.5:    mult = 2.0   # volátil — stop más amplio
        elif b >= 0.8:  mult = 1.5   # normal
        else:           mult = 1.2   # estable — stop ajustado
        riesgo_atr = atr * mult
        # No exceder 8% ni ser menor que 3% del precio
        riesgo_atr = max(precio*0.03, min(precio*0.08, riesgo_atr))
        stop = round(precio - riesgo_atr, 2)
    else:
        # Respaldo: 6% fijo
        stop = round(precio*(1-CONFIG["stop_loss_pct"]/100), 2)

    rx  = precio - stop
    acc = max(1, int(CONFIG["riesgo_fijo_usd"] / rx))
    tot = round(acc*precio, 2)
    perd= round(acc*rx, 2)
    # v8: T1=1.5R → 40% pos → stop BE | T2=2.5R → 35% | T3=4R runner EMA8
    t1  = round(precio + 1.5*rx, 2)
    t2  = round(precio + 2.5*rx, 2)
    t3  = round(precio + 4.0*rx, 2)
    if ath_52w and ath_52w > precio:
        techo = round(ath_52w*0.98, 2)
        if techo > t1:
            if t2 > techo: t2 = techo
            if t3 > techo: t3 = techo
    # R/R real calculado sobre T2 (Minervini/Van Tharp — no sobre T1)
    rr = round((t2-precio)/rx, 2)
    return {"acc":acc,"tot":tot,"stop":stop,"perd":perd,
            "t1":t1,"t2":t2,"t3":t3,"rx":rx,"rr":rr}

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

def build_msg(d, a, pos, ia, sent_texto, tendencia_semanal, noticia_av=None, sector_boost=0, tema=None):
    hora   = hora_et()
    pm_tag = " [PRE-MKTG]" if d.get("es_pm") else ""
    rsi_v  = d["rsi"] if d["rsi"] else 0
    vol_r  = a["vol_r"]
    score  = a.get("score", 0)
    adx_v  = d["adx"] if d["adx"] else 0
    atr_v  = d["atr"] if d["atr"] else 0
    atr_pct= round(atr_v/d["precio"]*100,1) if d["precio"]>0 else 0
    etf_ref= get_sector_etf(d.get("sector","N/A"))
    ath    = d.get("ath_52w", 0) or 0
    dist_ath = round((ath - d["precio"]) / ath * 100, 1) if ath > d["precio"] else 0
    rsi_ico = "🟢" if 45<=rsi_v<=58 else "🟡" if rsi_v<=65 else "🔴"
    adx_ico = "🟢" if adx_v>=25 else "🟡" if adx_v>=20 else "🔴"
    vol_ico = "🔥" if vol_r>=1.5 else "✅" if vol_r>=0.8 else "⚠️"
    macd_ico= "🟢" if "Bull" in d["macd_e"] else "🔴"
    fan_icons = "".join(["✅" if i < a["fan"] else "❌" for i in range(4)])
    prio_ico, prio_txt = calcular_prioridad(a, ia, d)
    ia_ico  = "⭐" if ia["prob"]>=70 else "🔔" if ia["prob"]>=55 else "🔭"
    ia_senal_txt = ia.get("senal","")
    ia_alerta = ia.get("alerta","")
    def esc(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    tema_txt = f" · 🎯 {tema.upper()}" if tema else ""
    # ── BLOQUE 1 ──────────────────────────────────────────────
    b1  = f"{prio_ico} <b>SIRIO v8 · {prio_txt}</b>\n"
    b1 += f"🕐 {hora}{pm_tag}\n\n"
    b1 += f"<b>{d['ticker']}</b>  {d.get('nombre','')}\n"
    b1 += f"🏭 {d.get('sector','N/A')} · {etf_ref}{tema_txt}\n"
    b1 += f"💲 ${d['precio']:.2f} ({d['pct']:+.1f}%)\n"
    b1 += f"🏔 ATH -{dist_ath:.1f}% libre · Espacio ✅\n\n"
    b1 += f"<b>Abanico SMA {a['fan']}/4</b>  {fan_icons}\n"
    b1 += f"<b>Puntuación: {score}/100</b>\n\n"
    b1 += f"{rsi_ico} RSI {rsi_v:.0f}  ·  {adx_ico} ADX {adx_v:.0f}  ·  {vol_ico} Vol {vol_r:.1f}x  ·  {macd_ico} MACD {d['macd_e']}\n"
    b1 += f"📐 ATR {atr_pct:.1f}%  ·  {d.get('entorno_txt','')}\n"
    b1 += f"🕯 {esc(d.get('vela_patron','Sin patrón'))}\n\n"
    b1 += f"<b>💰 Posición</b>\n"
    b1 += f"{pos['acc']} acc · ${pos['tot']:.0f} · Riesgo ${pos['perd']:.0f}\n"
    b1 += f"🛑 Stop ${pos['stop']:.2f}\n"
    b1 += f"🎯 T1 ${pos['t1']:.2f} (+{((pos['t1']/d['precio'])-1)*100:.1f}%) · 40%\n"
    b1 += f"🎯 T2 ${pos['t2']:.2f} (+{((pos['t2']/d['precio'])-1)*100:.1f}%) · 35%\n"
    b1 += f"🏃 T3 ${pos['t3']:.2f} trailing EMA8 · 25%\n"
    b1 += f"R/R real: <b>{pos['rr']:.1f}x</b>  ·  ⏱ Time-stop 7 días\n\n"
    b1 += f"{ia_ico} <b>IA {ia['prob']}% → {esc(ia_senal_txt)}</b>\n"
    if ia_alerta:
        b1 += f"👁 Vigilar: {esc(ia_alerta)}\n"
    if noticia_av:
        b1 += f"\n📰 {esc(noticia_av)}\n"
    b1 += f"\n<i>Sirio v8 · Solares 🌟</i>"
    # ── BLOQUE 2 — solo si score >= 75 o IA=ENTRAR ───────────
    enviar_bloque2 = score >= 75 or "ENTRAR" in ia_senal_txt.upper()
    bloque2 = None
    if enviar_bloque2:
        p1, p2, p3 = calc_probabilidades(a["fan"], d["adx"], rsi_v, vol_r)
        acc = pos["acc"]
        s40 = max(1, round(acc*0.40))
        s35 = max(1, round(acc*0.35))
        s25 = max(0, acc - s40 - s35)
        ctx   = esc(ia.get("contexto",""))
        razon = esc(ia.get("razon",""))
        b2  = f"📋 <b>{d['ticker']} · Detalle completo</b>\n"
        b2 += f"🕐 {hora}\n\n"
        if sent_texto:
            b2 += f"<b>📰 Sentimiento</b>\n{sent_texto}\n\n"
        b2 += f"<b>Tendencia semanal</b>\n{tendencia_semanal}\n\n"
        b2 += f"<b>Estructura precio</b>\n{d.get('hh_hl_txt','')}\n"
        if d.get('gap_ok'):
            b2 += f"📊 {esc(d.get('gap_txt',''))}\n"
        b2 += f"\n<b>Probabilidades</b>\nT1: {p1}%  ·  T2: {p2}%  ·  T3: {p3}%\n\n"
        b2 += f"<b>Gestión de salida</b>\n"
        b2 += f"40% ({s40} acc) → T1 ${pos['t1']:.2f} → stop a costo\n"
        b2 += f"35% ({s35} acc) → T2 ${pos['t2']:.2f}\n"
        b2 += f"25% ({s25} acc) → trailing EMA8\n\n"
        if ctx:
            b2 += f"<b>IA contexto</b>\n🌍 {ctx}\n📐 {razon}\n\n"
        b2 += f"<i>Sirio v8 · Solares 🌟</i>"
        bloque2 = b2
    return b1, bloque2

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

        f"<i>Mensaje completo ya fue enviado hoy — este es el seguimiento.\n"
        f"Sirio v8 — Solares</i> 🌟"
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
            + f"\n\n<i>Sirio v8 — Solares</i> 🌟"
        )
        enviados.append(clave)
        estado["resumen_4h"] = enviados
        guardar_estado(estado)
        print(f"  [4H] {clave} — {len(activas)} señal(es) activa(s), alerta enviada.")



def main():
    print(f"\n{'='*55}")
    print(f"  SISTEMA SIRIO — Solares v7.3 {hora_et()}")
    print(f"{'='*55}\n")

    estado  = cargar_estado()
    ya      = estado.get("alertados", [])
    count   = estado.get("count", 0)

    # v8: sector momentum al inicio del run
    sectores_activos = {}
    try:
        sectores_activos = analizar_sector_etfs()
    except Exception as ex:
        print(f"  [SECTORES] Error: {ex}")

    # v9: regimen de mercado — UNA sola vez por corrida (no por ticker, para no
    # multiplicar llamadas a yfinance sobre ~900 tickers x 7 corridas/dia).
    regimen_hoy = None
    try:
        df_spx = yf.download("^GSPC", period="1y", interval="1d", progress=False)
        if not df_spx.empty:
            resultado_regimen = detectar_regimen(df_spx)
            regimen_hoy = resultado_regimen.regimen
            print(f"  [REGIMEN] {regimen_hoy} — {resultado_regimen.razon}")
    except Exception as ex:
        print(f"  [REGIMEN] Error (no bloqueante): {ex}")

    # v8: fondito monitor (1 vez al día, primer run)
    if not estado.get("fondito_chequeado_hoy", False):
        try:
            msg_fondito = fondito_monitor()
            if msg_fondito:
                ok_f = send_telegram(msg_fondito)
                if ok_f:
                    estado["fondito_chequeado_hoy"] = True
                    guardar_estado(estado)
        except Exception as ex:
            print(f"  [FONDITO] Error: {ex}")

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
                f"<i>Sirio v8 — Solares</i> 🌟"
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

        # ── PASO 1 OBLIGATORIO: precio SOBRE SMA200 — veto absoluto ──────
        # Regla Lu: sin esta condición NO hay entrada. Gate previo a cualquier score.
        if d.get("sma200") and d["precio"] < d["sma200"]:
            print(f"    skip: precio {d['precio']:.2f} < SMA200 {d['sma200']:.2f} — Paso 1 violado")
            razones["sma200"] = razones.get("sma200", 0) + 1; continue

        # ── Filtros técnicos ──
        # v9: fan_minimo_observar ahora LEE de CONFIG en vez de estar hardcoded.
        # Antes: CONFIG decia min_fan_to_alert=4 pero esta linea tenia un "3" fijo
        # sin relacion — dos fuentes de verdad distintas. Ahora hay una sola.
        fan_minimo_observar = CONFIG["min_fan_to_alert"]
        if a["fan"] < fan_minimo_observar or not a["en_rango"] or not a["vol_ok"]:
            razones["fan"] = razones.get("fan", 0) + 1; continue
        # Compensacion por fan incompleto: a menor fan, mayor score exigido.
        # v9: se agrega el escalon fan=2 (antes solo existia fan=3 con +5 de exigencia).
        if a["fan"] == 2:      score_minimo_efectivo = 75
        elif a["fan"] == 3:    score_minimo_efectivo = 70
        else:                  score_minimo_efectivo = CONFIG["score_minimo"]  # fan=4, estandar 65
        es_fan3 = (a["fan"] == 3)  # se mantiene por compatibilidad con el resto del archivo
        # Volumen ratio mínimo 0.7x
        if a["vol_r"] < 0.7:
            print(f"    skip: vol_r {a['vol_r']:.1f}x < 0.7x mínimo")
            razones["vol_bajo"] = razones.get("vol_bajo", 0) + 1; continue
        # RSI sobrecomprado extremo
        rsi_v_check = d.get("rsi", 0) or 0
        if rsi_v_check > 80:
            print(f"    skip: RSI {rsi_v_check:.0f} > 80 sobrecomprado extremo")
            razones["rsi_extremo"] = razones.get("rsi_extremo", 0) + 1; continue
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
            if dist_ath_pct < 10.0:   # incluye negativo (en ATH o por encima) — v7.3: bajado de 20% a 10%
                print(f"    skip: ATH 52s — {dist_ath_pct:.1f}% del maximo 52s "
                      f"({ath_v:.2f}) — necesita >=20% de espacio")
                razones["cerca_ath"] = razones.get("cerca_ath", 0) + 1
                continue

        # ── Verificar zona de entrada ─────────────────────────
        largo_enviado = estado.get("largo_enviado", [])
        es_repetido   = ticker in largo_enviado

        sma8v    = d.get("sma8") or 0
        dist_s8  = ((d["precio"] - sma8v) / sma8v * 100) if sma8v > 0 else 0

        if dist_s8 > 3.5:
            print(f"    skip señal: extendida {dist_s8:.1f}% sobre SMA8 (max 3.5%) — sin entrada")
            razones["extendida"] = razones.get("extendida", 0) + 1
            continue

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
        pos = posicion(d["precio"], d.get("ath_52w"), d.get("atr"), d.get("beta"))
        ia  = analizar_ia(d, a, pos, sc_sent, tend_sem, noticias_yh)
        print(f"     IA: {ia['prob']}% — {ia['senal']}")

        # v8: obtener noticia Alpha Vantage (solo para señales confirmadas)
        noticia_av = _av_noticia(ticker) if AV_KEY else None
        # v8: boost sector
        s_boost = boost_sector(ticker, sectores_activos) if sectores_activos else 0
        a["score"] = min(100, a["score"] + s_boost)
        if s_boost > 0:
            a["score_det"] += f"+Sector:{s_boost}"
        tema_ticker = detectar_tema(ticker)

        if es_repetido:
            msg_principal = build_msg_corto(d, a, pos, ia, tend_sem, dist_s8)
            ok = send_telegram(msg_principal)
        elif es_fan3:
            bloque1, bloque2 = build_msg(d, a, pos, ia, txt_sent, tend_sem, noticia_av, s_boost, tema_ticker)
            aviso = "\U0001f52d <b>[OBSERVAR — Fan 3/4]</b> Estructura incompleta. Confirmar SMA faltante.\n\n"
            ok = send_telegram(aviso + bloque1)
            if ok and bloque2:
                time.sleep(1)
                send_telegram(bloque2)
        else:
            bloque1, bloque2 = build_msg(d, a, pos, ia, txt_sent, tend_sem, noticia_av, s_boost, tema_ticker)
            # v9: composite score de 5 factores — SOLO para señales nuevas Fan 4/4
            # confirmadas (aca, no en el loop completo de ~900 tickers) para no
            # multiplicar llamadas a yfinance. Es INFORMATIVO — se muestra pero
            # no bloquea el envio, para no arriesgar el bot en produccion con un
            # reemplazo total del filtro sin mas corridas reales de prueba primero.
            try:
                sector_etf = get_sector_etf(d.get("sector", ""))
                hist_sector = yf.Ticker(sector_etf).history(period="1y", interval="1d")
                hist_ticker = yf.Ticker(ticker).history(period="1y", interval="1d")
                info_ticker = yf.Ticker(ticker).info
                dist_sma50_pct = ((d["precio"] - d["sma50"]) / d["sma50"] * 100) if d.get("sma50") else 0
                sc = None
                if not hist_ticker.empty and len(hist_ticker) >= 50:
                    sc = composite_score(
                        close=hist_ticker["Close"], high=hist_ticker["High"], low=hist_ticker["Low"],
                        rsi_actual=d.get("rsi") or 50, dist_sma50_pct=dist_sma50_pct,
                        volumen_ratio=a.get("vol_r", 1.0), df_spx=df_spx,
                        close_sector=hist_sector["Close"], info_yf=info_ticker,
                    )
                if sc:
                    bloque1 = (f"🧮 <b>Composite:</b> {sc.total:.0f}/100 "
                               f"(Téc:{sc.factores['tecnico']:.0f} Mac:{sc.factores['macro']:.0f} "
                               f"Sec:{sc.factores['sector']:.0f} Fun:{sc.factores['fundamental']:.0f} "
                               f"Geo:{sc.factores['geopolitico']:.0f})\n"
                               + (f"🌡️ <b>Régimen:</b> {regimen_hoy}\n\n" if regimen_hoy else "\n")
                               + bloque1)
            except Exception as ex:
                print(f"    [COMPOSITE] Error (no bloqueante, se envia sin el bloque): {ex}")
            ok = send_telegram(bloque1)
            if ok and bloque2:
                time.sleep(1)
                send_telegram(bloque2)

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

    # v8: detectar clusters temáticos
    if nuevas > 0:
        clusters = detectar_clusters(estado.get("alertados", []))
        for tema_c, tickers_c in clusters:
            msg_cl = (
                f"🎯 <b>CLUSTER TEMÁTICO — {tema_c.upper()}</b>\n"
                f"🕐 {hora_et()}\n\n"
                f"{len(tickers_c)} señales hoy en el mismo tema:\n"
                + "\n".join(f"  · <b>{t}</b>" for t in tickers_c)
                + f"\n\n<i>Rotación sectorial detectada — Sirio v8 🌟</i>"
            )
            send_telegram(msg_cl)

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
            f"⚙️ ~900 tickers · score ≥65 · ATH ≥10% · precio>SMA200 · fan 3-4/4\n"
            f"📊 VIX: <b>{vix_str_hb}</b>\n\n"
            f"<i>Señal nueva → aviso inmediato.</i>\n"
            f"<i>Sin señales → status cada 2h + cierre 3:30pm.</i>\n\n"
            f"<i>Sirio v8 — Solares</i> 🌟"
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
        mapa = {"fan":"Fan 4/4","sma200":"Precio<SMA200","score":"Score<65","earnings":"Earnings",
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

# ═══════════════════════════════════════════════════════════════
#  v8.0 — NUEVAS FUNCIONES
# ═══════════════════════════════════════════════════════════════

# ── ALPHA VANTAGE — noticias contextuales en español ─────────
def _av_noticia(ticker):
    """
    Busca UNA noticia relevante de hoy vía Alpha Vantage.
    Solo aparece si: relevancia >= 0.6, sentimiento bullish, noticia de hoy/ayer.
    Retorna string en español listo para el mensaje, o None si no hay nada útil.
    """
    if not AV_KEY:
        return None
    try:
        url = (f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
               f"&tickers={ticker}&limit=5&apikey={AV_KEY}")
        r = requests.get(url, timeout=10)
        if r.status_code != 200: return None
        feed = r.json().get("feed", [])
        if not feed: return None
        hoy = date.today()
        ayer = hoy - timedelta(days=1)
        for item in feed:
            # Solo noticias de hoy o ayer
            try:
                fecha_noticia = datetime.strptime(item["time_published"][:8], "%Y%m%d").date()
                if fecha_noticia < ayer: continue
            except: continue
            # Buscar el ticker específico en ticker_sentiment
            for ts in item.get("ticker_sentiment", []):
                if ts.get("ticker","").upper() != ticker.upper(): continue
                rel = float(ts.get("relevance_score", 0))
                sent = float(ts.get("ticker_sentiment_score", 0))
                if rel < 0.6 or sent < 0.15: continue  # irrelevante o neutral
                titulo = item.get("title", "")[:120]
                pct_bull = int((sent + 1) / 2 * 100)
                return f"📰 {titulo} — alcista {pct_bull}%"
        return None
    except:
        return None


# ── SECTOR MOMENTUM — chequea ETFs de referencia ─────────────
SECTOR_MAP = {
    "XLE":  ["OXY","CVX","XOM","COP","DVN","HAL","SLB","MRO","FANG","EOG"],
    "GDX":  ["NEM","AEM","GOLD","WPM","KGC","AGI","PAAS","AG"],
    "XLB":  ["FCX","NUE","STLD","CLF","RS","AA","MP","ALB","SQM"],
    "XLI":  ["CAT","DE","GE","HON","LMT","RTX","NOC","GD","KTOS"],
    "XLV":  ["HIMS","ABBV","MRK","AMGN","GILD","EW","UNH"],
    "XLF":  ["JPM","BAC","GS","MS","BX","KKR"],
    "XLY":  ["MGM","WYNN","DAL","UAL","NKE","F","GM"],
    "ARKK": ["RKLB","ASTS","COIN","MSTR","HOOD"],
    "COPX": ["FCX","SCCO","HBM","TECK"],
    "LIT":  ["ALB","SQM","LAC","PLL"],
    "REMX": ["MP","UAMY"],
    "URA":  ["CCJ","UEC","DNN","LEU"],
}

TEMA_MAP = {
    "espacial":  ["RKLB","ASTS","LUNR","MNTS","BWXT","RDW"],
    "nuclear":   ["SMR","CCJ","NNE","UEC","DNN","LEU","UUUU","OKLO"],
    "cripto":    ["MARA","RIOT","CLSK","COIN","MSTR","HOOD","BTBT"],
    "defensa":   ["KTOS","AXON","LMT","RTX","NOC","GD","BWXT","HII"],
    "glp1":      ["HIMS","VKTX","CELH","CAVA"],
    "semis_ai":  ["MU","AMD","INTC","AMAT","KLAC","LRCX","ON","ENTG","AMKR","UCTT"],
    "oro_plata": ["NEM","AEM","WPM","GOLD","PAAS","AG","KGC"],
    "energia":   ["OXY","CVX","XOM","COP","DVN","SLB","HAL","BTU"],
    "homebuilders": ["DHI","LEN","PHM","TOL","KBH","MDC","NVR","SKY"],
}

def analizar_sector_etfs():
    """
    Chequea los ETFs de referencia al inicio del run.
    Retorna dict: {etf: {"fan":N, "activo":bool, "pct":X}}
    ETF con fan >= 3 = sector activo → boost score +8 a sus miembros.
    """
    resultado = {}
    etfs_check = list(SECTOR_MAP.keys()) + ["SPY","QQQ"]
    for etf in etfs_check:
        try:
            h = yf.Ticker(etf).history(period="60d", interval="1d")
            if h.empty or len(h) < 30: continue
            c = h["Close"]
            p = float(c.iloc[-1])
            s20 = float(c.iloc[-20:].mean())
            s50 = float(c.iloc[-50:].mean())
            s200 = float(c.iloc[-min(200,len(c)):].mean())
            fan = sum([p > s200, s50 > s200, s20 > s50, p > s20])
            pct = round((p - float(c.iloc[-2])) / float(c.iloc[-2]) * 100, 2)
            resultado[etf] = {"fan": fan, "activo": fan >= 3, "pct": pct, "precio": round(p,2)}
        except:
            continue
    activos = [e for e, v in resultado.items() if v["activo"]]
    print(f"  [SECTORES] {len(activos)} activos: {', '.join(activos[:6])}")
    return resultado


def detectar_tema(ticker):
    """Retorna el nombre del tema al que pertenece un ticker, o None."""
    for tema, lista in TEMA_MAP.items():
        if ticker in lista:
            return tema
    return None


def detectar_clusters(alertados_hoy):
    """
    Si 3+ tickers del mismo tema señalaron hoy → alerta especial de cluster.
    Retorna lista de (tema, tickers) con clusters activos.
    """
    conteo = {}
    for t in alertados_hoy:
        tema = detectar_tema(t)
        if tema:
            conteo.setdefault(tema, []).append(t)
    return [(tema, tickers) for tema, tickers in conteo.items() if len(tickers) >= 3]


def boost_sector(ticker, sectores_activos):
    """
    +8 puntos si el sector del ticker está activo (ETF fan >= 3).
    +5 puntos si el tema está en TEMA_MAP y algún ETF correlacionado activo.
    """
    pts = 0
    for etf, miembros in SECTOR_MAP.items():
        if ticker in miembros:
            info = sectores_activos.get(etf, {})
            if info.get("activo"):
                pts += 8
                break
    tema = detectar_tema(ticker)
    if tema and pts == 0:
        pts += 3  # bonus leve por pertenencia a tema aunque el ETF no esté
    return pts


# ── TICKERS NUEVOS v8 — universo expandido ───────────────────
TICKERS_NUEVOS_V8 = [
    # ── ESPACIAL / SpaceX proxy ────────────────────────────────
    "RKLB","ASTS","LUNR","MNTS","RDW","BWXT","KTOS","AXON",
    # ── NUCLEAR / Uranio ──────────────────────────────────────
    "CCJ","NNE","UEC","DNN","LEU","UUUU","OKLO","VST","CEG","SMR",
    # ── HOMEBUILDERS ──────────────────────────────────────────
    "DHI","LEN","PHM","TOL","KBH","MDC","NVR","SKY","MHO","TMHC",
    # ── AI INFRAESTRUCTURA ────────────────────────────────────
    "VRT","CDNS","ANET","SMCI","COHR","BE","POWL",
    # ── DEFENSA MODERNA ───────────────────────────────────────
    "AXON","LDOS","CACI","BAH","SAIC","TDG","HWM","CW",
    # ── GLP-1 / VIDA NUEVA ────────────────────────────────────
    "VKTX","RVNC","ZFOX","HIMS",
    # ── CRIPTO INFRAESTRUCTURA ────────────────────────────────
    "HOOD","MSTR","SMLR","BTBT","HUT","CLSK",
    # ── MATERIAS PRIMAS / AGUA ────────────────────────────────
    "AWK","WTRG","XYL","GFL","CLH",
    # ── CONSUMO RESILIENTE ────────────────────────────────────
    "CAVA","BROS","SG","WING","TXRH",
    # ── ENERGÍA ALTERNATIVA ───────────────────────────────────
    "FSLR","ENPH","RUN","BE","PLUG",
]


# ── FONDITO MONITOR ──────────────────────────────────────────
# v9: corregido — tenia solo cantidades de T1 (04-may). Ahora incluye T2 (07-jul)
# con costo promedio ponderado real. Verificar contra el estado de cuenta IBKR
# si se ejecuta una T3 antes de que esto se vuelva a actualizar.
FONDITO_ACTIVAS = {
    "IAU":  {"entrada": 81.765,  "shares": 4,      "tipo": "oro"},      # T1 2@85.03 + T2 2@78.50
    "SLV":  {"entrada": 60.7875, "shares": 4,      "tipo": "plata"},    # T1 2@65.935 + T2 2@55.64
    "XLE":  {"entrada": 57.133,  "shares": 5,      "tipo": "energia"},  # T1 3@59.235 + T2 2@53.98
    "SGOV": {"entrada": 100.4325,"shares": 1.9958, "tipo": "cash"},     # T1 0.9958@100.417 + T2 1@100.45
}

FONDITO_PENDIENTES = {
    "HODL": {"cond": "entrar_ya",   "presupuesto": 1170, "nota": "Clarity Act aprobado"},
    "URA":  {"cond": "entrar_ya",   "presupuesto": 750,  "nota": "Nuclear ancla v8"},
    "SMR":  {"cond": "entrar_ya",   "presupuesto": 290,  "nota": "Especulativo pequeño"},
    "ASTS": {"cond": "precio_max",  "precio_max": 55,    "presupuesto": 520,  "nota": "SpaceX proxy"},
    "UCTT": {"cond": "precio_max",  "precio_max": 65,    "presupuesto": 520,  "nota": "Semis backend"},
    "AMKR": {"cond": "precio_max",  "precio_max": 63,    "presupuesto": 520,  "nota": "Semis backend"},
    "REMX": {"cond": "precio_rsi",  "precio_max": 95, "rsi_max": 65, "presupuesto": 0, "nota": "Watchlist tierras raras"},
    "CAVA": {"cond": "sobre_sma50", "presupuesto": 390,  "nota": "GLP-1 estilo vida"},
    "CELH": {"cond": "entrar_jun",  "presupuesto": 390,  "nota": "GLP-1 energéticas"},
    "KTOS": {"cond": "entrar_jun",  "presupuesto": 1170, "nota": "Defensa geopolítica"},
    "MP":   {"cond": "entrar_jun",  "presupuesto": 650,  "nota": "Tierras raras"},
    "AYA":  {"cond": "entrar_jun",  "presupuesto": 650,  "nota": "Plata Marruecos"},
    "ONEQ": {"cond": "julio",       "presupuesto": 1300, "nota": "Tech broad DCA fijo"},
}

def fondito_monitor():
    """
    Monitoreo del portafolio pasivo Fondito.
    1. Posiciones ACTIVAS: alerta si RSI semanal > 75 (toma parcial 25%)
    2. Pendientes PRECIO: alerta si alcanzaron condición de entrada
    3. Retorna mensaje Telegram o None si no hay nada urgente.
    """
    alertas = []
    print("\n  [FONDITO] Revisando posiciones...")

    # ── Activas — RSI semanal ──────────────────────────────────
    for ticker, info in FONDITO_ACTIVAS.items():
        if ticker == "SGOV": continue  # cash, skip
        try:
            h = yf.Ticker(ticker).history(period="1y", interval="1wk")
            if h.empty or len(h) < 14: continue
            c = h["Close"]
            precio = float(c.iloc[-1])
            rsi_w = calc_rsi(c, 14)
            entrada = info["entrada"]
            pct_desde = round((precio - entrada) / entrada * 100, 1)
            print(f"    {ticker}: ${precio:.2f} | RSI_w={rsi_w:.0f if rsi_w else 'N/A'} | {pct_desde:+.1f}% desde entrada")
            if rsi_w and rsi_w > 75:
                alertas.append(
                    f"⚠️ <b>{ticker}</b> — RSI semanal {rsi_w:.0f} &gt; 75\n"
                    f"   💲 ${precio:.2f} ({pct_desde:+.1f}% desde entrada ${entrada})\n"
                    f"   → Regla: toma parcial 25% ({info['shares']*0.25:.2f} acc)\n"
                    f"   → Reinvertir en próximo tranche"
                )
        except Exception as ex:
            print(f"    {ticker} error: {ex}")

    # ── Pendientes — condición de precio ─────────────────────
    for ticker, info in FONDITO_PENDIENTES.items():
        cond = info.get("cond","")
        if cond not in ("precio_max","precio_rsi","sobre_sma50","entrar_ya"):
            continue
        try:
            fi = yf.Ticker(ticker).fast_info
            precio = float(getattr(fi, "last_price", None) or 0)
            if precio <= 0: continue
            nota = info.get("nota","")
            presup = info.get("presupuesto", 0)
            shares_aprox = int(presup / precio) if precio > 0 else 0

            if cond == "entrar_ya":
                rsi_v = calc_rsi(yf.Ticker(ticker).history(period="60d")["Close"])
                rsi_txt = f"RSI {rsi_v:.0f}" if rsi_v else ""
                alertas.append(
                    f"🟢 <b>{ticker}</b> — ENTRADA PENDIENTE\n"
                    f"   💲 ${precio:.2f} | {rsi_txt} | {nota}\n"
                    f"   → ~{shares_aprox} acciones | ${presup} presupuesto"
                )
            elif cond == "precio_max":
                precio_max = info.get("precio_max", 9999)
                if precio <= precio_max:
                    alertas.append(
                        f"✅ <b>{ticker}</b> — CONDICIÓN ALCANZADA\n"
                        f"   💲 ${precio:.2f} ≤ ${precio_max} ✅ | {nota}\n"
                        f"   → ~{shares_aprox} acciones | ${presup} presupuesto"
                    )
            elif cond == "precio_rsi":
                precio_max = info.get("precio_max", 9999)
                rsi_max_f = info.get("rsi_max", 65)
                hist_r = yf.Ticker(ticker).history(period="60d")
                rsi_v = calc_rsi(hist_r["Close"]) if not hist_r.empty else None
                if precio <= precio_max and rsi_v and rsi_v <= rsi_max_f:
                    alertas.append(
                        f"✅ <b>{ticker}</b> — WATCHLIST ACTIVADA\n"
                        f"   💲 ${precio:.2f} ≤ ${precio_max} ✅ | RSI {rsi_v:.0f} ≤ {rsi_max_f} ✅\n"
                        f"   → {nota}"
                    )
            elif cond == "sobre_sma50":
                hist_r = yf.Ticker(ticker).history(period="60d")
                if not hist_r.empty:
                    sma50_v = float(hist_r["Close"].iloc[-50:].mean())
                    if precio >= sma50_v:
                        alertas.append(
                            f"✅ <b>{ticker}</b> — SOBRE SMA50\n"
                            f"   💲 ${precio:.2f} ≥ SMA50 ${sma50_v:.2f} ✅ | {nota}\n"
                            f"   → ~{shares_aprox} acciones | ${presup} presupuesto"
                        )
        except Exception as ex:
            print(f"    {ticker} fondito error: {ex}")

    ciclo_txt = ""
    if CICLO_MOSTRAR_EN_BOT:
        dias_c, fase_c, nota_c = fase_ciclo_hoy()
        ciclo_txt = f"\n{fase_c} (día {dias_c}) — {nota_c}\n"

    astro_txt = resumen_astrologico_hoy() if TRANSITOS_MOSTRAR_EN_BOT else ""

    if not alertas and not ciclo_txt and not astro_txt:
        print("  [FONDITO] Sin alertas activas")
        return None

    cuerpo_alertas = ("\n\n".join(alertas) + "\n\n") if alertas else "Sin alertas de precio/RSI hoy.\n\n"
    msg = (
        f"💼 <b>FONDITO — SOLARES</b>\n"
        f"🕐 {hora_et()}\n{ciclo_txt}{astro_txt}\n"
        + cuerpo_alertas
        + f"<i>Sin stop loss — DCA por tiempo\nSolares Fondito v8 🌟</i>"
    )
    return msg

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
                f"<i>Sirio v8 — Solares</i>"
            )
        except Exception:
            pass
