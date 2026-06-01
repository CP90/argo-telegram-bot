import asyncio
import csv
import io
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# CONFIGURAZIONE
# =========================
TOKEN = os.environ["TOKEN"]
UPDATE_INTERVAL_SECONDS = 60

SYM_SPY = "spy.us"
SYM_VIX = "^vix"

ITALY_TZ = timezone(timedelta(hours=2))  # CEST (ora legale); usa hours=1 in inverno

# =========================
# FETCH EVENTI MACRO (ForexFactory)
# =========================
_macro_cache: Dict[str, Any] = {"date": None, "events": []}

def fetch_macro_events() -> list:
    """
    Recupera eventi macro USD (high/medium impact) dal calendario ForexFactory.
    Risultato cachato per tutta la giornata per non fare troppe richieste.
    """
    global _macro_cache
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Usa cache se già recuperato oggi
    if _macro_cache["date"] == today_str:
        return _macro_cache["events"]

    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()

        events = []
        for ev in data:
            if ev.get("country", "").upper() != "USD":
                continue
            if ev.get("impact", "").lower() not in ("high", "medium"):
                continue

            raw_date = ev.get("date", "")
            date_str = raw_date[:10]
            if date_str != today_str:
                continue

            try:
                dt_utc = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S%z")
                dt_it  = dt_utc.astimezone(ITALY_TZ)
                time_str = dt_it.strftime("%H:%M")
            except Exception:
                continue

            events.append({
                "name":   ev.get("title", "Evento USD"),
                "time":   time_str,
                "impact": ev.get("impact", ""),
            })

        # Ordina per orario
        events.sort(key=lambda e: e["time"])

        _macro_cache = {"date": today_str, "events": events}
        print(f"📅 Eventi macro caricati: {len(events)} per oggi ({today_str})")
        return events

    except Exception as e:
        print(f"Errore fetch eventi macro: {e}")
        _macro_cache = {"date": today_str, "events": []}
        return []

def parse_event_time(hhmm: str):
    try:
        hh, mm = hhmm.split(":")
        now = datetime.now()
        return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        return None

def next_event_within(minutes: int = 15):
    events = fetch_macro_events()
    now = datetime.now()
    for ev in events:
        dt = parse_event_time(ev["time"])
        if not dt:
            continue
        delta = (dt - now).total_seconds() / 60
        if 0 <= delta <= minutes:
            return {
                "name":    ev["name"],
                "time":    ev["time"],
                "minutes": int(round(delta)),
                "impact":  ev.get("impact", ""),
            }
    return None

def format_events_list() -> str:
    """Formatta tutti gli eventi del giorno per il briefing."""
    events = fetch_macro_events()
    if not events:
        return "Nessun evento macro USD oggi"
    icon = {"high": "🔴", "medium": "🟡"}
    lines = []
    for ev in events:
        i = icon.get(ev.get("impact", ""), "⚪")
        lines.append(f"{i} {ev['time']} — {ev['name']}")
    return "\n".join(lines)

# =========================
# FETCH DA STOOQ
# =========================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,*/*",
}

def stooq_quote(symbol: str) -> Dict[str, float]:
    url = "https://stooq.com/q/l/"
    params = {"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    if not rows:
        raise RuntimeError(f"Nessun dato Stooq per {symbol}")
    row = rows[0]

    def safe(val, fallback=None):
        try:
            v = val.strip()
            return float(v) if v not in ("N/D", "", "-", "N/A") else fallback
        except Exception:
            return fallback

    return {
        "open":   safe(row.get("Open",   "N/D")),
        "high":   safe(row.get("High",   "N/D")),
        "low":    safe(row.get("Low",    "N/D")),
        "close":  safe(row.get("Close",  "N/D")),
        "volume": safe(row.get("Volume", "N/D"), 0.0),
    }

def stooq_history(symbol: str, days: int = 5) -> list:
    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "i": "d"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    if not rows:
        raise RuntimeError(f"Storico Stooq vuoto per {symbol}")
    result = []
    for row in rows[-days:]:
        try:
            result.append({
                "date":   row.get("Date", ""),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
            })
        except Exception:
            continue
    return result

# =========================
# SNAPSHOT MERCATO
# =========================
def fetch_market_snapshot() -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    spy      = stooq_quote(SYM_SPY)
    spy_hist = stooq_history(SYM_SPY, days=5)
    vix_data = stooq_quote(SYM_VIX)

    price = spy.get("close")
    if price is None:
        raise RuntimeError(
            "⚠️ Prezzo non disponibile.\n"
            "Il mercato USA potrebbe essere chiuso\n"
            "(weekend, festivi) oppure Stooq non ha\n"
            "ancora aggiornato i dati.\n"
            "Riprova durante gli orari di borsa USA\n"
            "(15:30–22:00 ora italiana)."
        )

    open_         = spy.get("open")  or price
    intraday_high = spy.get("high")  or price
    intraday_low  = spy.get("low")   or price
    vwap          = (open_ + intraday_high + intraday_low + price) / 4.0

    prev_close = None
    if len(spy_hist) >= 2:
        prev_close = spy_hist[-2]["close"]
    elif len(spy_hist) == 1:
        prev_close = spy_hist[-1]["close"]
    else:
        prev_close = open_

    vix = vix_data.get("close") or 18.0

    return {
        "timestamp":     now,
        "price":         price,
        "open":          open_,
        "prev_close":    prev_close,
        "vwap":          vwap,
        "intraday_high": intraday_high,
        "intraday_low":  intraday_low,
        "vix":           vix,
    }

# =========================
# LOGICA ARGO
# =========================
def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, int(round(v))))

