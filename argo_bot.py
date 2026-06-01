import yfinance as yf
import asyncio
import logging
import os
import threading
from datetime import datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from flask import Flask
from requests.adapters import HTTPAdapter
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from urllib3.util.retry import Retry

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# =========================
# MINI SERVER HTTP PER RENDER
# =========================
web_app = Flask(__name__)


@web_app.get("/")
def home():
    return "ARGO bot is running", 200


@web_app.get("/health")
def health():
    return "ok", 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


# =========================
# CONFIGURAZIONE
# =========================
TOKEN = os.environ["TOKEN"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]

# 5 minuti: più sostenibile lato API
UPDATE_INTERVAL_SECONDS = 300
MARKET_CACHE_SECONDS = 300

SYM_SPY = "SPY"

ITALY_TZ = ZoneInfo("Europe/Rome")
NY_TZ = ZoneInfo("America/New_York")

# =========================
# SESSION HTTP ROBUSTA
# =========================
SESSION = requests.Session()

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1.2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)

adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
    "Accept": "application/json,text/plain,*/*",
}

# =========================
# CACHE
# =========================
_macro_cache: Dict[str, Any] = {"date": None, "events": []}
_market_cache: Dict[str, Any] = {"ts": None, "data": None}
_vix_cache: Dict[str, Any] = {"ts": None, "value": None}


# =========================
# TIME HELPERS
# =========================
def italy_now() -> datetime:
    return datetime.now(ITALY_TZ)


def ny_now() -> datetime:
    return datetime.now(NY_TZ)


def is_us_market_open_now() -> bool:
    """
    Mercato cash USA: lun-ven, 09:30-16:00 America/New_York.
    Serve solo per limitare il job automatico; i comandi manuali restano disponibili sempre.
    """
    now = ny_now()
    if now.weekday() >= 5:
        return False
    return time(9, 30) <= now.time() <= time(16, 0)


# =========================
# FETCH EVENTI MACRO (ForexFactory)
# =========================
def fetch_macro_events() -> list:
    global _macro_cache

    today_str = italy_now().strftime("%Y-%m-%d")

    if _macro_cache["date"] == today_str:
        return _macro_cache["events"]

    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = SESSION.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(10, 20),
        )
        r.raise_for_status()
        data = r.json()

        events = []
        for ev in data:
            if ev.get("country", "").upper() != "USD":
                continue
            if ev.get("impact", "").lower() not in ("high", "medium"):
                continue

            raw_date = ev.get("date", "")
            if not raw_date:
                continue

            try:
                dt_utc = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S%z")
                dt_it = dt_utc.astimezone(ITALY_TZ)
                if dt_it.strftime("%Y-%m-%d") != today_str:
                    continue
                time_str = dt_it.strftime("%H:%M")
            except Exception:
                continue

            events.append(
                {
                    "name": ev.get("title", "Evento USD"),
                    "time": time_str,
                    "impact": ev.get("impact", "").lower(),
                }
            )

        events.sort(key=lambda e: e["time"])
        _macro_cache = {"date": today_str, "events": events}
        logger.info("📅 Eventi macro caricati: %s per oggi (%s)", len(events), today_str)
        return events

    except Exception as e:
        logger.exception("❌ Errore fetch eventi macro: %s", e)
        _macro_cache = {"date": today_str, "events": []}
        return []


def parse_event_time(hhmm: str) -> Optional[datetime]:
    try:
        hh, mm = hhmm.split(":")
        now = italy_now()
        return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        return None


def next_event_within(minutes: int = 15):
    now = italy_now()
    for ev in fetch_macro_events():
        dt = parse_event_time(ev["time"])
        if not dt:
            continue
        delta = (dt - now).total_seconds() / 60
        if 0 <= delta <= minutes:
            return {
                "name": ev["name"],
                "time": ev["time"],
                "minutes": int(round(delta)),
                "impact": ev.get("impact", ""),
            }
    return None


