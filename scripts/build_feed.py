#!/usr/bin/env python3
"""
Build feed/feed.json for the Serenity Stock Tracker dashboard.

1. Reads the public post archive (yan-labs/serenity-aleabitoreddit) to find every
   $ticker Serenity has mentioned and when it was first mentioned.
2. Downloads daily closing prices from Yahoo Finance (yfinance) once a day.
3. Optional: if the X_BEARER_TOKEN secret is set, fetches posts newer than the
   last run from the official X API (pay-per-use), so the dashboard is fresher
   than the archive, which updates about twice a day.
4. Optional: if the OPENAI_API_KEY secret is set, labels each post's stance per
   ticker (bull / bear / neu / none) and stores the labels in the feed, so every
   device sees the same stances without tagging in the browser.

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
IMPORT_FILE = ROOT / "feed" / "stances_import.json"   # exported from the dashboard (Settings → Export stances)

ARCHIVE_URL = "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/data/aleabitoreddit_tweets.json"
SYNC_URL = "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/data/sync_state.json"
ACCOUNT = "aleabitoreddit"
USER_ID = "1940360837547565056"          # @aleabitoreddit (saves a paid user lookup)

MIN_MENTIONS = int(os.getenv("MIN_MENTIONS", "3"))   # price names with at least this many mentions...
RECENT_DAYS = 90                                     # ...or any mention in the last 90 days
KEEP_POST_DAYS = 3                                   # the archive covers anything older
TAG_DAYS = int(os.getenv("TAG_DAYS") or "90")        # stance-tag posts from the last N days (0 = all history)
MAX_TAG_PER_RUN = int(os.getenv("MAX_TAG_PER_RUN") or "800")   # cost guard per run
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
OPENAI_BASE = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
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
    first, count, recent, taggable = {}, {}, set(), []
    cutoff = (NOW - dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    for x in arr:
        if x.get("isRetweet"):
            continue
        day = iso_date(x.get("createdAtISO") or "")
        if not day:
            continue
        tks = cashtags(x.get("text"))
        if tks:
            taggable.append({"id": str(x["id"]), "day": day, "text": x.get("text", ""), "tickers": tks})
        for t in tks:
            count[t] = count.get(t, 0) + 1
            if t not in first or day < first[t]:
                first[t] = day
            if day >= cutoff:
                recent.add(t)
    log(f"Archive: {len(arr)} posts, {len(count)} cashtags")
    return first, count, recent, taggable


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


# ---------------------------------------------------------------- AI stance tagging (optional)
TAG_SYS = "\n".join([
    "You label stock stances in X posts written by @aleabitoreddit (Serenity), an AI and semiconductor supply-chain investor.",
    "For each post and each listed ticker, judge the AUTHOR'S OWN stance toward that stock as expressed in that post:",
    "bull = positive, buying, holding with conviction, or arguing the thesis is strengthening;",
    "bear = negative, selling, shorting, warning about dilution, financing, fraud or weak fundamentals;",
    "neu = discussed substantively but balanced or without a directional view;",
    "none = only mentioned in passing, in a list, as context, a joke, or quoting someone else without endorsement.",
    'Return JSON only, in this shape: {"r":[{"i":"<post id>","s":{"TICKER":"bull|bear|neu|none"}}]}. Include every post and every listed ticker.',
])


class Fatal(Exception):
    pass


def openai_json(user, key):
    body = {"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": TAG_SYS}, {"role": "user", "content": user}]}
    last = None
    for attempt in range(3):
        try:
            r = requests.post(f"{OPENAI_BASE}/chat/completions", json=body, timeout=240,
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        except requests.RequestException as e:
            last = str(e); time.sleep(5 * (attempt + 1)); continue
        if r.status_code in (401, 403) or "insufficient_quota" in r.text:
            raise Fatal(f"OpenAI {r.status_code}: {r.text[:300]}")
        if r.status_code == 429 or r.status_code >= 500:
            last = f"OpenAI {r.status_code}"; time.sleep(15 * (attempt + 1)); continue
        if r.status_code >= 400:
            raise Fatal(f"OpenAI {r.status_code}: {r.text[:300]}")
        txt = r.json()["choices"][0]["message"]["content"] or ""
        return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    raise RuntimeError(last or "OpenAI request failed")


def norm_stance(v):
    v = str(v or "").lower()
    return "bull" if v.startswith("bull") else "bear" if v.startswith("bear") else "neu" if v.startswith("neu") else "none"


def tag_stances(candidates, stances, state):
    """candidates: [{id, day, text, tickers}] ; stances: {id: {T: s}} updated in place."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        log("No OPENAI_API_KEY set: skipping stance tagging (the dashboard can tag in the browser).")
        return
    from concurrent.futures import ThreadPoolExecutor
    since = "" if TAG_DAYS <= 0 else (NOW - dt.timedelta(days=TAG_DAYS)).strftime("%Y-%m-%d")
    todo = [c for c in candidates if c["day"] >= since and not all(t in stances.get(c["id"], {}) for t in c["tickers"])]
    todo.sort(key=lambda c: c["day"], reverse=True)          # newest first
    pending = len(todo)
    todo = todo[:MAX_TAG_PER_RUN]
    if not todo:
        log("Stances: nothing to tag."); state["tag_pending"] = 0; return
    log(f"Stances: tagging {len(todo)} of {pending} untagged posts with {OPENAI_MODEL}…")
    batches = [todo[i:i + 20] for i in range(0, len(todo), 20)]

    def run(b):
        user = "\n---\n".join(f"id: {c['id']}\ntickers: {', '.join(c['tickers'])}\ntext: {' '.join(c['text'].split())[:1500]}" for c in b)
        return openai_json(user, key)

    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(run, b) for b in batches]
        for f in futures:
            try:
                j = f.result()
            except Fatal as e:
                log(f"Stances stopped: {e}")
                for g in futures: g.cancel()
                break
            except Exception as e:
                log(f"  batch failed ({e}); will retry next run")
                continue
            for r in j.get("r") or j.get("results") or []:
                pid = str(r.get("i") or r.get("id") or "")
                if not pid or not isinstance(r.get("s"), dict):
                    continue
                o = stances.setdefault(pid, {})
                for t, v in r["s"].items():
                    o[str(t).upper().lstrip("$")] = norm_stance(v)
                done += 1
    state["tag_pending"] = max(0, pending - done)
    log(f"Stances: tagged {done} posts; {state['tag_pending']} still pending.")


