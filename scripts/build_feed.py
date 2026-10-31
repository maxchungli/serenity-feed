#!/usr/bin/env python3
"""
Build feed/feed.json for the Serenity Stock Tracker dashboard.

1. Reads the public post archive (yan-labs/serenity-aleabitoreddit) to find every
   $ticker Serenity has mentioned and when it was first mentioned.
2. Downloads daily closing prices from Yahoo Finance (yfinance) once a day.
3. Optional: if the X_BEARER_TOKEN secret is set, fetches posts newer than the
   last run from the official X API (pay-per-use), so the dashboard is fresher
   than the archive, which updates about twice a day.

The dashboard merges this feed on top of the archive by post id. Prices and
any posts here take priority.
"""
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "feed" / "feed.json"
STATE = ROOT / "feed" / "state.json"
MAP_FILE = ROOT / "ticker_map.json"

ARCHIVE_URL = "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/data/aleabitoreddit_tweets.json"
ACCOUNT = "aleabitoreddit"
USER_ID = "1940360837547565056"          # @aleabitoreddit (saves a paid user lookup)

MIN_MENTIONS = int(os.getenv("MIN_MENTIONS", "3"))   # price names with at least this many mentions...
RECENT_DAYS = 90                                     # ...or any mention in the last 90 days
KEEP_POST_DAYS = 3                                   # the archive covers anything older
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,5})(?![A-Za-z0-9])")
SUFFIX_CCY = {
    ".ST": "SEK", ".PA": "EUR", ".DE": "EUR", ".AS": "EUR", ".MI": "EUR", ".HE": "EUR", ".BR": "EUR",
    ".L": "GBX", ".TW": "TWD", ".TWO": "TWD", ".KS": "KRW", ".KQ": "KRW", ".T": "JPY", ".HK": "HKD",
    ".TO": "CAD", ".V": "CAD", ".AX": "AUD", ".SW": "CHF", ".CO": "DKK", ".OL": "NOK", ".SS": "CNY", ".SZ": "CNY",
}

NOW = dt.datetime.now(dt.timezone.utc)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # failed symbols are summarised below instead


def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def cashtags(text):
    return sorted({m.upper() for m in CASHTAG.findall(text or "")})


def iso_date(s):
    return s[:10]