def format_events_list() -> str:
    events = fetch_macro_events()
    if not events:
        return "Nessun evento macro USD oggi"

    icon = {"high": "🔴", "medium": "🟡"}
    return "\n".join(
        f"{icon.get(ev.get('impact', ''), '⚪')} {ev['time']} — {ev['name']}"
        for ev in events
    )


# =========================
# TWELVEDATA (SPY)
# =========================
def td_get(endpoint: str, params: dict) -> dict:
    headers = dict(HEADERS)
    headers["Authorization"] = f"apikey {TWELVEDATA_API_KEY}"

    try:
        r = SESSION.get(
            f"https://api.twelvedata.com/{endpoint}",
            params=params,
            headers=headers,
            timeout=(15, 30),
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        logger.exception("❌ Errore rete TwelveData [%s]: %s", endpoint, e)
        raise RuntimeError(f"Errore rete TwelveData: {e}")

    if isinstance(data, dict) and data.get("status") == "error":
        msg = data.get("message", "errore sconosciuto")
        logger.error("❌ Errore TwelveData [%s]: %s", endpoint, msg)
        raise RuntimeError(f"Errore TwelveData: {msg}")

    return data


def _safe_float(v, fallback=None):
    try:
        if v in (None, "", "N/A"):
            return fallback
        return float(v)
    except Exception:
        return fallback


def td_quote(symbol: str) -> Dict[str, float]:
    data = td_get("quote", {"symbol": symbol})
    return {
        "open": _safe_float(data.get("open")),
        "high": _safe_float(data.get("high")),
        "low": _safe_float(data.get("low")),
        "close": _safe_float(data.get("close")),
        "volume": _safe_float(data.get("volume"), 0.0),
    }


def td_history(symbol: str, outputsize: int = 5) -> list:
    data = td_get(
        "time_series",
        {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": outputsize,
            "order": "ASC",
        },
    )

    out = []
    for row in data.get("values", []):
        try:
            out.append(
                {
                    "date": row.get("datetime", ""),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0),
                }
            )
        except Exception:
            continue

    return out


# =========================
# YAHOO FINANCE (VIX SPOT)
# =========================

def get_vix_spot(force_refresh: bool = False) -> float:
    """
    Legge il VIX spot da Yahoo Finance tramite yfinance.
    Tiene una piccola cache per evitare richieste ravvicinate.
    """
    global _vix_cache

    now_dt = italy_now()
    if not force_refresh and _vix_cache["ts"] and _vix_cache["value"] is not None:
        age = (now_dt - _vix_cache["ts"]).total_seconds()
        if age < MARKET_CACHE_SECONDS:
            return float(_vix_cache["value"])

    try:
        ticker = yf.Ticker("^VIX")

        # Primo tentativo: fast_info
        price = None
        try:
            fi = getattr(ticker, "fast_info", None)
            if fi:
                price = fi.get("lastPrice") or fi.get("last_price")
        except Exception:
            pass

        # Fallback: history intraday breve
        if price in (None, "", "N/A"):
            hist = ticker.history(period="1d", interval="1m")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].dropna().iloc[-1])

        # Fallback finale: history giornaliera
        if price in (None, "", "N/A"):
            hist = ticker.history(period="5d", interval="1d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].dropna().iloc[-1])

        if price in (None, "", "N/A"):
            raise RuntimeError("Prezzo VIX non disponibile da yfinance")

        value = float(price)
        _vix_cache = {"ts": now_dt, "value": value}
        return value

    except Exception as e:
        logger.exception("❌ Errore yfinance VIX: %s", e)
        raise RuntimeError(f"Errore Yahoo/yfinance VIX: {e}")

# =========================
# SNAPSHOT MERCATO
# =========================
def fetch_market_snapshot() -> Dict[str, Any]:
    global _market_cache

    now_dt = italy_now()

    if _market_cache["ts"] and _market_cache["data"]:
        age = (now_dt - _market_cache["ts"]).total_seconds()
        if age < MARKET_CACHE_SECONDS:
            return _market_cache["data"]

    spy = td_quote(SYM_SPY)

    try:
        spy_hist = td_history(SYM_SPY, outputsize=5)
    except Exception as e:
        logger.warning("⚠️ Storico SPY non disponibile -> %s", e)
        spy_hist = []

    # VIX spot vero da Yahoo Finance
    try:
        vix = get_vix_spot()
    except Exception as e:
        logger.warning("⚠️ VIX Yahoo non disponibile, uso fallback 18.0 -> %s", e)
        vix = 18.0

    price = spy.get("close")
    if price is None:
        raise RuntimeError(
            "⚠️ Prezzo non disponibile.\n"
            "Il mercato USA potrebbe essere chiuso\n"
            "(weekend, festivi) oppure TwelveData non ha ancora aggiornato i dati."
        )

    open_ = spy.get("open") or price
    intraday_high = spy.get("high") or price
    intraday_low = spy.get("low") or price
    vwap = (open_ + intraday_high + intraday_low + price) / 4.0

    if len(spy_hist) >= 2:
        prev_close = spy_hist[-2]["close"]
    elif len(spy_hist) == 1:
        prev_close = spy_hist[-1]["close"]
    else:
        prev_close = open_

    result = {
        "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "price": price,
        "open": open_,
        "prev_close": prev_close,
        "vwap": vwap,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "vix": vix,
    }

    _market_cache = {"ts": now_dt, "data": result}
    return result


# =========================
# LOGICA ARGO
# =========================
def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, int(round(v))))


