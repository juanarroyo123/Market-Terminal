#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market Intelligence Agent — Bloomberg Style
Uso: python3 server.py
Abre: http://localhost:5000
"""
import gc, json, os, re, subprocess, sys, threading, time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

def _pip(*pkgs):
    subprocess.check_call([sys.executable,"-m","pip","install",*pkgs,"-q"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def ensure_deps():
    import importlib.util
    needed=[("yfinance","yfinance"),("feedparser","feedparser"),
            ("requests","requests"),("beautifulsoup4","bs4"),("schedule","schedule")]
    missing=[p for p,m in needed if not importlib.util.find_spec(m)]
    if missing:
        print(f"Instalando: {', '.join(missing)} ...")
        _pip(*missing); print("Listo.\n")
ensure_deps()

import feedparser, requests, yfinance as yf
from bs4 import BeautifulSoup

# ── Traducción con Google Translate (gratis, sin API key) ────────────────────
_tc={}  # cache titulo_en[:80] -> titulo_es

def traducir_uno(txt):
    """Traduce un texto EN->ES usando Google Translate sin API key."""
    if not txt or len(txt)<8: return txt
    k=txt[:80]
    if k in _tc: return _tc[k]
    try:
        url="https://translate.googleapis.com/translate_a/single"
        params={"client":"gtx","sl":"en","tl":"es","dt":"t","q":txt[:500]}
        r=requests.get(url,params=params,timeout=8,
                       headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code==200:
            data=r.json()
            es="".join(part[0] for part in data[0] if part[0])
            if es and len(es)>4:
                _tc[k]=es; return es
    except: pass
    return txt

def traducir_batch(titulos):
    """Traduce en paralelo una lista de titulares."""
    if not titulos: return {}
    result={}
    por_traducir=[t for t in titulos if t[:80] not in _tc]
    # Servidos desde caché
    for t in titulos:
        if t[:80] in _tc: result[t]=_tc[t[:80]]
    if not por_traducir: return result
    # Traducir en paralelo (máx 8 threads)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures={ex.submit(traducir_uno,t):t for t in por_traducir}
        for f,orig in futures.items():
            try: result[orig]=f.result(timeout=10)
            except: result[orig]=orig
    return result

def traducir(txt):
    if not txt or len(txt)<8: return txt
    return traducir_uno(txt)

# ── Caché ─────────────────────────────────────────────────────────────────────
CACHE={"market":{},"fear_greed":{},"put_call":{},"calendar":[],
       "earnings":[],"options":[],"news":[],"social":[],
       "technicals":{},"breadth":{},"sectors":{},"last_update":None}
LOCK=threading.Lock()

# ── Tickers ───────────────────────────────────────────────────────────────────
TICKERS={
    "S&P 500":"^GSPC","Nasdaq 100":"^NDX","Dow Jones":"^DJI","Russell 2K":"^RUT",
    "VIX":"^VIX","VIX9D":"^VIX9D",
    "WTI Oil":"CL=F","Brent":"BZ=F","Oro":"GC=F","Plata":"SI=F","Cobre":"HG=F",
    "Nat. Gas":"NG=F",
    "DXY":"DX-Y.NYB","EUR/USD":"EURUSD=X","USD/JPY":"JPY=X","GBP/USD":"GBPUSD=X",
    "Bono 10Y":"^TNX","Bono 2Y":"^IRX","Bono 30Y":"^TYX",
    "Bitcoin":"BTC-USD","Ethereum":"ETH-USD",
}
TOP_SP500={
    "NVIDIA":"NVDA","Apple":"AAPL","Microsoft":"MSFT","Amazon":"AMZN",
    "Meta":"META","Alphabet":"GOOGL","Tesla":"TSLA","Broadcom":"AVGO",
    "JPMorgan":"JPM","AMD":"AMD","Netflix":"NFLX","Eli Lilly":"LLY",
    "Visa":"V","Walmart":"WMT","Exxon":"XOM",
    "UnitedHealth":"UNH","J&J":"JNJ","Mastercard":"MA","P&G":"PG",
    "Home Depot":"HD","Chevron":"CVX","Merck":"MRK","AbbVie":"ABBV",
    "BofA":"BAC","Costco":"COST","Oracle":"ORCL","Goldman":"GS",
    "Palantir":"PLTR","Salesforce":"CRM","Cisco":"CSCO",
}
KW_T1=["fed","federal reserve","fomc","powell","rate hike","rate cut",
       "crash","cpi","inflation","nonfarm","gdp","recession","default",
       "bankruptcy","war","nuclear","sanction","opec","tariff","trade war",
       "debt ceiling","ipc","recesión","guerra","aranceles","trump","china",
       "iran","russia","ukraine","interest rate","rate decision","jobs report"]
KW_T2=["earnings","guidance","buyback","dividend","merger","acquisition",
       "ipo","sec","fraud","layoffs","yield curve","treasury","oil","gold",
       "nasdaq","s&p","vix","israel","resultados","beneficios","fusión",
       "despidos","bonos","inflación","results","profit","revenue","beats",
       "misses","outlook","forecast","downgrade","upgrade","target","rally",
       "selloff","plunge","surge","soars","tumbles","drops","jumps",
       "nvidia","apple","microsoft","amazon","meta","google","alphabet",
       "tesla","nvidia","broadcom","jpmorgan","amd","netflix","eli lilly",
       "visa","walmart","exxon","unitedhealth","johnson","mastercard",
       "goldman","morgan stanley","boeing","palantir","openai","anthropic",
       "nvda","aapl","msft","amzn","googl","tsla","avgo","jpm","meta"]

# Qué afecta cada keyword al mercado
MARKET_IMPACT={
    "fed":"Fed/Tipos","fomc":"Fed/Tipos","powell":"Fed/Tipos",
    "rate hike":"Tipos↑ Bonos↓","rate cut":"Tipos↓ Bonos↑",
    "cpi":"Inflación/Fed","inflation":"Inflación/Fed",
    "nonfarm":"Empleo/Fed","gdp":"Macro/Crecimiento",
    "recession":"Riesgo Macro","war":"Risk-off/Oil↑",
    "nuclear":"Risk-off extremo","sanction":"FX/Commodities",
    "opec":"WTI/Brent","tariff":"FX/Exportadoras",
    "trade war":"FX/Exportadoras","earnings":"Acción directa",
    "merger":"Acción directa","ipo":"Sector","sec":"Acción directa",
    "iran":"Oil↑ Risk-off","russia":"Oil/Gas↑","china":"FX/Tech",
    "ukraine":"Gas↑ Risk-off","oil":"WTI/Brent","gold":"Oro/Risk-off",
}

def score_text(t,b=""):
    txt=(t+" "+b).lower()
    return min(sum(3 for k in KW_T1 if k in txt)+sum(1 for k in KW_T2 if k in txt),10)

def get_impact_label(t,b=""):
    txt=(t+" "+b).lower()
    for k,v in MARKET_IMPACT.items():
        if k in txt: return v
    return "Mercado general"

# ── MERCADO ───────────────────────────────────────────────────────────────────
# Cache de cierres anteriores — se rellena una vez y se reutiliza
_PREV_CLOSES={}

def _load_prev_closes():
    """Descarga cierres diarios de 5d para tener el precio de cierre anterior."""
    global _PREV_CLOSES
    try:
        syms=list(TICKERS.values())  # Solo principales — SP500 consume demasiada RAM
        raw=yf.download(syms,period="5d",interval="1d",
                        progress=False,threads=False,auto_adjust=True)
        closes=raw["Close"] if "Close" in raw else raw
        for sym in syms:
            try:
                s=closes[sym].dropna()
                if len(s)>=2:
                    _PREV_CLOSES[sym]=float(s.iloc[-2])  # cierre de ayer
            except: pass
    except Exception as e:
        print(f"Error prev closes: {e}")

def fetch_market():
    """Precio en tiempo real (5m) + % vs cierre anterior (1d)."""
    data={}
    try:
        # Tickers principales — precio live con intervalo de 5 minutos
        main_syms=list(TICKERS.values())
        raw_live=yf.download(main_syms,period="1d",interval="5m",
                             progress=False,threads=True,auto_adjust=True)
        live=raw_live["Close"] if "Close" in raw_live else raw_live

        for name,sym in TICKERS.items():
            try:
                s=live[sym].dropna() if sym in live else None
                if s is None or len(s)==0: continue
                curr=float(s.iloc[-1])
                prev=_PREV_CLOSES.get(sym, curr)
                data[name]={"price":curr,"prev":prev,
                            "change":round(curr-prev,4),
                            "pct":round(((curr-prev)/prev)*100,2) if prev else 0.0}
            except: pass
        del live; del raw_live; gc.collect()

        # Top S&P500 — datos diarios (suficiente para ranking)
        sp_syms=list(TOP_SP500.values())[:30]
        raw_sp=yf.download(sp_syms,period="2d",interval="1d",
                           progress=False,threads=False,auto_adjust=True)
        sp_closes=raw_sp["Close"] if "Close" in raw_sp else raw_sp
        data["_top_stocks"]={}
        for name,sym in TOP_SP500.items():
            try:
                s=sp_closes[sym].dropna()
                if len(s)<2: continue
                prev,curr=float(s.iloc[-2]),float(s.iloc[-1])
                pct=round(((curr-prev)/prev)*100,2)
                if abs(pct)>20: continue
                data["_top_stocks"][name]={"sym":sym,"price":round(curr,2),
                    "pct":pct,"change":round(curr-prev,2)}
            except: pass
    except Exception as e:
        print(f"Error mercado: {e}")
    return data


# ── INDICADORES TÉCNICOS ─────────────────────────────────────────────────────
def fetch_technicals():
    result = {}
    symbols = {
        'SPX': '^GSPC', 'NDX': '^NDX',
    }
    for name, sym in symbols.items():
        try:
            hist = yf.Ticker(sym).history(period='6mo', interval='1d', auto_adjust=True)
            if hist is None or len(hist) < 30: continue
            closes = hist['Close'].dropna()
            del hist
            curr = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            pct  = round((curr - prev) / prev * 100, 2)

            # RSI 14
            delta = closes.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta).clip(lower=0).rolling(14).mean()
            rs    = gain / loss.replace(0, 0.001)
            rsi   = round(float(100 - 100 / (1 + rs.iloc[-1])), 1)

            # MACD 12/26/9
            ema12  = closes.ewm(span=12, adjust=False).mean()
            ema26  = closes.ewm(span=26, adjust=False).mean()
            macd   = ema12 - ema26
            sig    = macd.ewm(span=9, adjust=False).mean()
            hist_m = macd - sig
            hval   = round(float(hist_m.iloc[-1]), 2)
            hprev  = round(float(hist_m.iloc[-2]), 2)
            macd_trend = 'BULL' if hval > 0 else 'BEAR'
            macd_cross = ('CRUCE ALCISTA' if hval > 0 and hprev <= 0 else
                          'CRUCE BAJISTA' if hval < 0 and hprev >= 0 else '')

            # Medias móviles
            ma20  = round(float(closes.rolling(20).mean().iloc[-1]), 2)
            ma50  = round(float(closes.rolling(50).mean().iloc[-1]), 2)
            ma200 = round(float(closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else closes.mean()), 2)

            result[name] = {
                'price': round(curr, 2), 'pct': pct,
                'rsi': rsi,
                'rsi_zone': ('SOBRECOMPRA' if rsi > 70 else 'SOBREVENTA' if rsi < 30 else 'NEUTRAL'),
                'macd': round(float(macd.iloc[-1]), 2),
                'macd_sig': round(float(sig.iloc[-1]), 2),
                'macd_hist': hval,
                'macd_trend': macd_trend,
                'macd_cross': macd_cross,
                'ma20': ma20, 'ma50': ma50, 'ma200': ma200,
                'above_ma20': curr > ma20,
                'above_ma50': curr > ma50,
                'above_ma200': curr > ma200,
                'golden_cross': ma50 > ma200,
            }
            del closes
            gc.collect()
        except Exception as e:
            print(f'Error tech {name}: {e}')
    return result

# ── AMPLITUD DE MERCADO ───────────────────────────────────────────────────────
def fetch_market_breadth():
    sp500_sample = list(TOP_SP500.values())
    try:
        advances=0; declines=0; unchanged=0
        above50=0; above200=0; total=0
        new_highs=0; new_lows=0

        for sym in sp500_sample:
            try:
                tk = yf.Ticker(sym)
                s_df = tk.history(period='3mo', interval='1d', auto_adjust=True)
                if s_df is None or len(s_df) < 2: continue
                s = s_df['Close'].dropna()
                del s_df
                if len(s) < 2: continue
                total += 1
                curr = float(s.iloc[-1])
                prev = float(s.iloc[-2])
                chg  = curr - prev
                if   chg > 0: advances  += 1
                elif chg < 0: declines  += 1
                else:         unchanged += 1
                if len(s) >= 50  and curr > float(s.rolling(50).mean().iloc[-1]):  above50  += 1
                if len(s) >= 200 and curr > float(s.rolling(200).mean().iloc[-1]): above200 += 1
                hi52 = float(s.tail(252).max())
                lo52 = float(s.tail(252).min())
                if curr >= hi52 * 0.995: new_highs += 1
                if curr <= lo52 * 1.005: new_lows   += 1
            except: pass

        ad_ratio = round(advances / max(declines,1), 2)
        return {
            'advances': advances, 'declines': declines,
            'unchanged': unchanged, 'total': total,
            'ad_ratio': ad_ratio,
            'pct_above50':  round(above50  / max(total,1) * 100, 1),
            'pct_above200': round(above200 / max(total,1) * 100, 1),
            'new_highs': new_highs, 'new_lows': new_lows,
            'signal': ('ALCISTA' if ad_ratio > 1.5 else
                       'BAJISTA' if ad_ratio < 0.67 else 'NEUTRAL'),
        }
    except Exception as e:
        print(f'Error breadth: {e}')
        return {}

# ── HEATMAP SECTORES ─────────────────────────────────────────────────────────
def fetch_sector_heatmap():
    etfs = {
        'Tecnologia': 'XLK', 'Salud': 'XLV', 'Financiero': 'XLF',
        'Cons.Discr': 'XLY', 'Industriales': 'XLI', 'Energia': 'XLE',
        'Cons.Basico': 'XLP', 'Utilities': 'XLU', 'Real Estate': 'XLRE',
        'Materiales': 'XLB', 'Comunic.': 'XLC',
    }
    result = {}
    for sec, sym in etfs.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period='5d', interval='1d', auto_adjust=True)
            if hist is None or len(hist) < 2: continue
            curr = float(hist['Close'].iloc[-1])
            prev = float(hist['Close'].iloc[-2])
            if curr and prev and prev > 0:
                result[sec] = round((curr-prev)/prev*100, 2)
        except Exception as e:
            print(f'Error sector {sec}: {e}')
    return result

def fetch_fear_greed():
    try:
        r=requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                       timeout=8,headers={"User-Agent":"Mozilla/5.0"})
        fg=r.json()["fear_and_greed"]
        return {"score":float(fg["score"]),"rating":fg["rating"],
                "prev":float(fg.get("previous_close",fg["score"]))}
    except: return {}

def fetch_put_call():
    try:
        r=requests.get("https://www.cboe.com/us/options/market_statistics/daily/",
                       timeout=10,headers={"User-Agent":"Mozilla/5.0"})
        soup=BeautifulSoup(r.text,"html.parser")
        result={}
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells=[td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells)<2: continue
                label=cells[0].lower()
                try:
                    val=float(cells[-1])
                    if "equity" in label and "put" in label: result["equity_pc"]=val
                    elif "total" in label and "put" in label: result["total_pc"]=val
                except: pass
        return result
    except: return {}

# ── CALENDARIO ────────────────────────────────────────────────────────────────
def _parse_ff_time(date_str):
    try:
        ds=date_str.replace("Z","+00:00")
        if "+" in ds[10:] or (len(ds)>19 and ds[19]=="-"):
            dt_utc=datetime.fromisoformat(ds)
            if dt_utc.tzinfo is None: dt_utc=dt_utc.replace(tzinfo=timezone.utc)
            return dt_utc.astimezone()
        return datetime.fromisoformat(ds)
    except: return None

def fetch_calendar():
    events=[]
    today=datetime.now()
    today_str=today.strftime("%Y-%m-%d")
    data=[]
    # Fuentes múltiples — ForexFactory + fallback
    sources = [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
        "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://cdn-nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]
    for cal_url in sources:
        try:
            r=requests.get(cal_url,timeout=15,
                          headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                   "Accept":"application/json,*/*","Referer":"https://www.forexfactory.com/"})
            if r.status_code==200 and len(r.text)>10:
                data.extend(r.json())
                print(f"Calendario OK: {cal_url} — {len(r.json())} eventos")
                break  # Con uno es suficiente
        except Exception as e:
            print(f"Error calendario {cal_url}: {e}")
    # Fallback: construir eventos mínimos desde fuentes alternativas
    if not data:
        print("Calendario: todas las fuentes fallaron")
    try:
        imp_map={"High":"Alto","Medium":"Medio","Low":"Bajo",
                 "Holiday":"Bajo","Non-Economic":"Bajo"}
        for item in data:
            if item.get("country","")!="USD": continue
            imp_raw=item.get("impact","Low")
            if imp_raw not in ("High",): continue  # Solo HIGH impact
            dt_local=_parse_ff_time(item.get("date",""))
            if dt_local is None: continue
            date_str=dt_local.strftime("%Y-%m-%d")
            time_str=dt_local.strftime("%H:%M")
            sort_ts=dt_local.timestamp()
            is_today=date_str==today_str
            try:
                is_past=dt_local<datetime.now().astimezone()
            except: is_past=False
            ev_name=item.get("title","").encode("ascii","replace").decode("ascii")
            ev_es=ev_name  # No traducir - evitar caracteres rotos
            # Qué impacta en mercado
            impact_lbl=get_impact_label(ev_name)
            events.append({"time":time_str,"event":ev_es,"event_en":ev_name,
                           "imp":imp_map.get(imp_raw,"Bajo"),"imp_raw":imp_raw,
                           "actual":item.get("actual",""),
                           "forecast":item.get("forecast",""),
                           "previous":item.get("previous",""),
                           "date":date_str,"is_today":is_today,
                           "is_past":is_past,"sort_ts":sort_ts,
                           "market_impact":impact_lbl})
    except Exception as e:
        print(f"Error calendario: {e}")
    seen=set(); unique=[]
    for e in events:
        k=f"{e['date']}_{e['time']}_{e['event'][:20]}"
        if k not in seen: seen.add(k); unique.append(e)
    unique.sort(key=lambda x:x["sort_ts"])
    return unique

def fetch_earnings():
    """
    Earnings enriquecido estilo Investing.com:
    - EPS estimado vs actual + beat/miss %
    - Revenue estimado vs actual + beat/miss %
    - Market cap, nombre empresa, hora (BMO/AMC)
    - Ventana: últimos 3 días + próximos 14 días
    - Fuente: yfinance .calendar + .info + .quarterly_financials
    """
    TOP_CAPS = [
        "NVDA","AAPL","MSFT","AMZN","GOOGL","META","TSLA","AVGO","JPM","LLY",
        "V","UNH","XOM","MA","HD","PG","COST","ABBV","MRK","CVX",
    ]

    today = datetime.now().date()
    window_start = today - timedelta(days=3)
    window_end   = today + timedelta(days=14)

    earnings = []
    seen = set()

    import concurrent.futures

    def _fmt_mcap(v):
        if v is None: return "—"
        try:
            v = float(v)
            if v >= 1e12: return f"${v/1e12:.1f}T"
            if v >= 1e9:  return f"${v/1e9:.0f}B"
            if v >= 1e6:  return f"${v/1e6:.0f}M"
        except: pass
        return "—"

    def _fmt_rev(v):
        if v is None: return None
        try:
            v = float(v)
            if v >= 1e12: return f"${v/1e12:.2f}T"
            if v >= 1e9:  return f"${v/1e9:.2f}B"
            if v >= 1e6:  return f"${v/1e6:.1f}M"
        except: pass
        return None

    def _beat_pct(actual, estimate):
        """Calcula % beat/miss. Retorna float o None."""
        try:
            a, e = float(actual), float(estimate)
            if e == 0: return None
            return round((a - e) / abs(e) * 100, 1)
        except:
            return None

    def _get_earnings(sym):
        try:
            tk   = yf.Ticker(sym)
            cal  = tk.calendar
            if cal is None:
                return None
            if hasattr(cal, 'to_dict'):
                cal = cal.to_dict()

            # ── Fecha de earnings ──────────────────────────────────────────
            earn_date = None
            for key in ['Earnings Date', 'earningsDate', 'Earnings_Date']:
                val = cal.get(key)
                if val is not None:
                    if hasattr(val, '__iter__') and not isinstance(val, str):
                        val = list(val)
                        if val: val = val[0]
                    try:
                        earn_date = val.date() if hasattr(val, 'date') else \
                                    datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
                    except: pass
                    break

            if earn_date is None or not (window_start <= earn_date <= window_end):
                return None

            # ── Info básica ────────────────────────────────────────────────
            info = {}
            try: info = tk.info or {}
            except: pass

            company  = info.get('shortName') or info.get('longName') or sym
            mcap_raw = info.get('marketCap')
            mcap     = _fmt_mcap(mcap_raw)

            # ── EPS estimado y actual ──────────────────────────────────────
            eps_est = None
            for key in ['EPS Estimate', 'epsEstimate', 'Eps_Estimate']:
                val = cal.get(key)
                if val is not None:
                    try: eps_est = float(val)
                    except: pass
                    break

            eps_actual = None
            for key in ['EPS Actual', 'epsActual', 'Reported EPS']:
                val = cal.get(key)
                if val is not None:
                    try: eps_actual = float(val)
                    except: pass
                    break

            # Fallback: buscar en quarterly_earnings si cal no tiene actual
            if eps_actual is None and earn_date <= today:
                try:
                    qe = tk.quarterly_earnings
                    if qe is not None and not qe.empty:
                        # La fila más reciente
                        row = qe.iloc[-1]
                        if 'Reported EPS' in qe.columns:
                            v = row.get('Reported EPS')
                            if v is not None:
                                try: eps_actual = float(v)
                                except: pass
                        if eps_actual is None and 'EPS Actual' in qe.columns:
                            v = row.get('EPS Actual')
                            if v is not None:
                                try: eps_actual = float(v)
                                except: pass
                        if eps_est is None and 'EPS Estimate' in qe.columns:
                            v = row.get('EPS Estimate')
                            if v is not None:
                                try: eps_est = float(v)
                                except: pass
                except: pass

            # ── Revenue estimado ───────────────────────────────────────────
            rev_est = None
            for key in ['Revenue Estimate', 'revenueEstimate', 'Revenue_Estimate']:
                val = cal.get(key)
                if val is not None:
                    try: rev_est = float(val)
                    except: pass
                    break

            rev_actual = None
            for key in ['Revenue Actual', 'revenueActual']:
                val = cal.get(key)
                if val is not None:
                    try: rev_actual = float(val)
                    except: pass
                    break

            # Fallback revenue: quarterly_financials
            if rev_actual is None and earn_date <= today:
                try:
                    qf = tk.quarterly_financials
                    if qf is not None and not qf.empty:
                        for rk in ['Total Revenue', 'Revenue']:
                            if rk in qf.index:
                                v = qf.loc[rk].iloc[0]
                                try: rev_actual = float(v)
                                except: pass
                                break
                except: pass

            # ── Beat / Miss ────────────────────────────────────────────────
            eps_beat_pct = _beat_pct(eps_actual, eps_est) if eps_actual is not None and eps_est is not None else None
            rev_beat_pct = _beat_pct(rev_actual, rev_est)  if rev_actual is not None and rev_est is not None else None

            # ── Hora (BMO / AMC / —) ──────────────────────────────────────
            time_str = "—"
            for key in ['Earnings Time', 'earningsTime', 'Earnings Call Time']:
                val = cal.get(key)
                if val is not None:
                    s = str(val).upper()
                    if 'BMO' in s or 'BEFORE' in s:
                        time_str = "BMO"
                    elif 'AMC' in s or 'AFTER' in s:
                        time_str = "AMC"
                    else:
                        time_str = str(val)[:5]
                    break

            # ── Delta / WHEN ───────────────────────────────────────────────
            delta = (earn_date - today).days
            if delta < 0:
                when = f"-{abs(delta)}d"
            elif delta == 0:
                when = "HOY"
            elif delta == 1:
                when = "MAÑANA"
            else:
                when = earn_date.strftime("%d/%m")

            return {
                "date":         earn_date.strftime("%Y-%m-%d"),
                "ticker":       sym,
                "company":      company[:28],
                "when":         when,
                "time":         time_str,           # BMO / AMC
                "mcap":         mcap,
                "eps_est":      eps_est,
                "eps_actual":   eps_actual,
                "eps_beat_pct": eps_beat_pct,
                "rev_est":      _fmt_rev(rev_est),
                "rev_actual":   _fmt_rev(rev_actual),
                "rev_beat_pct": rev_beat_pct,
                "reported":     eps_actual is not None or rev_actual is not None,
                "today":        delta == 0,
                "delta":        delta,
            }
        except Exception as ex:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_get_earnings, sym): sym for sym in TOP_CAPS}
        for f in concurrent.futures.as_completed(futures, timeout=60):
            try:
                res = f.result()
                if res and res["ticker"] not in seen:
                    seen.add(res["ticker"])
                    earnings.append(res)
            except: pass

    earnings.sort(key=lambda x: (x.get("delta", 99), x.get("mcap", "—") == "—"))
    return earnings[:30]


def fetch_put_call():
    """Put/Call ratio + VIX term structure desde CBOE (gratuito)"""
    result = {}
    try:
        r = requests.get("https://www.cboe.com/us/options/market_statistics/daily/",
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2: continue
                label = cells[0].lower()
                try:
                    val = float(cells[-1])
                    if "equity" in label and "put" in label:   result["equity_pc"] = val
                    elif "index" in label and "put" in label:  result["index_pc"]  = val
                    elif "total" in label and "put" in label:  result["total_pc"]  = val
                except: pass
    except: pass

    # VIX term structure: VIX9D, VIX, VIX3M, VIX6M
    for sym, key in [("^VIX9D","vix9d"),("^VIX","vix"),("^VIX3M","vix3m"),("^VIX6M","vix6m")]:
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="2d")
            if not hist.empty:
                result[key] = round(float(hist["Close"].iloc[-1]), 2)
        except: pass

    # SKEW index desde CBOE
    try:
        r2 = requests.get("https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.json",
                          timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r2.status_code == 200:
            data = r2.json()
            if data and isinstance(data, list):
                last = data[-1]
                result["skew"] = round(float(last.get("close", last.get("price", 0))), 1)
    except: pass

    return result


def fetch_options():
    """
    Opciones enriquecidas con Polygon.io (gratis, 15min delay):
    - Sentimiento: P/C ratio por ticker, dominancia call/put
    - Flujo: volumen, OI, vol/OI ratio
    - Estructura: IV implícita, greeks agregados, expected move
    Cubre ETFs principales + top S&P500 por capitalización
    """
    import os
    POLYGON_KEY = os.environ.get("POLYGON_KEY", "")

    # ETFs principales
    ETFS = [
        "SPY","QQQ","IWM","DIA",
        "XLK","XLF","XLE","XLV","XLY","XLI","XLP","XLU","XLRE","XLB","XLC",
        "GLD","SLV","TLT","HYG","EEM","EFA","VXX","UVXY",
    ]

    # S&P 500 completo — top 500 por capitalización aproximada
    SP500 = [
        # Mega cap
        "NVDA","AAPL","MSFT","AMZN","GOOGL","META","TSLA","AVGO","JPM","LLY",
        "V","UNH","XOM","MA","HD","PG","COST","ABBV","MRK","CVX",
        "WMT","BAC","NFLX","AMD","ACN","TMO","MCD","CRM","ORCL","GS",
        "MS","IBM","TXN","CSCO","QCOM","ADBE","NOW","AMAT","INTU","AMGN",
        # Large cap
        "INTC","DIS","GE","CAT","HON","BA","BLK","SPGI","DE","VRTX",
        "REGN","ADI","PANW","KLAC","LRCX","SNPS","CDNS","MRVL","WDAY","TTD",
        "AXP","MMM","LMT","RTX","UNP","UPS","FDX","SBUX","NKE","MO",
        "PM","KO","PEP","JNJ","ABT","BMY","PFE","GILD","ISRG","SYK",
        "ZTS","CI","HUM","ELV","CNC","MCK","PYPL","SQ","COIN","UBER",
        "ABNB","HOOD","RBLX","SNAP","PINS","LYFT","ROKU","DKNG","ZM","SHOP",
        # Financials
        "WFC","C","USB","TFC","PNC","RF","FITB","KEY","CFG","HBAN",
        "STT","BK","SCHW","CME","ICE","CBOE","NDAQ","AIG","MET","PRU",
        "AFL","ALL","PGR","TRV","MMC","AON","AJG",
        # Healthcare
        "DHR","BSX","MDT","EW","BAX","BDX","IQV","IQVIA","A","WAT",
        "MTD","PKI","HOLX","ALGN","DXCM","IDXX","PODD","NVOAX","MRNA","BNTX",
        # Tech
        "PLTR","SNOW","NET","CRWD","ZS","OKTA","DDOG","MDB","GTLB","U",
        "RBLX","PATH","AI","BBAI","SOUN","ASTS","IONQ","QUBT","RGTI","ARQT",
        # Industrials
        "EMR","ETN","PH","ROK","AME","IR","XYL","IEX","FTV","GNRC",
        "LHX","NOC","GD","HII","TDG","HWM","SPR","TXT","WWD","AXON",
        # Consumer
        "AMZN","TGT","LOW","TJX","ROST","BURL","DG","DLTR","FIVE","BBBY",
        "CMG","YUM","DPZ","QSR","SHAK","WING","CAKE","EAT","DINE","FAT",
        # Energy
        "SLB","HAL","BKR","MPC","VLO","PSX","COP","EOG","DVN","FANG",
        "MRO","APA","OXY","HES","CNX","AR","EQT","RRC","SWN","COG",
        # Materials
        "APD","LIN","PPG","SHW","ECL","DD","DOW","LYB","NEM","FCX",
        "AA","CLF","X","NUE","STLD","RS","CMC","ATI","HCC","HL",
        # Utilities / REIT
        "NEE","DUK","SO","D","AEP","EXC","SRE","XEL","ES","AWK",
        "AMT","PLD","CCI","EQIX","DLR","PSA","O","SPG","VTR","WELL",
        # More large cap
        "F","GM","STLA","TM","HMC","RIVN","LCID","FSR","NKLA","GOEV",
        "NCLH","CCL","RCL","MAR","HLT","H","IHG","WH","CHH","VAC",
        "UAL","DAL","AAL","LUV","ALK","JBLU","SAVE","HA","MESA","SKYW",
    ]

    flows = []
    seen = set()

    def _polygon_options(sym):
        """Obtiene snapshot de opciones desde Polygon para un ticker"""
        if not POLYGON_KEY:
            return None
        try:
            url = f"https://api.polygon.io/v3/snapshot/options/{sym}"
            params = {
                "apiKey": POLYGON_KEY,
                "limit": 250,
                "order": "desc",
                "sort": "open_interest",
            }
            r = requests.get(url, params=params, timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            data = r.json().get("results", [])
            if not data:
                return None

            calls = [x for x in data if x.get("details", {}).get("contract_type") == "call"]
            puts  = [x for x in data if x.get("details", {}).get("contract_type") == "put"]

            # Volumen y OI agregados
            vol_c  = sum(x.get("day", {}).get("volume", 0) or 0 for x in calls)
            vol_p  = sum(x.get("day", {}).get("volume", 0) or 0 for x in puts)
            oi_c   = sum(x.get("open_interest", 0) or 0 for x in calls)
            oi_p   = sum(x.get("open_interest", 0) or 0 for x in puts)
            vol_total = vol_c + vol_p

            if vol_total < 100:
                return None

            # Put/Call ratio
            pc_vol = round(vol_p / vol_c, 2) if vol_c > 0 else None
            pc_oi  = round(oi_p  / oi_c, 2) if oi_c  > 0 else None

            # Vol/OI ratio (actividad inusual si >0.5)
            oi_total = oi_c + oi_p
            vol_oi = round(vol_total / oi_total, 3) if oi_total > 0 else None

            # IV implícita media (weighted por volumen)
            iv_vals = []
            for x in data:
                iv = x.get("implied_volatility")
                v  = x.get("day", {}).get("volume", 0) or 0
                if iv and v > 0:
                    iv_vals.append((float(iv), v))
            iv_wavg = None
            if iv_vals:
                total_v = sum(v for _, v in iv_vals)
                iv_wavg = round(sum(iv * v for iv, v in iv_vals) / total_v * 100, 1) if total_v > 0 else None

            # Greeks agregados (delta medio de calls más activos)
            delta_vals = []
            for x in sorted(calls, key=lambda x: x.get("day", {}).get("volume", 0) or 0, reverse=True)[:10]:
                d = x.get("greeks", {}).get("delta")
                if d: delta_vals.append(float(d))
            avg_delta = round(sum(delta_vals) / len(delta_vals), 2) if delta_vals else None

            # Expected move: IV * precio * sqrt(DTE/365)
            exp_move_pct = None
            try:
                # Buscar contrato ATM más cercano con mayor volumen
                atm = sorted(data, key=lambda x: abs((x.get("day", {}).get("close", 0) or 0) - 0), reverse=False)
                for x in data[:20]:
                    iv_x = x.get("implied_volatility")
                    det  = x.get("details", {})
                    dte_str = det.get("expiration_date", "")
                    if iv_x and dte_str:
                        from datetime import date as ddate
                        dte = (ddate.fromisoformat(dte_str) - ddate.today()).days
                        if 1 <= dte <= 45:
                            exp_move_pct = round(float(iv_x) * (dte / 365) ** 0.5 * 100, 1)
                            break
            except: pass

            # Señal dominante
            if pc_vol is not None:
                if pc_vol < 0.7:   tipo = "CALL"; signal = "Dom. CALL"
                elif pc_vol > 1.3: tipo = "PUT";  signal = "Dom. PUT"
                else:              tipo = "MIX";  signal = "Equilibrado"
            else:
                tipo = "MIX"; signal = "Sin datos"

            # Top strike call y put por volumen
            top_c = sorted(calls, key=lambda x: x.get("day", {}).get("volume", 0) or 0, reverse=True)
            top_p = sorted(puts,  key=lambda x: x.get("day", {}).get("volume", 0) or 0, reverse=True)
            sk_c  = top_c[0]["details"].get("strike_price", "?") if top_c else "?"
            sk_p  = top_p[0]["details"].get("strike_price", "?") if top_p else "?"
            exp_c = top_c[0]["details"].get("expiration_date", "")[:10] if top_c else ""

            detail = (f"{signal} | Exp {exp_c} | "
                      f"C:{vol_c:,}@${sk_c} P:{vol_p:,}@${sk_p}")

            return {
                "ticker":      sym,
                "tipo":        tipo,
                "signal":      signal,
                "detail":      detail,
                "source":      "POLY",
                "time":        (datetime.utcnow() + timedelta(hours=2)).strftime("%H:%M"),
                "is_etf":      sym in ETFS,
                "score":       6 if tipo != "MIX" else 2,
                # Métricas enriquecidas
                "vol_call":    vol_c,
                "vol_put":     vol_p,
                "vol_total":   vol_total,
                "oi_call":     oi_c,
                "oi_put":      oi_p,
                "pc_vol":      pc_vol,
                "pc_oi":       pc_oi,
                "vol_oi":      vol_oi,
                "iv_avg":      iv_wavg,
                "avg_delta":   avg_delta,
                "exp_move_pct": exp_move_pct,
                "top_call_strike": sk_c,
                "top_put_strike":  sk_p,
                "top_exp":     exp_c,
            }
        except Exception as e:
            return None

    def _yf_options_fallback(sym):
        """Fallback con yfinance si no hay Polygon key"""
        try:
            tk   = yf.Ticker(sym)
            exps = tk.options
            if not exps: return None
            chain  = tk.option_chain(exps[0])
            calls  = chain.calls
            puts   = chain.puts
            vol_c  = int(calls["volume"].fillna(0).sum()) if not calls.empty else 0
            vol_p  = int(puts["volume"].fillna(0).sum())  if not puts.empty else 0
            oi_c   = int(calls["openInterest"].fillna(0).sum()) if not calls.empty else 0
            oi_p   = int(puts["openInterest"].fillna(0).sum())  if not puts.empty else 0
            if vol_c + vol_p < 200: return None
            pc_vol = round(vol_p / vol_c, 2) if vol_c > 0 else None
            if pc_vol is not None:
                if pc_vol < 0.7:   tipo = "CALL"; signal = "Dom. CALL"
                elif pc_vol > 1.3: tipo = "PUT";  signal = "Dom. PUT"
                else:              tipo = "MIX";  signal = "Equilibrado"
            else:
                tipo = "MIX"; signal = "Sin datos"
            top_c = calls.nlargest(1,"volume").iloc[0] if not calls.empty and "volume" in calls.columns else None
            top_p = puts.nlargest(1,"volume").iloc[0]  if not puts.empty  and "volume" in puts.columns  else None
            sk_c  = top_c.get("strike","?") if top_c is not None else "?"
            sk_p  = top_p.get("strike","?") if top_p is not None else "?"
            iv_avg = None
            if not calls.empty and "impliedVolatility" in calls.columns:
                iv_vals = calls["impliedVolatility"].dropna()
                if not iv_vals.empty:
                    iv_avg = round(float(iv_vals.mean()) * 100, 1)
            detail = (f"{signal} | Exp {exps[0]} | "
                      f"C:{vol_c:,}@${sk_c} P:{vol_p:,}@${sk_p}")
            return {
                "ticker":    sym,
                "tipo":      tipo,
                "signal":    signal,
                "detail":    detail,
                "source":    "YF",
                "time":      (datetime.utcnow() + timedelta(hours=2)).strftime("%H:%M"),
                "is_etf":    sym in ETFS,
                "score":     5 if tipo != "MIX" else 2,
                "vol_call":  vol_c,
                "vol_put":   vol_p,
                "vol_total": vol_c + vol_p,
                "oi_call":   oi_c,
                "oi_put":    oi_p,
                "pc_vol":    pc_vol,
                "pc_oi":     round(oi_p / oi_c, 2) if oi_c > 0 else None,
                "vol_oi":    round((vol_c + vol_p) / (oi_c + oi_p), 3) if (oi_c + oi_p) > 0 else None,
                "iv_avg":    iv_avg,
                "avg_delta": None,
                "exp_move_pct": None,
                "top_call_strike": sk_c,
                "top_put_strike":  sk_p,
                "top_exp":   exps[0],
            }
        except: return None

    import concurrent.futures

    all_syms = ["SPY","QQQ","IWM","DIA","VXX","GLD","TLT","XLK","XLF","XLE"]

    def _get(sym):
        if POLYGON_KEY:
            res = _polygon_options(sym)
            if res: return res
        # fallback yfinance solo para los top 65 (evitar timeout)
        if sym in ETFS or sym in SP500[:50]:
            return _yf_options_fallback(sym)
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_get, sym): sym for sym in all_syms}
        for f in concurrent.futures.as_completed(futures, timeout=55):
            try:
                res = f.result()
                if res and res["ticker"] not in seen:
                    seen.add(res["ticker"])
                    flows.append(res)
            except: pass

    # Ordenar: ETFs primero, luego por score y volumen
    flows.sort(key=lambda x: (
        0 if x.get("is_etf") else 1,
        -x.get("score", 0),
        -x.get("vol_total", 0)
    ))
    return flows[:30]


NEWS_SOURCES=[
    ("CNBC",        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("CNBC",        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("CNBC",        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("WSJ",         "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("WSJ",         "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("Benzinga",    "https://www.benzinga.com/feed"),
    ("Benzinga",    "https://www.benzinga.com/category/news/feed"),
    ("Yahoo",       "https://finance.yahoo.com/rss/topstories"),
    ("Yahoo",       "https://finance.yahoo.com/rss/industry?industry=semiconductors"),
    ("Yahoo",       "https://finance.yahoo.com/rss/industry?industry=technology"),
    ("Zerohedge",   "https://feeds.feedburner.com/zerohedge/feed"),
    ("Fed",         "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("UW",          "https://unusualwhales.com/rss"),
    ("FT",          "https://www.ft.com/rss/home"),
    ("Bloomberg",   "https://feeds.bloomberg.com/markets/news.rss"),
    ("Investing",   "https://www.investing.com/rss/news.rss"),
    ("FinancialJuice","https://www.financialjuice.com/feed.ashx?xy=rss"),
]

def fetch_news():
    articles=[]
    cutoff=time.time()-12*3600  # Ventana de 12 horas
    seen_titles=set()  # Reset en cada ciclo — no acumular entre llamadas
    seen_links=set()
    headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
             "Accept":"application/rss+xml, application/xml, text/xml, */*"}
    for source,url in NEWS_SOURCES:
        try:
            r=requests.get(url,timeout=10,headers=headers)
            if r.status_code!=200: continue
            feed=feedparser.parse(r.text)
            for entry in feed.entries[:15]:  # Mas entradas por feed
                pub=entry.get("published_parsed")
                if pub and time.mktime(pub)<cutoff: continue
                title=entry.get("title","").strip()
                if not title or len(title)<15: continue
                # Dedup por titulo Y por link
                k=title[:60].lower().replace(" ","")
                lnk=entry.get("link","")
                if k in seen_titles or lnk in seen_links: continue
                seen_titles.add(k)
                if lnk: seen_links.add(lnk)
                summary=BeautifulSoup(entry.get("summary",entry.get("description","")),"html.parser").get_text()[:400]
                sc=score_text(title,summary)
                pub_str=(datetime(*pub[:6])+timedelta(hours=2)).strftime("%H:%M %d/%m") if pub else "--"
                ts=time.mktime(pub) if pub else time.time()-3600
                impact_lbl=get_impact_label(title,summary)
                articles.append({"source":source,"title":title,"title_en":title,
                                  "summary":summary,"link":lnk,
                                  "score":sc,"time":pub_str,"timestamp":ts,
                                  "market_impact":impact_lbl})
        except Exception as e:
            pass
    # Ordenar por timestamp desc
    articles.sort(key=lambda x:x["timestamp"],reverse=True)
    # Limitar a 120 noticias (sin filtro de score mínimo)
    articles = articles[:60]
    # Traducir en batch (los primeros 60 para no tardar demasiado)
    # Traduccion desactivada para reducir RAM
    return articles

# ── SOCIAL FEED ───────────────────────────────────────────────────────────────
# Arquitectura: fuentes primarias directas con RSS fiable + Truth Social para Trump
# Cada fuente tiene cutoff de 48h y timestamp numérico real para ordenar correctamente
# Nitter eliminado — bloqueado en Railway. Solo fuentes RSS nativas.

SOCIAL_CUTOFF_H = 24  # horas hacia atrás — solo posts de hoy

# (display_name, platform_tag, url, min_score, max_len)
# min_score=0  → siempre mostrar (institucional — siempre relevante)
# min_score=1+ → filtrar ruido
# max_len      → truncar texto a N chars (tweets cortos vs artículos largos)
#
# CRITERIO de inclusión aquí vs NEWS_SOURCES:
#   SOCIAL = fuentes tipo "tweet" / squawk / comunicado corto / alerta
#   NEWS   = artículos largos de medios (CNBC, MarketWatch, WSJ, Bloomberg…)
#   ZeroHedge y TheStreet están en NEWS — aquí solo pinchamos su feed de titulares cortos

SOCIAL_SOURCES = [
    # ── TRUMP / TRUTH SOCIAL ──────────────────────────────────────────────────
    # Posts originales de Trump — RSS nativo de Truth Social, funciona bien
    ("TRUMP",         "TRUTH",  "https://www.trumpstruth.org/feed",                                      0, 280),

    # ── CASA BLANCA ───────────────────────────────────────────────────────────
    # Executive orders, proclamaciones, briefings — salen aquí antes que en X
    ("WHITE HOUSE",   "WH",     "https://www.whitehouse.gov/feed/",                                     0, 200),

    # ── FED / POWELL / GOBERNADORES ───────────────────────────────────────────
    # Comunicados de prensa Fed, minutas FOMC, discursos Powell y gobernadores
    ("FED",           "FED",    "https://www.federalreserve.gov/feeds/press_all.xml",                   0, 200),
    ("FED",           "FED",    "https://www.federalreserve.gov/feeds/speeches.xml",                    0, 200),

    # ── TREASURY / BESSENT / OFAC ─────────────────────────────────────────────
    # Comunicados Treasury, sanciones OFAC — mueven DXY, bonos y acciones afectadas
    ("TREASURY",      "GOV",    "https://home.treasury.gov/news/press-releases.xml",                    0, 200),
    ("TREASURY",      "GOV",    "https://home.treasury.gov/system/files/136/ofac-recent-actions.xml",   0, 200),

    # ── SEC ───────────────────────────────────────────────────────────────────
    # Enforcement actions, cargos de fraude — mueven acciones individuales
    ("SEC",           "GOV",    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=10&search_text=&output=atom", 0, 200),

    # ── FINANCIALJUICE — SQUAWK PRO ───────────────────────────────────────────
    # Titulares cortos tipo squawk de sala de trading — lo que repostea DeItaone
    # Formato ideal: una línea, directo al grano, muy alta frecuencia
    ("FINANCIALJUICE","FJ",     "https://www.financialjuice.com/feed.ashx?xy=rss",                      0, 220),

    # ── UNUSUAL WHALES ────────────────────────────────────────────────────────
    # Flujo de opciones, dark pool, insider — alertas cortas tipo tweet
    ("UNUSUALWHALES", "UW",     "https://unusualwhales.com/rss",                                        0, 220),

    # ── ZEROHEDGE — solo titulares ────────────────────────────────────────────
    # Títulos cortos, impacto macro — el cuerpo del artículo ya está en NEWS
    ("ZEROHEDGE",     "X/ZH",   "https://feeds.feedburner.com/zerohedge/feed",                          1, 120),

    # ── REUTERS BREAKING ──────────────────────────────────────────────────────
    # Reuters Wire — titulares cortos de mercados y macro global
    # DeItaone en X básicamente repostea esto + FinancialJuice
    ("REUTERS",       "RTRS",   "https://feeds.reuters.com/reuters/businessNews",                       1, 160),
    ("REUTERS",       "RTRS",   "https://feeds.reuters.com/reuters/marketsNews",                        1, 160),

    # ── POLITICO ──────────────────────────────────────────────────────────────
    # Política económica Washington — aranceles, sanciones, nombramientos
    ("POLITICO",      "POL",    "https://rss.politico.com/economy.xml",                                 1, 160),

    # ── AP BREAKING ───────────────────────────────────────────────────────────
    # AP rompe geopolítica antes que nadie — Irán, Ucrania, OPEC, guerras
    ("AP",            "AP",     "https://feeds.apnews.com/rss/apf-finance",                             1, 160),
    ("AP",            "AP",     "https://feeds.apnews.com/rss/apf-topnews",                             2, 160),
]

_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
}

def _fetch_social_source(display, tag, url, min_score, max_len=220):
    """Descarga y parsea una fuente social. Retorna lista de posts."""
    cutoff = time.time() - SOCIAL_CUTOFF_H * 3600
    out = []
    try:
        r = requests.get(url, timeout=12, headers=_HDR)
        if r.status_code != 200:
            return []
        feed = feedparser.parse(r.text)
        if not feed.entries:
            return []
        for e in feed.entries[:15]:
            pub = e.get("published_parsed") or e.get("updated_parsed")
            ts  = time.mktime(pub) if pub else (time.time() - 3600)
            # Filtro de tiempo — solo posts recientes
            if ts < cutoff:
                continue
            # Extraer título y cuerpo del post
            title = (e.get("title") or "").strip()
            raw   = (e.get("summary") or e.get("description") or "")
            body  = BeautifulSoup(raw, "html.parser").get_text(" ").strip()
            body  = re.sub(r'\s+', ' ', body)

            # Detectar títulos inútiles (URLs, "Sin título", vacíos)
            title_bad = (
                len(title) < 15
                or title.lower().startswith("[sin t")
                or title.lower().startswith("no title")
                or title.lower().startswith("[no title")
                or title.startswith("http")
                or title.startswith("RT: http")
                or re.match(r'^https?://', title)
            )

            if title_bad:
                # Usar body directamente (caso Trump/Truth Social)
                txt = body[:max_len] if body and len(body) > 15 else ""
            elif max_len <= 220 and not title_bad:
                # Squawk corto: el título ya es el texto (FinancialJuice, ZH, Reuters)
                txt = title[:max_len]
            elif body and len(body) > 20:
                # Posts largos: usar cuerpo
                txt = body[:max_len]
            else:
                txt = title[:max_len]

            # Descartar si sigue siendo basura
            if not txt or len(txt) < 15 or txt.startswith("http"):
                continue

            sc = score_text(txt)
            if sc < min_score:
                continue

            time_str = datetime.utcfromtimestamp(ts + 7200).strftime("%H:%M %d/%m")
            out.append({
                "account":       display,
                "platform":      tag,
                "text":          txt,
                "time":          time_str,
                "timestamp":     ts,      # numérico — para ordenar bien
                "score":         sc,
                "link":          e.get("link", ""),
                "market_impact": get_impact_label(txt),
            })
    except Exception as ex:
        print(f"[social] Error {display} ({url[:50]}): {ex}")
    return out


def fetch_social():
    """Descarga todas las fuentes sociales en paralelo, traduce ES y ordena por timestamp real."""
    all_posts = []
    seen = set()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {
            ex.submit(_fetch_social_source, disp, tag, url, msc, mxl): (disp, tag, url)
            for disp, tag, url, msc, mxl in SOCIAL_SOURCES
        }
        for f in concurrent.futures.as_completed(futures, timeout=25):
            try:
                posts = f.result()
                for p in posts:
                    k = p["text"][:80].lower().replace(" ", "")
                    if k in seen:
                        continue
                    seen.add(k)
                    p["text_en"] = p["text"]  # guardar original en ingles
                    all_posts.append(p)
            except Exception as e:
                print(f"[social] Future error: {e}")

    # Ordenar por timestamp numerico DESC
    all_posts.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    all_posts = all_posts[:60]

    # Traducir EN->ES igual que fetch_news — batch en paralelo
    # Traduccion desactivada para reducir RAM

    return all_posts

_ulock=threading.Lock()
def update_all():
    if not _ulock.acquire(blocking=False): return
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Actualizando...")
        # Secuencial para no saturar memoria — cada bloque libera antes del siguiente
        for fn, key in [
            (fetch_market,       "market"),
            (fetch_news,         "news"),
            (fetch_social,       "social"),
            (fetch_calendar,     "calendar"),
            (fetch_fear_greed,   "fear_greed"),
            (fetch_sector_heatmap, "sectors"),
        ]:
            try:
                result = fn()
                with LOCK: CACHE[key] = result
                gc.collect()
            except Exception as e:
                print(f"Error {key}: {e}")
        # Pesados solo si hay memoria disponible
        for fn, key in [
            (fetch_technicals,      "technicals"),
            (fetch_put_call,        "put_call"),
            (fetch_earnings,        "earnings"),
            (fetch_market_breadth,  "breadth"),
            (fetch_options,         "options"),
        ]:
            try:
                result = fn()
                with LOCK: CACHE[key] = result
                gc.collect()
            except Exception as e:
                print(f"Error {key}: {e}")
        with LOCK: CACHE["last_update"]=datetime.now().isoformat()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] OK")
    finally: _ulock.release()

def update_fast():
    """Precios cada 30s"""
    try:
        results = {}
        def _m(): results["market"] = fetch_market()
        def _t(): results["technicals"] = fetch_technicals()
        threads = [threading.Thread(target=fn, daemon=True) for fn in [_m, _t]]
        for t in threads: t.start()
        for t in threads: t.join(timeout=90)
        with LOCK:
            CACHE.update(results)
            CACHE["last_update"] = datetime.now().isoformat()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Precios OK")
    except Exception as e:
        print(f"Error fast update: {e}")

def update_medium():
    """Noticias y opciones cada 3 min"""
    try:
        results = {}
        def _n(): results["news"] = fetch_news()
        def _o(): results["options"] = fetch_options()
        def _s(): results["social"] = fetch_social()
        def _se(): results["sectors"] = fetch_sector_heatmap()
        threads = [threading.Thread(target=fn, daemon=True) for fn in [_n, _o, _s, _se]]
        for t in threads: t.start()
        for t in threads: t.join(timeout=120)
        with LOCK:
            CACHE.update(results)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Noticias+Opciones OK — {len(results.get('news',[]))} noticias | {len(results.get('options',[]))} opciones")
    except Exception as e:
        print(f"Error medium update: {e}")

def update_slow():
    """Calendario, earnings, sentimiento cada 15 min"""
    try:
        results = {}
        def _f(): results["fear_greed"] = fetch_fear_greed()
        def _p(): results["put_call"] = fetch_put_call()
        def _c(): results["calendar"] = fetch_calendar()
        def _e(): results["earnings"] = fetch_earnings()
        def _br(): results["breadth"] = fetch_market_breadth()
        threads = [threading.Thread(target=fn, daemon=True) for fn in [_f, _p, _c, _e, _br]]
        for t in threads: t.start()
        for t in threads: t.join(timeout=40)
        with LOCK:
            CACHE.update(results)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Calendario+Sentimiento OK")
    except Exception as e:
        print(f"Error slow update: {e}")

def bg_updater():
    fast_count = 0
    medium_count = 0
    slow_count = 0
    while True:
        time.sleep(30)
        fast_count += 1
        medium_count += 1
        slow_count += 1
        # Precios cada 30s
        try: threading.Thread(target=update_fast, daemon=True).start()
        except: pass
        # Noticias+Opciones cada 90s (cada 3 ciclos de 30s)
        if medium_count >= 10:
            medium_count = 0
            try: threading.Thread(target=update_medium, daemon=True).start()
            except: pass
        # Calendario+Sentimiento cada 15 min (cada 30 ciclos de 30s)
        if fast_count >= 60:
            fast_count = 0
            try: threading.Thread(target=update_slow, daemon=True).start()
            except: pass
        # Cierres anteriores — refrescar cada hora (120 ciclos de 30s)
        if slow_count >= 120:
            slow_count = 0
            try: threading.Thread(target=_load_prev_closes, daemon=True).start()
            except: pass

HTML=Path(__file__).parent/"dashboard.html"
HTML_MOBILE=Path(__file__).parent/"dashboard_mobile.html"
SW_JS=Path(__file__).parent/"sw.js"
MANIFEST=Path(__file__).parent/"manifest.json"
FAVICON=Path(__file__).parent/"favicon.png"
ICON192=Path(__file__).parent/"icon-192.png"
ICON512=Path(__file__).parent/"icon-512.png"
def generate_local_analysis(cache):
    m = cache.get('market', {})
    tech = cache.get('technicals', {})
    fg = cache.get('fear_greed', {})
    breadth = cache.get('breadth', {})
    news = cache.get('news', [])
    options = cache.get('options', [])
    cal = cache.get('calendar', [])
    social = cache.get('social', [])

    L = []
    now = datetime.now().strftime('%H:%M %d/%m/%Y')

    spx = m.get('S&P 500', {})
    ndx = m.get('Nasdaq 100', {})
    dji = m.get('Dow Jones', {})
    rut = m.get('Russell 2K', {})
    vix = m.get('VIX', {})
    wti = m.get('WTI Oil', {})
    brt = m.get('Brent', {})
    oro = m.get('Oro', {})
    dxy = m.get('DXY', {})
    b2  = m.get('Bono 2Y', {})
    b10 = m.get('Bono 10Y', {})
    spx_t = tech.get('SPX', {})
    ndx_t = tech.get('NDX', {})

    spx_pct = spx.get('pct', 0) or 0
    vix_p   = vix.get('price', 20) or 20
    wti_pct = wti.get('pct', 0) or 0
    oro_pct = oro.get('pct', 0) or 0
    dxy_pct = dxy.get('pct', 0) or 0
    rsi_val = spx_t.get('rsi', 50) or 50
    above_ma200 = spx_t.get('above_ma200', True)

    # Risk scoring
    risk = 0
    if vix_p > 25: risk += 1
    if vix_p > 35: risk += 1
    if spx_pct < -1: risk += 1
    if spx_pct < -2: risk += 1
    if wti_pct > 1.5: risk += 1
    if oro_pct > 0.5: risk += 1
    if dxy_pct > 0.3: risk += 1
    if not above_ma200: risk += 1
    risk_mode = 'RISK-OFF EXTREMO' if risk >= 6 else 'RISK-OFF FUERTE' if risk >= 4 else 'RISK-OFF' if risk >= 2 else 'RISK-ON'

    vix_s = 'PANICO' if vix_p > 35 else 'MIEDO ALTO' if vix_p > 25 else 'ELEVADO' if vix_p > 20 else 'NORMAL'
    trend = 'BAJISTA FUERTE' if spx_pct < -2 else 'BAJISTA' if spx_pct < -0.5 else 'ALCISTA FUERTE' if spx_pct > 2 else 'ALCISTA' if spx_pct > 0.5 else 'LATERAL'

    L.append('MARKET INTELLIGENCE REPORT -- ' + now)
    L.append('=' * 60)

    # 1. SITUACION GENERAL
    L.append('')
    L.append('1. SITUACION GENERAL DEL MERCADO')
    L.append('-' * 40)
    if spx: L.append(f'  S&P 500  : {spx.get("price",0):>10.2f}  ({spx_pct:+.2f}%)  [{trend}]')
    if ndx:  L.append(f'  Nasdaq   : {ndx.get("price",0):>10.2f}  ({ndx.get("pct",0):+.2f}%)')
    if dji:  L.append(f'  Dow Jones: {dji.get("price",0):>10.2f}  ({dji.get("pct",0):+.2f}%)')
    if rut:  L.append(f'  Russell2K: {rut.get("price",0):>10.2f}  ({rut.get("pct",0):+.2f}%)')
    L.append('')
    if vix:  L.append(f'  VIX      : {vix_p:>6.2f}  [{vix_s}]  ({vix.get("pct",0):+.2f}%)')
    if wti:  L.append(f'  WTI Oil  : {wti.get("price",0):>6.2f}  ({wti_pct:+.2f}%)')
    if brt:  L.append(f'  Brent    : {brt.get("price",0):>6.2f}  ({brt.get("pct",0):+.2f}%)')
    if oro:  L.append(f'  Oro      : {oro.get("price",0):>6.2f}  ({oro_pct:+.2f}%)')
    if dxy:  L.append(f'  DXY      : {dxy.get("price",0):>8.4f}  ({dxy_pct:+.2f}%)')
    if b10:  L.append(f'  Bono 10Y : {b10.get("price",0):>5.2f}%  ({b10.get("pct",0):+.2f}%)')
    if b2:   L.append(f'  Bono 2Y  : {b2.get("price",0):>5.2f}%')
    if b2 and b10:
        spread = (b10.get("price",0) or 0) - (b2.get("price",0) or 0)
        inv = ' [INVERTIDA]' if spread < 0 else ''
        L.append(f'  Curva 2s10s: {spread:+.2f}%{inv}')
    L.append('')
    L.append(f'  >> MODO MERCADO: {risk_mode} ({risk} senales negativas activas)')

    # 2. TECNICOS
    L.append('')
    L.append('2. INDICADORES TECNICOS')
    L.append('-' * 40)
    if spx_t:
        rsi_lbl = 'SOBRECOMPRA -- cuidado con posiciones largas' if rsi_val > 70 else 'SOBREVENTA -- posible rebote tecnico' if rsi_val < 30 else 'zona neutral'
        L.append(f'  RSI(14) SPX: {rsi_val}  [{rsi_lbl}]')
        L.append(f'  MACD SPX   : {spx_t.get("macd_trend","")}  |  Histograma: {spx_t.get("macd_hist",0):.2f}')
        ma_flags = []
        if not spx_t.get('above_ma20'):  ma_flags.append('DEBAJO MA20')
        if not spx_t.get('above_ma50'):  ma_flags.append('DEBAJO MA50')
        if not spx_t.get('above_ma200'): ma_flags.append('DEBAJO MA200')
        if ma_flags:
            L.append(f'  Medias     : {" | ".join(ma_flags)}')
        else:
            L.append('  Medias     : precio por encima de MA20, MA50 y MA200')
        L.append(f'  Cross      : {"GOLDEN CROSS" if spx_t.get("golden_cross") else "DEATH CROSS"}')
    if ndx_t:
        L.append(f'  RSI(14) NDX: {ndx_t.get("rsi",0)}  [{ndx_t.get("rsi_zone","")}]')
        L.append(f'  Cross NDX  : {"GOLDEN CROSS" if ndx_t.get("golden_cross") else "DEATH CROSS"}')
    if breadth:
        ad = breadth.get("ad_ratio", 0)
        L.append(f'  Amplitud   : {breadth.get("advances",0)} suben / {breadth.get("declines",0)} bajan  |  Ratio A/D: {ad}  [{breadth.get("signal","")}]')
        L.append(f'  % s/MA50   : {breadth.get("pct_above50",0)}%  |  % s/MA200: {breadth.get("pct_above200",0)}%')
        L.append(f'  52W Highs  : {breadth.get("new_highs",0)}  |  52W Lows: {breadth.get("new_lows",0)}')

    # 3. OPCIONES
    L.append('')
    L.append('3. FLUJO DE OPCIONES')
    L.append('-' * 40)
    calls_dom = [o for o in options if o.get('tipo') == 'CALL']
    puts_dom  = [o for o in options if o.get('tipo') == 'PUT']
    etf_opts  = [o for o in options if o.get('is_etf')]
    if calls_dom: L.append(f'  CALLS dominantes : {" | ".join([o["ticker"] for o in calls_dom[:6]])}')
    if puts_dom:  L.append(f'  PUTS dominantes  : {" | ".join([o["ticker"] for o in puts_dom[:6]])}')
    for tk in ['SPY','QQQ','IWM']:
        o = next((x for x in options if x['ticker']==tk), None)
        if o: L.append(f'  {tk}: {o.get("detail","")}')
    if fg.get('score') is not None:
        s = fg['score']
        fg_lbl = 'MIEDO EXTREMO' if s < 25 else 'MIEDO' if s < 45 else 'NEUTRAL' if s < 55 else 'CODICIA' if s < 75 else 'CODICIA EXTREMA'
        L.append(f'  Fear & Greed : {s:.0f}/100  [{fg_lbl}]')

    # 4. NOTICIAS CLAVE
    L.append('')
    L.append('4. NOTICIAS CLAVE Y CATALIZADORES')
    L.append('-' * 40)
    top_news = sorted([n for n in news if n.get('score',0) >= 4], key=lambda x: x.get('timestamp',0), reverse=True)[:8]
    if top_news:
        for n in top_news:
            score_lbl = '(!!)' if n.get('score',0) >= 7 else '(!) '
            L.append(f'  {score_lbl} [{n.get("source","")}] {n.get("title_en", n.get("title",""))[:85]}')
    else:
        L.append('  Sin noticias de alto impacto en las ultimas horas')

    next_events = [e for e in cal if not e.get('is_past') and e.get('imp_raw') == 'High'][:5]
    if next_events:
        L.append('')
        L.append('  PROXIMOS EVENTOS (Alto Impacto):')
        for e in next_events:
            fcst = f'  Prev: {e.get("previous","")}  Fcst: {e.get("forecast","")}' if e.get('forecast') else ''
            L.append(f'  >> {e.get("date","")} {e.get("time","")} -- {e.get("event","")[:55]}{fcst}')

    # 5. SOCIAL
    L.append('')
    L.append('5. SOCIAL DESTACADO')
    L.append('-' * 40)
    top_social = [p for p in social if p.get('score',0) >= 2][:5]
    if top_social:
        for p in top_social:
            L.append(f'  [{p.get("account","")}] {p.get("text","")[:100]}')
    else:
        L.append('  Sin posts relevantes recientes')

    # 6. TOP ACCIONES
    L.append('')
    L.append('6. TOP MOVIMIENTOS ACCIONES S&P500')
    L.append('-' * 40)
    stocks = m.get('_top_stocks', {})
    if stocks:
        sorted_stocks = sorted(stocks.items(), key=lambda x: abs(x[1].get('pct',0)), reverse=True)[:10]
        for name, d in sorted_stocks:
            arrow = 'UP' if d.get('pct',0) >= 0 else 'DN'
            L.append(f'  [{arrow}] {d.get("sym",""):5s} {name[:18]:18s}: {d.get("pct",0):+.2f}%  ${d.get("price",0):.2f}')

    # 7. CONCLUSION
    L.append('')
    L.append('7. CONCLUSION Y PLAN DE ACCION')
    L.append('-' * 40)

    conclusions = []
    if vix_p > 35:
        conclusions.append('VIX en zona de PANICO (>' + str(int(vix_p)) + '). Historicamente zona de suelo potencial pero confirmacion necesaria.')
    if rsi_val < 25:
        conclusions.append(f'RSI SPX en sobreventa extrema ({rsi_val}). Rebote tecnico probable a corto plazo -- no anadir cortos aqui.')
    if rsi_val > 75:
        conclusions.append(f'RSI SPX en sobrecompra ({rsi_val}). Reducir exposicion larga o ajustar stops.')
    if not above_ma200:
        conclusions.append('SPX por debajo de MA200. Tendencia bajista de largo plazo activa. Favorecer estrategias defensivas.')
    if wti_pct > 3:
        conclusions.append(f'WTI Oil subiendo fuerte ({wti_pct:+.2f}%). Presion inflacionaria. Beneficia a XLE/energia, perjudica al consumidor.')
    if oro_pct > 1:
        conclusions.append(f'Oro subiendo ({oro_pct:+.2f}%). Senal de busqueda de refugio. Correlacion negativa con riesgo.')
    if dxy_pct > 0.5:
        conclusions.append(f'Dolar fuerte (DXY {dxy_pct:+.2f}%). Presion sobre materias primas y emergentes.')
    if b2 and b10:
        spread_val = (b10.get("price",0) or 0) - (b2.get("price",0) or 0)
        if spread_val < 0:
            conclusions.append(f'Curva invertida ({spread_val:+.2f}%). Senal historica de recesion a 12-18 meses.')

    if risk_mode in ('RISK-OFF EXTREMO', 'RISK-OFF FUERTE'):
        conclusions.append('MODO RISK-OFF ACTIVO: reducir exposicion a renta variable, sobreponderar cash/oro/bonos cortos.')
    elif risk_mode == 'RISK-ON' and above_ma200:
        conclusions.append('Mercado en modo RISK-ON con precio sobre MA200. Momentum positivo -- mantener posiciones con stops ajustados.')
    elif risk_mode == 'RISK-ON' and not above_ma200:
        conclusions.append('Señales mixtas: indicadores de corto plazo positivos pero SPX bajo MA200. Operar con cautela y posiciones reducidas.')

    # Add news-based conclusion
    iran_news = [n for n in news if 'iran' in n.get('title_en','').lower() or 'iran' in n.get('title','').lower()]
    tariff_news = [n for n in news if 'tariff' in n.get('title_en','').lower() or 'trade' in n.get('title_en','').lower()]
    fed_news = [n for n in news if 'fed' in n.get('title_en','').lower() or 'powell' in n.get('title_en','').lower() or 'rate' in n.get('title_en','').lower()]

    if iran_news:
        conclusions.append(f'Conflicto Iran activo ({len(iran_news)} noticias). Oil en riesgo al alza, bolsa en riesgo a la baja. Vigilar Strait of Hormuz.')
    if tariff_news:
        conclusions.append(f'Noticias de aranceles ({len(tariff_news)}). Impacto en exportadoras, tech con supply chain en Asia.')
    if fed_news:
        conclusions.append(f'Noticias Fed/tipos ({len(fed_news)}). Vigilar declaraciones para proxima decision de tipos.')

    if not conclusions:
        conclusions.append(f'Mercado en consolidacion. S&P {spx_pct:+.2f}%, VIX {vix_p:.1f}. Sin senal clara -- esperar confirmacion antes de actuar.')

    for c in conclusions:
        L.append(f'  >> {c}')

    L.append('')
    L.append('=' * 60)
    L.append(f'  Generado: {now}  |  Datos actualizados cada 30s')

    return '\n'.join(L)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()
    def do_POST(self):
        self.send_response(404); self.end_headers()
    def do_GET(self):
        if self.path.startswith("/api/analyze"):
            try:
                with LOCK:
                    analysis = generate_local_analysis(CACHE)
                # Clean any remaining non-ASCII chars
                analysis_clean = analysis.encode('ascii', 'replace').decode('ascii')
                payload = json.dumps({"analysis": analysis_clean})
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Access-Control-Allow-Origin","*")
                self.end_headers()
                self.wfile.write(payload.encode('ascii'))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type","application/json")
                self.send_header("Access-Control-Allow-Origin","*")
                self.end_headers()
                self.wfile.write(json.dumps({"error":str(e)}).encode())
        elif self.path=="/api/data":
            with LOCK: payload=json.dumps(CACHE,ensure_ascii=False,default=str)
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers(); self.wfile.write(payload.encode())
        elif self.path=="/api/refresh":
            threading.Thread(target=update_all,daemon=True).start()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.end_headers(); self.wfile.write(b'{"status":"ok"}')
        elif self.path=="/manifest.json":
            try:
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.end_headers()
                self.wfile.write(MANIFEST.read_bytes())
            except: self.send_response(404); self.end_headers()
        elif self.path=="/favicon.png":
            try:
                self.send_response(200)
                self.send_header("Content-Type","image/png")
                self.end_headers()
                self.wfile.write(FAVICON.read_bytes())
            except: self.send_response(404); self.end_headers()
        elif self.path=="/icon-192.png":
            try:
                self.send_response(200)
                self.send_header("Content-Type","image/png")
                self.end_headers()
                self.wfile.write(ICON192.read_bytes())
            except: self.send_response(404); self.end_headers()
        elif self.path=="/icon-512.png":
            try:
                self.send_response(200)
                self.send_header("Content-Type","image/png")
                self.end_headers()
                self.wfile.write(ICON512.read_bytes())
            except: self.send_response(404); self.end_headers()
        elif self.path=="/sw.js":
            try:
                self.send_response(200)
                self.send_header("Content-Type","application/javascript")
                self.send_header("Service-Worker-Allowed","/")
                self.send_header("Cache-Control","no-cache")
                self.end_headers()
                if SW_JS.exists():
                    self.wfile.write(SW_JS.read_bytes())
                else:
                    self.wfile.write(b"// sw.js not found")
            except:
                self.send_response(404); self.end_headers()
        elif self.path in ("/","/dashboard","/mobile"):
            try:
                ua=self.headers.get("User-Agent","").lower()
                is_mobile=any(x in ua for x in ["iphone","android","mobile","ipad"])
                use_mobile=is_mobile or self.path=="/mobile"
                html_file=HTML_MOBILE if (use_mobile and HTML_MOBILE.exists()) else HTML
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.end_headers(); self.wfile.write(html_file.read_bytes())
            except:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"Pon dashboard.html en la misma carpeta")
        else:
            self.send_response(404); self.end_headers()

if __name__=="__main__":
    PORT=int(os.environ.get("PORT", 8080))
    print("="*55)
    print("  MARKET INTELLIGENCE — Bloomberg Terminal")
    print("="*55)
    print(f"\n  Abre: http://localhost:{PORT}")
    print("  Actualiza cada 60 segundos | Ctrl+C para detener\n")
    print("  Cargando datos (~20 seg)...")
    _load_prev_closes()
    update_all()
    threading.Thread(target=bg_updater,daemon=True).start()
    import webbrowser
    if not os.environ.get("RENDER"): threading.Timer(1.5,lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    server=HTTPServer(("0.0.0.0",PORT),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nDetenido.")
