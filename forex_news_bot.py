"""
DREAM CREATORS - Forex News Bot for Discord
-------------------------------------------
Posts to your Discord channel automatically:
  1. Only IMPORTANT breaking headlines (central banks, big data, major shocks), capped per hour/day
  2. A daily morning briefing of today's high-impact news events (in IST)
  3. A warning X minutes before each high-impact event

Setup: see README. Only thing you MUST change is WEBHOOK_URL below.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ======================= SETTINGS (edit these) =======================

# Paste your Discord webhook URL here (Channel settings > Integrations > Webhooks)
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

# News feeds to watch (headline + link only is posted, never the full article)
FEEDS = [
    "https://www.investing.com/rss/news_1.rss",           # Investing.com forex news
    "https://www.fxstreet.com/rss/news",                  # FXStreet news
]

# IMPORTANT-ONLY FILTER
# A headline is posted only if it contains at least one of these market-moving words...
IMPORTANT_KEYWORDS = [
    "fed ", "fomc", "powell", "rate decision", "rate cut", "rate hike", "cuts rates",
    "hikes rates", "holds rates", "interest rate", "ecb", "lagarde", "rba", "bullock",
    "cpi", "inflation", "pce", "nonfarm", "non-farm", "nfp", "payrolls",
    "unemployment rate", "gdp", "retail sales", "ism ",
    "tariff", "sanction", "war ", "ceasefire", "attack", "emergency", "shutdown",
    "gold hits", "gold record", "record high", "all-time high", "plunge", "surge", "crash",
]
# ...and NONE of these words (these are opinion / analysis / forecast articles)
SKIP_WORDS = [
    "forecast", "prediction", "technical", "analysis", "outlook", "elliott", "chart",
    "what to expect", "preview", "week ahead", "weekly", "could", "may ", "might",
    "eyes", "targets", "levels to watch", "price action", "trade idea",
    "?", "poll", "podcast", "video", "opinion", "explained",
]
# Top-priority news is never held back by the hourly cap (still counts toward the daily cap)
TOP_PRIORITY = [
    "rate decision", "holds rates", "cuts rates", "hikes rates", "rate cut", "rate hike",
    "fomc", "cpi", "nonfarm", "non-farm", "nfp", "payrolls", "pce",
]
MAX_HEADLINES_PER_HOUR = 2               # hard cap so the channel never floods
MAX_HEADLINES_PER_DAY = 10
SIMILAR_TITLE_CUTOFF = 0.5               # skip a headline that repeats one already posted

# Economic calendar (high-impact events, this week)
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CURRENCIES = {"USD", "EUR", "AUD"}      # Gold moves on USD news
IMPACTS = {"High"}                      # add "Medium" if you want more events

BRIEFING_TIME_IST = "07:00"             # daily morning post time (24h, IST)
ALERT_MINUTES_BEFORE = 20               # warning before each event (cloud runs every ~5-10 min)
NEWS_CHECK_SECONDS = 60                 # how often to check headlines
CALENDAR_REFRESH_MINUTES = 60           # how often to re-download the calendar

BOT_NAME = "DC"                         # name shown on posts in Discord
BRAND = "DC • Forex News"
STATE_FILE = "bot_state.json"

# =====================================================================

IST = timezone(timedelta(hours=5, minutes=30))
COLORS = {"news": 0x3B82F6, "brief": 0xF59E0B, "alert": 0xEF4444}
FLAGS = {"USD": "🇺🇸", "EUR": "🇪🇺", "AUD": "🇦🇺"}


def log(msg):
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


# ---------------- state (remembers what was already posted) ----------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                s.setdefault("seen_links", [])
                s.setdefault("alerted", [])
                s.setdefault("last_brief", "")
                s.setdefault("posted", [])          # [timestamp, title] of headlines sent
                return s
        except Exception:
            pass
    return {"seen_links": [], "alerted": [], "last_brief": "", "posted": [], "first_run": True}


def save_state(state):
    state["seen_links"] = state["seen_links"][-3000:]   # keep file small
    state["alerted"] = state["alerted"][-500:]
    state["posted"] = state.get("posted", [])[-200:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------- Discord ----------------

def send_embed(title, description, color, url=None, footer=None):
    if "PASTE_YOUR" in WEBHOOK_URL:
        log("!! Set WEBHOOK_URL first. Would have posted: " + title)
        return False
    embed = {
        "title": title[:256],
        "description": description[:4000],
        "color": color,
        "footer": {"text": footer or BRAND},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        embed["url"] = url
    for _ in range(3):
        try:
            r = requests.post(WEBHOOK_URL, json={"username": BOT_NAME, "embeds": [embed]}, timeout=15)
            if r.status_code == 429:                      # Discord rate limit
                wait = float(r.json().get("retry_after", 2))
                time.sleep(wait + 0.5)
                continue
            r.raise_for_status()
            time.sleep(1)                                 # stay gentle on Discord
            return True
        except Exception as e:
            log(f"Discord error: {e}")
            time.sleep(3)
    return False


# ---------------- 1. Breaking headlines ----------------

def has_term(text, term):
    # single words match whole words only (so "may" doesn't match "mayor")
    t = " " + "".join(ch if ch.isalnum() or ch in "?-" else " " for ch in text.lower()) + " "
    term = term.strip().lower()
    if " " in term or not term.isalnum():
        return term in t
    return f" {term} " in t


def is_important(title):
    t = " " + title.lower() + " "
    if any(has_term(title, w) for w in SKIP_WORDS):
        return False
    return any(k in t for k in IMPORTANT_KEYWORDS)


def words(title):
    return {w for w in "".join(ch if ch.isalnum() else " " for ch in title.lower()).split() if len(w) > 2}


def is_repeat(title, state):
    a = words(title)
    for _, old in state.get("posted", [])[-60:]:
        b = words(old)
        if a and b and len(a & b) / len(a | b) >= SIMILAR_TITLE_CUTOFF:
            return True
    return False


def under_cap(state, title=""):
    now = time.time()
    sent = [ts for ts, _ in state.get("posted", [])]
    last_hour = sum(1 for ts in sent if now - ts < 3600)
    today = datetime.now(IST).date()
    today_count = sum(1 for ts in sent if datetime.fromtimestamp(ts, IST).date() == today)
    top = any(k in title.lower() for k in TOP_PRIORITY)
    return (top or last_hour < MAX_HEADLINES_PER_HOUR) and today_count < MAX_HEADLINES_PER_DAY


def check_news(state):
    seen = set(state["seen_links"])
    silent = state.get("first_run", False)   # first run: don't flood old news
    new_items = []
    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers={"User-Agent": "Mozilla/5.0"})
            source = feed.feed.get("title", feed_url.split("/")[2])
            for e in feed.entries[:30]:
                link = e.get("link") or e.get("id")
                title = (e.get("title") or "").strip()
                if not link or not title or link in seen:
                    continue
                seen.add(link)
                state["seen_links"].append(link)
                if not silent and is_important(title):
                    new_items.append((title, link, source))
        except Exception as ex:
            log(f"Feed failed ({feed_url}): {ex}")

    for title, link, source in reversed(new_items):  # oldest first
        if is_repeat(title, state):
            log(f"Skipped (repeat): {title}")
            continue
        if not under_cap(state, title):
            log(f"Skipped (hourly/daily cap reached): {title}")
            continue
        if send_embed(f"🔴 {title}", f"Source: {source}", COLORS["news"], url=link):
            state.setdefault("posted", []).append([time.time(), title])
            log(f"Posted headline: {title}")

    if silent:
        state["first_run"] = False
        log(f"First run: marked {len(seen)} existing headlines as seen.")


# ---------------- 2 & 3. Economic calendar ----------------

def fetch_calendar():
    try:
        r = requests.get(CALENDAR_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        events = []
        for ev in r.json():
            if ev.get("country") not in CURRENCIES or ev.get("impact") not in IMPACTS:
                continue
            try:
                when = datetime.fromisoformat(ev["date"]).astimezone(IST)
            except Exception:
                continue
            events.append({
                "id": f'{ev.get("country")}|{ev.get("title")}|{ev["date"]}',
                "title": ev.get("title", ""),
                "country": ev.get("country", ""),
                "impact": ev.get("impact", ""),
                "forecast": ev.get("forecast") or "-",
                "previous": ev.get("previous") or "-",
                "time": when,
            })
        events.sort(key=lambda x: x["time"])
        log(f"Calendar loaded: {len(events)} high-impact events this week.")
        return events
    except Exception as e:
        log(f"Calendar download failed: {e}")
        return None


def event_line(ev):
    flag = FLAGS.get(ev["country"], "")
    return (f'`{ev["time"]:%I:%M %p}` {flag} **{ev["country"]} – {ev["title"]}**\n'
            f'   Forecast: {ev["forecast"]} | Previous: {ev["previous"]}')


def post_briefing(events, state):
    today = datetime.now(IST).date()
    todays = [e for e in events if e["time"].date() == today]
    if todays:
        body = "\n".join(event_line(e) for e in todays)
        body += ("\n\n⚠️ Expect sharp moves in **XAUUSD, EURUSD, AUDUSD** around these times. "
                 "Funded traders: check your prop firm's news rule.")
    else:
        body = "✅ No high-impact USD/EUR/AUD news today. Normal technical day."
    send_embed(f"🌅 Today's High-Impact News – {today:%a %d %b %Y} (IST)",
               body, COLORS["brief"])
    state["last_brief"] = today.isoformat()
    log("Posted morning briefing.")


def check_alerts(events, state):
    now = datetime.now(IST)
    alerted = set(state["alerted"])
    for ev in events:
        mins = (ev["time"] - now).total_seconds() / 60
        if 0 < mins <= ALERT_MINUTES_BEFORE and ev["id"] not in alerted:
            send_embed(
                f'⏰ {int(round(mins))} min to {ev["country"]} {ev["title"]}',
                event_line(ev) + "\n\nVolatility ahead – manage open trades, avoid new entries.",
                COLORS["alert"],
            )
            state["alerted"].append(ev["id"])
            log(f'Alert sent: {ev["title"]}')


# ---------------- main loop ----------------

def main():
    log("Dream Creators Forex News Bot started.")
    state = load_state()
    log("Important-only mode: max %d headlines/hour, %d/day." % (MAX_HEADLINES_PER_HOUR, MAX_HEADLINES_PER_DAY))
    events, last_cal = [], 0

    while True:
        try:
            if time.time() - last_cal > CALENDAR_REFRESH_MINUTES * 60:
                fresh = fetch_calendar()
                if fresh is not None:
                    events = fresh
                last_cal = time.time()

            now = datetime.now(IST)
            if (now.strftime("%H:%M") >= BRIEFING_TIME_IST
                    and state["last_brief"] != now.date().isoformat()):
                post_briefing(events, state)

            check_alerts(events, state)
            check_news(state)
            save_state(state)
        except Exception as e:
            log(f"Loop error (bot keeps running): {e}")
        time.sleep(NEWS_CHECK_SECONDS)


def run_once():
    """One check, used by the GitHub Actions schedule (every 5 minutes)."""
    state = load_state()
    events = fetch_calendar() or []
    now = datetime.now(IST)
    if (now.strftime("%H:%M") >= BRIEFING_TIME_IST
            and state["last_brief"] != now.date().isoformat()):
        post_briefing(events, state)
    check_alerts(events, state)
    check_news(state)
    save_state(state)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        run_once()
    else:
        main()