def label_from_score(s):
    if s <= 20:
        return "molto debole"
    if s <= 40:
        return "debole"
    if s <= 60:
        return "moderato"
    if s <= 75:
        return "forte"
    return "molto forte"


def compute_argo(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    price = snapshot["price"]
    open_ = snapshot["open"]
    prev_close = snapshot["prev_close"]
    vwap = snapshot["vwap"]
    vix = snapshot["vix"]

    pivot = round((open_ + prev_close) / 2, 2)
    magnet = round(vwap, 2)
    down_break = round(pivot * 0.995, 2)
    structural_break = round(pivot * 0.990, 2)

    dist_pivot_pct = ((price - pivot) / pivot) * 100
    dist_magnet_pct = ((price - magnet) / magnet) * 100
    intraday_range_pct = (
        (snapshot["intraday_high"] - snapshot["intraday_low"]) / max(price, 1)
    ) * 100

    if abs(dist_magnet_pct) <= 0.20 and vix < 18.0:
        regime, structure = "LONG GAMMA", "mercato vincolato"
    elif price < down_break or (abs(dist_magnet_pct) > 0.45 and vix >= 18.0):
        regime, structure = "SHORT GAMMA", "mercato più libero"
    else:
        regime, structure = "NEUTRO", "struttura ibrida"

    pullback_absorbed = price > pivot and price > magnet * 0.998
    pivot_reclaimed = price > pivot and dist_pivot_pct > 0.08
    deterioration_active = price < down_break
    structural_break_active = price < structural_break
    magnet_chop = abs(dist_magnet_pct) <= 0.10 and intraday_range_pct < 0.90

    bullish = bearish = no_edge = 0
    confidence = 70

    if regime == "LONG GAMMA":
        bullish += 20
        bearish += 10
        no_edge += 15
    elif regime == "SHORT GAMMA":
        bullish += 12
        bearish += 22
        no_edge += 8
    else:
        bullish += 10
        bearish += 10
        no_edge += 25

    if price > pivot:
        bullish += 15
        bearish -= 5
    else:
        bearish += 15
        bullish -= 5

    if abs(dist_magnet_pct) <= 0.12:
        no_edge += 18
        bullish -= 5
        bearish -= 5

    if deterioration_active:
        bearish += 20
        bullish -= 10

    if structural_break_active:
        bearish += 25
        no_edge -= 5

    if pullback_absorbed:
        bullish += 10

    if pivot_reclaimed:
        bullish += 15

    if deterioration_active and abs(dist_magnet_pct) > 0.35:
        bearish += 12

    if magnet_chop:
        no_edge += 20

    bull_trigger = pivot_reclaimed and price > magnet * 0.998
    bear_trigger = deterioration_active and not pivot_reclaimed

    if bull_trigger:
        bullish += 15
    if bear_trigger:
        bearish += 15

    next_ev = next_event_within(15)
    if next_ev:
        no_edge += 15
        confidence -= 15

    bullish = clamp(bullish)
    bearish = clamp(bearish)
    no_edge = clamp(no_edge)
    confidence = clamp(confidence)

    zone = "zona sporca"
    if price > pivot:
        zone = "sopra pivot"
    if price < down_break:
        zone = "deterioramento attivo"
    if price < structural_break:
        zone = "break strutturale"
    if abs(price - magnet) / max(magnet, 1) < 0.001:
        zone = "zona magnete"

    return {
        "timestamp": snapshot["timestamp"],
        "price": round(price, 2),
        "vix": round(vix, 2),
        "pivot": pivot,
        "magnet": magnet,
        "down_break": down_break,
        "structural_break": structural_break,
        "regime": regime,
        "structure": structure,
        "zone": zone,
        "bull_score": bullish,
        "bear_score": bearish,
        "no_edge_score": no_edge,
        "confidence_score": confidence,
        "bull_label": label_from_score(bullish),
        "bear_label": label_from_score(bearish),
        "no_edge_label": label_from_score(no_edge),
        "confidence_label": label_from_score(confidence),
        "bull_trigger_active": bull_trigger,
        "bear_trigger_active": bear_trigger,
        "next_event": next_ev,
    }


# =========================
# FORMATTERS
# =========================
def _impact_icon(impact: str) -> str:
    return {"high": "🔴", "medium": "🟡"}.get(impact, "⚪")


def format_status(d):
    ev_row = ""
    if d.get("next_event"):
        ev = d["next_event"]
        ev_row = (
            f"\n⚠️ Evento tra {ev['minutes']} min: "
            f"{_impact_icon(ev['impact'])} {ev['name']} ({ev['time']})"
        )

    return (
        f"🧭 STATO ARGO\n\n"
        f"Prezzo: {d['price']}\n"
        f"Regime: {d['regime']}\n"
        f"Struttura: {d['structure']}\n"
        f"Zona: {d['zone']}\n\n"
        f"📊 Score\n"
        f"Bullish:    {d['bull_score']}/100 ({d['bull_label']})\n"
        f"Bearish:    {d['bear_score']}/100 ({d['bear_label']})\n"
        f"No Edge:    {d['no_edge_score']}/100 ({d['no_edge_label']})\n"
        f"Confidence: {d['confidence_score']}/100 ({d['confidence_label']})"
        f"{ev_row}\n\n"
        f"Aggiornato: {d['timestamp']}"
    )


def format_postit(d):
    ev_row = ""
    if d.get("next_event"):
        ev = d["next_event"]
        ev_row = (
            f"\n{_impact_icon(ev['impact'])} EVENTO → "
            f"{ev['name']} {ev['time']} (tra {ev['minutes']} min)"
        )

    return (
        f"📝 POST-IT ARGO\n\n"
        f"PREZZO  → {d['price']}\n"
        f"MAGNETE → {d['magnet']}\n"
        f"PIVOT   → {d['pivot']}\n\n"
        f"BULL    → {d['bull_score']}\n"
        f"BEAR    → {d['bear_score']}\n"
        f"NO EDGE → {d['no_edge_score']}\n"
        f"CONF    → {d['confidence_score']}\n\n"
        f"> PIVOT → probabilità rialzista\n"
        f"{d['pivot']}–{d['down_break']} → zona sporca\n"
        f"< {d['down_break']} → rischio ribassista\n"
        f"< {d['structural_break']} → break strutturale\n"
        f"\nTRIGGER:\n- reclaim pivot\n- perdita + no reclaim"
        f"{ev_row}"
    )


def format_briefing(d):
    return (
        f"🧭 ARGO BRIEFING\n\n"
        f"Regime: {d['regime']}\n"
        f"Struttura: {d['structure']}\n"
        f"Prezzo: {d['price']}  VIX: {d['vix']}\n\n"
        f"📍 LIVELLI\n"
        f"Pivot:             {d['pivot']}\n"
        f"Magnete (VWAP):    {d['magnet']}\n"
        f"Break downside:    {d['down_break']}\n"
        f"Break strutturale: {d['structural_break']}\n\n"
        f"📊 SCORE\n"
        f"Bullish:    {d['bull_score']}/100 ({d['bull_label']})\n"
        f"Bearish:    {d['bear_score']}/100 ({d['bear_label']})\n"
        f"No Edge:    {d['no_edge_score']}/100 ({d['no_edge_label']})\n"
        f"Confidence: {d['confidence_score']}/100 ({d['confidence_label']})\n\n"
        f"⚡ TRIGGER\n"
        f"- rialzista:  {'SI ✅' if d['bull_trigger_active'] else 'NO'}\n"
        f"- ribassista: {'SI ✅' if d['bear_trigger_active'] else 'NO'}\n\n"
        f"📅 EVENTI MACRO OGGI\n{format_events_list()}"
    )


def build_alert(last, current):
    if last is None:
        return None

    if current["regime"] != last.get("regime"):
        return (
            f"🚨 REGIME SHIFT: {last.get('regime')} → {current['regime']}\n"
            f"Prezzo: {current['price']}  Pivot: {current['pivot']}"
        )

    if last.get("price", 0) <= last.get("pivot", 0) and current["price"] > current["pivot"]:
        return f"⚡ RECLAIM PIVOT {current['pivot']}  Prezzo: {current['price']}"

    if last.get("price", 0) >= last.get("pivot", 0) and current["price"] < current["pivot"]:
        return f"🚨 LOSS OF PIVOT {current['pivot']}  Prezzo: {current['price']}"

    if last.get("price", 999999) >= last.get("down_break", 999999) and current["price"] < current["down_break"]:
        return f"🚨 DETERIORAMENTO — rotto {current['down_break']}  Prezzo: {current['price']}"

    if last.get("price", 999999) >= last.get("structural_break", 999999) and current["price"] < current["structural_break"]:
        return f"🚨 BREAK STRUTTURALE — violato {current['structural_break']} Prezzo: {current['price']}"

    ev = current.get("next_event")
    if ev:
        key = f"{ev['name']}@{ev['time']}"
        if last.get("next_event_key") != key:
            current["next_event_key"] = key
            return (
                f"📅 EVENTO TRA {ev['minutes']} MIN\n"
                f"{_impact_icon(ev['impact'])} {ev['name']} — {ev['time']}\n\n"
                f"Bull: {current['bull_score']}  "
                f"Bear: {current['bear_score']}  "
                f"NE: {current['no_edge_score']}  "
                f"Conf: {current['confidence_score']}"
            )

    return None


# =========================
# ASYNC
# =========================
async def analyze_now():
    snap = await asyncio.to_thread(fetch_market_snapshot)
    return compute_argo(snap)


# =========================
# COMANDI TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("📩 /start ricevuto da chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "✅ ARGO Bot attivo!\n\n"
        "/status   — snapshot\n"
        "/briefing — analisi completa + eventi macro\n"
        "/postit   — livelli chiave\n"
        "/eventi   — calendario macro del giorno\n"
        "/vix      — solo VIX spot\n"
        "/watchon  — alert automatici ON\n"
        "/watchoff — alert automatici OFF\n"
        "/ping     — test rapido"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("📩 /ping ricevuto da chat_id=%s", update.effective_chat.id)
    await update.message.reply_text("pong 🟢")


async def vix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero VIX…")
    try:
        price = await asyncio.to_thread(get_vix_spot, True)
        now_str = italy_now().strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(
            f"📈 VIX SPOT\n\n"
            f"Valore: {price:.2f}\n"
            f"Aggiornato: {now_str}"
        )
    except Exception as e:
        logger.exception("❌ Errore /vix: %s", e)
        await update.message.reply_text(f"❌ Errore VIX: {e}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero dati…")
    try:
        result = await analyze_now()
        await update.message.reply_text(format_status(result))
    except Exception as e:
        logger.exception("❌ Errore /status: %s", e)
        await update.message.reply_text(f"❌ {e}")


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Elaboro briefing…")
    try:
        result = await analyze_now()
        await update.message.reply_text(format_briefing(result))
    except Exception as e:
        logger.exception("❌ Errore /briefing: %s", e)
        await update.message.reply_text(f"❌ {e}")


async def postit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero livelli…")
    try:
        result = await analyze_now()
        await update.message.reply_text(format_postit(result))
    except Exception as e:
        logger.exception("❌ Errore /postit: %s", e)
        await update.message.reply_text(f"❌ {e}")


async def eventi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        evs = await asyncio.to_thread(fetch_macro_events)
        if not evs:
            await update.message.reply_text(
                "📅 Nessun evento macro USD oggi\n"
                "(oppure feed temporaneamente non disponibile)"
            )
            return

        icon = {"high": "🔴", "medium": "🟡"}
        lines = ["📅 EVENTI MACRO USD OGGI\n"]
        for ev in evs:
            lines.append(f"{icon.get(ev.get('impact', ''), '⚪')} {ev['time']} — {ev['name']}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception("❌ Errore /eventi: %s", e)
        await update.message.reply_text(f"❌ Errore eventi: {e}")


async def watchon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.setdefault("watch_chats", set()).add(update.effective_chat.id)
    await update.message.reply_text(f"✅ Watch ON — controllo ogni {UPDATE_INTERVAL_SECONDS}s")


async def watchoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.setdefault("watch_chats", set()).discard(update.effective_chat.id)
    await update.message.reply_text("⏸️ Watch OFF")


# =========================
# JOB AUTOMATICO
# =========================
async def market_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    try:
        if not app.bot_data.get("watch_chats"):
            return

        if not is_us_market_open_now():
            return

        current = await analyze_now()
        alert = build_alert(app.bot_data.get("last_global_state"), current)
        app.bot_data["last_global_state"] = current.copy()

        if alert:
            for cid in list(app.bot_data["watch_chats"]):
                try:
                    await app.bot.send_message(chat_id=cid, text=alert)
                except Exception as e:
                    logger.exception("❌ Errore invio alert a %s: %s", cid, e)

    except Exception as e:
        logger.exception("❌ Errore market_job: %s", e)


async def post_init(app):
    logger.info("🚀 post_init avviato")

    app.bot_data.setdefault("watch_chats", set())
    app.bot_data.setdefault("last_global_state", None)

    try:
        me = await app.bot.get_me()
        logger.info("✅ Bot Telegram connesso: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.exception("❌ Errore get_me in post_init: %s", e)

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook rimosso / polling pronto")
    except Exception as e:
        logger.exception("❌ Errore delete_webhook: %s", e)

    try:
        await asyncio.to_thread(fetch_macro_events)
        logger.info("✅ Eventi macro precaricati")
    except Exception as e:
        logger.exception("⚠️ Errore preload eventi macro: %s", e)

    try:
        app.job_queue.run_repeating(
            market_job,
            interval=UPDATE_INTERVAL_SECONDS,
            first=UPDATE_INTERVAL_SECONDS,
        )
        logger.info("✅ Job ARGO avviato")
    except Exception as e:
        logger.exception("❌ Errore avvio job queue: %s", e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("❌ Eccezione non gestita:", exc_info=context.error)


# =========================
# MAIN
# =========================
def main():
    logger.info("🟢 Avvio ARGO bot...")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )

    handlers = [
        ("start", start),
        ("ping", ping),
        ("vix", vix),
        ("status", status),
        ("briefing", briefing),
        ("postit", postit),
        ("eventi", eventi),
        ("watchon", watchon),
        ("watchoff", watchoff),
    ]

    for cmd, fn in handlers:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_error_handler(error_handler)

    threading.Thread(target=run_web_server, daemon=True).start()

    logger.info("🤖 Bot ARGO partito… entro in polling")

    app.run_polling(
        poll_interval=1.0,
        timeout=30,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