# ---------------------------------------------------------------- main
def main():
    state = load_json(STATE, {})
    old = load_json(OUT, {"stocks": {}, "posts": []})
    tmap = {k: v for k, v in load_json(MAP_FILE, {}).items() if not k.startswith("_")}

    stocks = old.get("stocks", {})
    posts = list(old.get("posts", []))   # copy, so the change check below compares against the original
    stances = json.loads(json.dumps(old.get("stances", {})))   # deep copy, same reason

    # 0) one-off import of stances already tagged in a browser
    if IMPORT_FILE.exists():
        try:
            j = json.loads(IMPORT_FILE.read_text(encoding="utf-8"))
            src = j.get("stances", j) if isinstance(j, dict) else {}
            added = 0
            for pid, o in src.items():
                if not isinstance(o, dict):
                    continue
                cur = stances.setdefault(str(pid), {})
                for t, v in o.items():
                    t = str(t).upper().lstrip("$")
                    if t not in cur:                      # never overwrite labels already in the feed
                        cur[t] = norm_stance(v); added += 1
            log(f"Imported {added} ticker stances from {IMPORT_FILE.name} ({len(src)} posts).")
            IMPORT_FILE.unlink()                          # done; the workflow commits the removal
        except Exception as e:
            log(f"Could not import {IMPORT_FILE.name}: {e} (left in place)")

    # 1) fresh posts (optional)
    new_posts = fetch_new_posts(state)
    if new_posts:
        seen = {p["id"] for p in posts}
        posts += [p for p in new_posts if p["id"] not in seen]
    keep_from = (NOW - dt.timedelta(days=KEEP_POST_DAYS)).isoformat()
    posts = sorted([p for p in posts if (p.get("created_at") or "") >= keep_from[:19]],
                   key=lambda p: p.get("created_at") or "", reverse=True)

    # 2) decide whether the archive is needed (prices due, archive changed, or tagging pending)
    archive_changed = False
    try:
        sync = requests.get(SYNC_URL, timeout=30).json()
        archive_changed = sync.get("last_tweet_id") != state.get("archive_id")
    except Exception:
        sync = {}
    want_tags = bool(os.getenv("OPENAI_API_KEY", "").strip())
    need_archive = prices_due(state) or (want_tags and (archive_changed or state.get("tag_pending", 1) or new_posts))
    archive = read_archive() if need_archive else None
    if archive and sync.get("last_tweet_id"):
        state["archive_id"] = sync["last_tweet_id"]

    # 3) stances (optional)
    if archive and want_tags:
        cands = archive[3] + [{"id": p["id"], "day": iso_date(p["created_at"]), "text": p["text"],
                               "tickers": [m["ticker"] for m in p["mentions"]]} for p in posts]
        tag_stances(cands, stances, state)

    # 4) prices, once a day
    if prices_due(state):
        first, count, recent, _ = archive
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
        "stances": stances,
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