# ---------------------------------------------------------------- archive
def read_archive():
    log("Downloading archive…")
    r = requests.get(ARCHIVE_URL, timeout=120)
    r.raise_for_status()
    arr = r.json()
    first, count, recent = {}, {}, set()
    cutoff = (NOW - dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    for x in arr:
        if x.get("isRetweet"):
            continue
        day = iso_date(x.get("createdAtISO") or "")
        if not day:
            continue
        for t in cashtags(x.get("text")):
            count[t] = count.get(t, 0) + 1
            if t not in first or day < first[t]:
                first[t] = day
            if day >= cutoff:
                recent.add(t)
    log(f"Archive: {len(arr)} posts, {len(count)} cashtags")
    return first, count, recent


# ---------------------------------------------------------------- prices
def yahoo_symbol(t, tmap):
    if t in tmap:
        return tmap[t]
    return t.replace(".", "-")


def currency_for(sym):
    for suf, ccy in SUFFIX_CCY.items():
        if sym.upper().endswith(suf):
            return ccy
    return "USD"


def download_prices(symbols, start):
    import pandas as pd
    import yfinance as yf

    out = {}
    syms = sorted(set(symbols))
    for i in range(0, len(syms), 40):
        chunk = syms[i:i + 40]
        for attempt in range(3):
            try:
                data = yf.download(chunk, start=start, interval="1d", auto_adjust=False,
                                   group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # rate limits and transient errors
                log(f"  yfinance error ({e}); retrying in {20 * (attempt + 1)}s")
                time.sleep(20 * (attempt + 1))
        else:
            continue
        for s in chunk:
            try:
                df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
                col = df["Close"].dropna()
            except KeyError:
                continue
            if col.empty:
                continue
            out[s] = [[d.strftime("%Y-%m-%d"), round(float(v), 4 if float(v) < 1 else 2)] for d, v in col.items()]
        log(f"  prices {min(i + 40, len(syms))}/{len(syms)}")
        time.sleep(2)
    return out


def prices_due(state):
    if os.getenv("FORCE_PRICES", "").lower() == "true":
        return True
    last = state.get("prices_at")
    if not last:
        return True
    age_h = (NOW - dt.datetime.fromisoformat(last)).total_seconds() / 3600
    # once a day after the US close (~21:00 UTC), or catch up if a run was missed
    return (age_h > 12 and NOW.hour >= 21) or age_h > 30


# ---------------------------------------------------------------- X API (optional)
def fetch_new_posts(state):
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        log("No X_BEARER_TOKEN set: skipping X API (archive provides posts).")
        return None
    params = {
        "max_results": 100,
        "exclude": "retweets",
        "tweet.fields": "created_at,public_metrics,note_tweet,referenced_tweets",
    }
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    else:  # first run: don't pay to backfill history the archive already has
        params["start_time"] = (NOW - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://api.x.com/2/users/{USER_ID}/tweets",
                     headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    if r.status_code == 429:
        log("X API rate limited; will try next run.")
        return []
    if r.status_code >= 400:
        log(f"X API error {r.status_code}: {r.text[:300]}")
        return []
    j = r.json()
    newest = (j.get("meta") or {}).get("newest_id")
    if newest:
        state["since_id"] = newest
    posts = []
    for x in j.get("data", []) or []:
        text = (x.get("note_tweet") or {}).get("text") or x.get("text", "")
        tks = cashtags(text)
        if not tks:
            continue
        pm = x.get("public_metrics") or {}
        posts.append({
            "id": x["id"],
            "url": f"https://x.com/{ACCOUNT}/status/{x['id']}",
            "created_at": x.get("created_at"),
            "text": text,
            "isReply": any(rt.get("type") == "replied_to" for rt in x.get("referenced_tweets") or []),
            "metrics": {"likes": pm.get("like_count", 0), "retweets": pm.get("retweet_count", 0),
                        "replies": pm.get("reply_count", 0), "quotes": pm.get("quote_count", 0),
                        "views": pm.get("impression_count", 0)},
            "mentions": [{"ticker": t, "stance": None} for t in tks],
        })
    log(f"X API: {len(j.get('data', []) or [])} new posts, {len(posts)} with cashtags")
    return posts


# ---------------------------------------------------------------- main
def main():
    state = load_json(STATE, {})
    old = load_json(OUT, {"stocks": {}, "posts": []})
    tmap = {k: v for k, v in load_json(MAP_FILE, {}).items() if not k.startswith("_")}

    stocks = old.get("stocks", {})
    posts = list(old.get("posts", []))   # copy, so the change check below compares against the original

    # 1) fresh posts (optional)
    new_posts = fetch_new_posts(state)
    if new_posts:
        seen = {p["id"] for p in posts}
        posts += [p for p in new_posts if p["id"] not in seen]
    keep_from = (NOW - dt.timedelta(days=KEEP_POST_DAYS)).isoformat()
    posts = sorted([p for p in posts if (p.get("created_at") or "") >= keep_from[:19]],
                   key=lambda p: p.get("created_at") or "", reverse=True)

    # 2) prices, once a day
    if prices_due(state):
        first, count, recent = read_archive()
        for p in posts:
            for m in p["mentions"]:
                t = m["ticker"]
                recent.add(t)
                first.setdefault(t, iso_date(p["created_at"]))
        wanted = sorted(t for t in first if count.get(t, 0) >= MIN_MENTIONS or t in recent)
        sym_of = {t: yahoo_symbol(t, tmap) for t in wanted}
        sym_of = {t: s for t, s in sym_of.items() if s}
        start = (dt.date.fromisoformat(min(first[t] for t in sym_of)) - dt.timedelta(days=10)).isoformat()
        log(f"Downloading prices for {len(sym_of)} names from {start}…")
        px = download_prices(sym_of.values(), start)
        fresh = {}
        for t, s in sym_of.items():
            if s in px:
                fresh[t] = {"yahoo": s, "currency": currency_for(s), "prices": px[s]}
        missing = sorted(t for t in sym_of if t not in fresh)
        log(f"Prices found for {len(fresh)}; no data for {len(missing)}: {', '.join(missing[:60])}")
        if len(fresh) >= max(5, len(stocks) // 2):   # don't wipe good data on a bad Yahoo day
            stocks = fresh
            state["prices_at"] = NOW.isoformat()
            state["missing"] = missing
        else:
            log("Too few prices returned; keeping previous prices.")
    else:
        log("Prices are fresh; skipping download.")

    content = {
        "account": ACCOUNT,
        "as_of": (NOW - dt.timedelta(hours=4)).strftime("%Y-%m-%d"),   # approx. US Eastern date
        "stocks": stocks,
        "posts": posts,
    }
    old_cmp = {k: v for k, v in old.items() if k not in ("updated_at", "as_of")}
    new_cmp = {k: v for k, v in content.items() if k != "as_of"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if old_cmp != new_cmp or not OUT.exists():
        content["updated_at"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        OUT.write_text(json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"Wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    else:
        log("Feed unchanged.")
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