def label_from_score(s):
    if s <= 20: return "molto debole"
    if s <= 40: return "debole"
    if s <= 60: return "moderato"
    if s <= 75: return "forte"
    return "molto forte"

def compute_argo(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    price      = snapshot["price"]
    open_      = snapshot["open"]
    prev_close = snapshot["prev_close"]
    vwap       = snapshot["vwap"]
    vix        = snapshot["vix"]

    pivot            = round((open_ + prev_close) / 2, 2)
    magnet           = round(vwap, 2)
    down_break       = round(pivot * 0.995, 2)
    structural_break = round(pivot * 0.990, 2)

    dist_pivot_pct     = ((price - pivot)  / pivot)  * 100
    dist_magnet_pct    = ((price - magnet) / magnet) * 100
    intraday_range_pct = (
        (snapshot["intraday_high"] - snapshot["intraday_low"]) / max(price, 1)
    ) * 100

    if abs(dist_magnet_pct) <= 0.20 and vix < 18.0:
        regime, structure = "LONG GAMMA",  "mercato vincolato"
    elif price < down_break or (abs(dist_magnet_pct) > 0.45 and vix >= 18.0):
        regime, structure = "SHORT GAMMA", "mercato più libero"
    else:
        regime, structure = "NEUTRO",      "struttura ibrida"

    pullback_absorbed       = price > pivot and price > magnet * 0.998
    pivot_reclaimed         = price > pivot and dist_pivot_pct > 0.08
    deterioration_active    = price < down_break
    structural_break_active = price < structural_break
    magnet_chop             = abs(dist_magnet_pct) <= 0.10 and intraday_range_pct < 0.90

    bullish = bearish = no_edge = 0
    confidence = 70

    if regime == "LONG GAMMA":
        bullish += 20; bearish += 10; no_edge += 15
    elif regime == "SHORT GAMMA":
        bullish += 12; bearish += 22; no_edge +=  8
    else:
        bullish += 10; bearish += 10; no_edge += 25

    if price > pivot:
        bullish += 15; bearish -= 5
    else:
        bearish += 15; bullish -= 5

    if abs(dist_magnet_pct) <= 0.12:
        no_edge += 18; bullish -= 5; bearish -= 5

    if deterioration_active:
        bearish += 20; bullish -= 10
    if structural_break_active:
        bearish += 25; no_edge -= 5
    if pullback_absorbed:   bullish += 10
    if pivot_reclaimed:     bullish += 15
    if deterioration_active and abs(dist_magnet_pct) > 0.35:
        bearish += 12
    if magnet_chop:         no_edge += 20

    bull_trigger = pivot_reclaimed and price > magnet * 0.998
    bear_trigger = deterioration_active and not pivot_reclaimed

    if bull_trigger: bullish += 15
    if bear_trigger: bearish += 15

    next_ev = next_event_within(15)
    if next_ev:
        no_edge    += 15
        confidence -= 15

    bullish    = clamp(bullish)
    bearish    = clamp(bearish)
    no_edge    = clamp(no_edge)
    confidence = clamp(confidence)

    zone = "zona sporca"
    if price > pivot:                                 zone = "sopra pivot"
    if price < down_break:                            zone = "deterioramento attivo"
    if price < structural_break:                      zone = "break strutturale"
    if abs(price - magnet) / max(magnet, 1) < 0.001: zone = "zona magnete"

    return {
        "timestamp": snapshot["timestamp"],
        "price": round(price, 2), "vix": round(vix, 2),
        "pivot": pivot, "magnet": magnet,
        "down_break": down_break, "structural_break": structural_break,
        "regime": regime, "structure": structure, "zone": zone,
        "bull_score": bullish, "bear_score": bearish,
        "no_edge_score": no_edge, "confidence_score": confidence,
        "bull_label":       label_from_score(bullish),
        "bear_label":       label_from_score(bearish),
        "no_edge_label":    label_from_score(no_edge),
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
        ev_row = (f"\n⚠️ Evento tra {ev['minutes']} min: "
                  f"{_impact_icon(ev['impact'])} {ev['name']} ({ev['time']})")
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
        ev_row = (f"\n{_impact_icon(ev['impact'])} EVENTO → "
                  f"{ev['name']} {ev['time']} (tra {ev['minutes']} min)")
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
        f"Regime: {d['regime']}\nStruttura: {d['structure']}\n"
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
    if last is None: return None
    if current["regime"] != last.get("regime"):
        return (f"🚨 REGIME SHIFT: {last.get('regime')} → {current['regime']}\n"
                f"Prezzo: {current['price']}  Pivot: {current['pivot']}")
    if last.get("price", 0) <= last.get("pivot", 0) and current["price"] > current["pivot"]:
        return f"⚡ RECLAIM PIVOT {current['pivot']}  Prezzo: {current['price']}"
    if last.get("price", 0) >= last.get("pivot", 0) and current["price"] < current["pivot"]:
        return f"🚨 LOSS OF PIVOT {current['pivot']}  Prezzo: {current['price']}"
    if (last.get("price", 999999) >= last.get("down_break", 999999)
            and current["price"] < current["down_break"]):
        return f"🚨 DETERIORAMENTO — rotto {current['down_break']}  Prezzo: {current['price']}"
    if (last.get("price", 999999) >= last.get("structural_break", 999999)
            and current["price"] < current["structural_break"]):
        return (f"🚨 BREAK STRUTTURALE — violato {current['structural_break']}"
                f"  Prezzo: {current['price']}")
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
    await update.message.reply_text(
        "✅ ARGO Bot attivo!\n\n"
        "/status   — snapshot\n"
        "/briefing — analisi completa + eventi macro\n"
        "/postit   — livelli chiave\n"
        "/eventi   — calendario macro del giorno\n"
        "/watchon  — alert automatici ON\n"
        "/watchoff — alert automatici OFF"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🟢")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero dati…")
    try:
        await update.message.reply_text(format_status(await analyze_now()))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Elaboro briefing…")
    try:
        await update.message.reply_text(format_briefing(await analyze_now()))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def postit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Recupero livelli…")
    try:
        await update.message.reply_text(format_postit(await analyze_now()))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def eventi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra tutti gli eventi macro USD del giorno."""
    try:
        evs = await asyncio.to_thread(fetch_macro_events)
        if not evs:
            await update.message.reply_text(
                "📅 Nessun evento macro USD oggi\n"
                "(o mercato chiuso)"
            )
            return
        icon = {"high": "🔴", "medium": "🟡"}
        lines = ["📅 EVENTI MACRO USD OGGI\n"]
        for ev in evs:
            i = icon.get(ev.get("impact", ""), "⚪")
            lines.append(f"{i} {ev['time']} — {ev['name']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
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
        current = await analyze_now()
        alert   = build_alert(app.bot_data.get("last_global_state"), current)
        app.bot_data["last_global_state"] = current.copy()
        if alert:
            for cid in list(app.bot_data["watch_chats"]):
                try:
                    await app.bot.send_message(chat_id=cid, text=alert)
                except Exception as e:
                    print(f"Errore invio {cid}: {e}")
    except Exception as e:
        print(f"Errore market_job: {e}")

async def post_init(app):
    app.bot_data.setdefault("watch_chats", set())
    app.bot_data.setdefault("last_global_state", None)
    # Precarica eventi macro all'avvio
    await asyncio.to_thread(fetch_macro_events)
    app.job_queue.run_repeating(
        market_job,
        interval=UPDATE_INTERVAL_SECONDS,
        first=UPDATE_INTERVAL_SECONDS,
    )
    print("✅ Job ARGO avviato")

# =========================
# MAIN
# =========================
def main():
    app = (
        ApplicationBuilder().token(TOKEN)
        .read_timeout(30).write_timeout(30)
        .connect_timeout(30).pool_timeout(30)
        .post_init(post_init).build()
    )
    for cmd, fn in [
        ("start",    start),
        ("ping",     ping),
        ("status",   status),
        ("briefing", briefing),
        ("postit",   postit),
        ("eventi",   eventi),
        ("watchon",  watchon),
        ("watchoff", watchoff),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    print("🤖 Bot ARGO partito…")
    app.run_polling(poll_interval=1.0, timeout=30)

if __name__ == "__main__":
    main()
