#!/usr/bin/env python3
"""
Roommatch watcher
-----------------
Checks Roommatch for new listings in Den Haag that have NO "voorrang"
(priority) rule and sends a push notification to your phone.

No login needed: Roommatch's listing feed is public. You only log in
yourself (via ROOM.nl) when you want to react to a listing.

Usage
  python3 roommatch_watcher.py --test       # send a test notification
  python3 roommatch_watcher.py --dry-run    # show current matches, no notifications
  python3 roommatch_watcher.py              # check once (good for cron / Task Scheduler)
  python3 roommatch_watcher.py --loop       # keep running, check every CHECK_EVERY_MINUTES

Only uses the Python standard library (Python 3.8+), nothing to install.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ============================================================
# SETTINGS: change these to your liking
# (or set them as environment variables with the same name)
# ============================================================

# Cities to watch (as Roommatch writes them, case-insensitive)
CITIES = os.getenv("CITIES", "Den Haag").split(",")

# Priority rules are read from each listing's detail data (the "Eerste keus voor"
# section, e.g. "De voorrangsregeling voor eerstejaars studenten is van toepassing").
# STRICT (default): also skip EVERY listing that has a short info text, because
# that line is where other priority notes are shown ("Voorrang voor ...",
# "Eerstejaars masterstudent gezocht", ...). You only get listings with no
# conditions at all. Set STRICT=false to only skip texts with the words below.
STRICT = os.getenv("STRICT", "true").lower() == "true"

# Used when STRICT=false: a listing counts as priority if its info text has any of these
VOORRANG_WORDS = os.getenv(
    "VOORRANG_WORDS",
    "voorrang,urgentie,urgent,priority,voorkeur,preference,eerstejaars,first-year,"
    "gezocht,wanted,alleen,uitsluitend,only,doelgroep,master,bachelor,student van",
).split(",")

# Optional extra filters (leave empty / 0 to disable)
MAX_TOTAL_RENT = float(os.getenv("MAX_TOTAL_RENT", "0"))   # e.g. 800
ONLY_ZELFSTANDIG = os.getenv("ONLY_ZELFSTANDIG", "false").lower() == "true"
SKIP_HOSPITEREN = os.getenv("SKIP_HOSPITEREN", "false").lower() == "true"

# How often to check in --loop mode (please keep this >= 5 to be polite)
CHECK_EVERY_MINUTES = int(os.getenv("CHECK_EVERY_MINUTES", "10"))

# --- Notifications -----------------------------------------
# ntfy (easiest): install the free "ntfy" app on your phone, subscribe to
# the topic below. Pick something long and random, anyone who knows the
# topic name can read it.
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")  # set as a GitHub secret, never in this file
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# Telegram (optional): create a bot via @BotFather, get your chat id via @userinfobot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================

FEED_URL = "https://www.roommatch.nl/portal/object/frontend/getallobjects/format/json"
DETAIL_URL = "https://www.roommatch.nl/aanbod/studentenwoningen/details/{urlKey}"
DETAIL_API = "https://www.roommatch.nl/portal/object/frontend/getobject/format/json"
SEEN_FILE = Path(__file__).with_name("roommatch_seen.json")
USER_AGENT = "Mozilla/5.0 (personal listing alert; checks every few minutes)"

# When run in the background (pythonw / Task Scheduler) there is no console,
# so write the output to roommatch.log next to the script instead.
if sys.stdout is None:
    sys.stdout = sys.stderr = open(
        Path(__file__).with_name("roommatch.log"), "a", encoding="utf-8", buffering=1
    )
else:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- fetching ----------

def fetch_listings():
    # For testing you can point FEED_FILE at a saved copy of the JSON
    feed_file = os.getenv("FEED_FILE")
    if feed_file:
        data = json.loads(Path(feed_file).read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(
            FEED_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", [])


# ---------- filtering ----------

# Readable names for Roommatch priority rules (anything unknown is shown by its code)
RULE_NAMES = {
    "studentenHaaglanden1": "Voorrang eerstejaars studenten",
    "studentenUniversiteitLeiden1": "Voorrang eerstejaars studenten (Leiden)",
    "studentUitBuitenland": "Voorrang studenten uit het buitenland",
}


def in_cities(listing):
    city = ((listing.get("city") or {}).get("name") or "").strip().lower()
    return city in [c.strip().lower() for c in CITIES]


def passes_extra_filters(listing):
    if MAX_TOTAL_RENT and (listing.get("totalRent") or 0) > MAX_TOTAL_RENT:
        return False
    if ONLY_ZELFSTANDIG and not listing.get("isZelfstandig"):
        return False
    if SKIP_HOSPITEREN and (listing.get("model") or {}).get("isHospiteren"):
        return False
    return True


def info_reasons(listing):
    """Priority mentioned in the short info line shown on the listing card."""
    info = (listing.get("infoveldKort") or "").strip()
    if not info:
        return []
    if STRICT or any(w.strip().lower() in info.lower() for w in VOORRANG_WORDS if w.strip()):
        return [info]
    return []


def fetch_detail(listing_id):
    """Full listing data, the same data the listing's detail page shows (no login needed)."""
    detail_file = os.getenv("DETAIL_FILE")  # for testing: JSON {id: result}
    if detail_file:
        return json.loads(Path(detail_file).read_text(encoding="utf-8"))[str(listing_id)]
    req = urllib.request.Request(
        DETAIL_API,
        data=urllib.parse.urlencode({"id": listing_id}).encode(),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = data.get("result")
    if not isinstance(result, dict) or not result.get("id"):
        raise ValueError("no listing data returned")
    return result


def detail_reasons(d):
    """Every priority rule the detail page would list under 'Eerste keus voor'."""
    reasons = []
    for rule in d.get("sorteringBeleidsregels") or []:
        code = ((rule or {}).get("beleidsregel") or {}).get("code") or "?"
        reasons.append(RULE_NAMES.get(code, f"Voorrangsregel: {code}"))
    if ((d.get("sorteergroep") or {}).get("code") or "").strip():
        reasons.append(f"Sorteergroep: {d['sorteergroep']['code']}")
    if d.get("voorrangLeeftijdMin") or d.get("voorrangLeeftijdMax"):
        reasons.append(f"Voorrang leeftijd {d.get('voorrangLeeftijdMin') or ''}-{d.get('voorrangLeeftijdMax') or ''}")
    if d.get("voorrangHuishoudgrootteMin") or d.get("voorrangHuishoudgrootteMax"):
        reasons.append("Voorrang huishoudgrootte")
    if d.get("voorrangGezinnenKinderen"):
        reasons.append("Voorrang gezinnen met kinderen")
    if d.get("voorrangKernbinding"):
        reasons.append("Voorrang kernbinding")
    if d.get("voorrangAlleUrgenties"):
        reasons.append("Voorrang urgentie")
    reasons += info_reasons(d)
    return reasons


def priority_reasons(listing):
    """[] means: no priority at all. Raises if the detail data can't be fetched."""
    return info_reasons(listing) or detail_reasons(fetch_detail(listing.get("id")))


def describe(listing):
    addr = f"{listing.get('street', '')} {listing.get('houseNumber', '')}"
    if listing.get("houseNumberAddition"):
        addr += f" {listing['houseNumberAddition']}"
    city = (listing.get("city") or {}).get("name", "")
    quarter = (listing.get("quarter") or {}).get("name", "")
    rent = listing.get("totalRent") or 0
    area = listing.get("areaDwelling") or 0
    kind = (listing.get("woningsoort") or {}).get("localizedNaam", "")
    model = ((listing.get("model") or {}).get("modelCategorie") or {}).get("code", "")
    closing = listing.get("closingDate", "")
    url = DETAIL_URL.format(urlKey=listing.get("urlKey", ""))

    title = f"{addr.strip()}, {city} - EUR {rent:.0f}"
    lines = [
        f"{quarter}" if quarter else "",
        f"{kind}, {area} m2" if area else kind,
        f"Total rent: EUR {rent:.2f}",
        f"Type: {model}" if model else "",
        f"Closes: {closing}" if closing else "",
    ]
    body = "\n".join(l for l in lines if l)
    return title, body, url


# ---------- memory (so you only hear about each listing once) ----------
# notified: listings you already got a notification for
# rejected: listings skipped for priority, re-checked every RECHECK_HOURS in case
#           the priority is lifted later

RECHECK_HOURS = float(os.getenv("RECHECK_HOURS", "6"))


def load_state():
    try:
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, dict):
            return set(data.get("notified", [])), dict(data.get("rejected", {}))
    except (FileNotFoundError, ValueError):
        pass
    return set(), {}  # also resets the old format, whose entries all had priority


def save_state(notified, rejected):
    SEEN_FILE.write_text(json.dumps({"notified": sorted(notified), "rejected": rejected}))


# ---------- notifications ----------

def notify(title, body, url):
    sent = False
    if NTFY_TOPIC:
        params = urllib.parse.urlencode(
            {"title": title, "click": url, "tags": "house", "priority": "high"}
        )
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}?{params}",
            data=f"{body}\n{url}".encode("utf-8"),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
        sent = True
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        payload = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": f"🏠 {title}\n{body}\n{url}"}
        ).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload, timeout=15,
        ).read()
        sent = True
    if not sent:
        log("No notification channel set (NTFY_TOPIC or TELEGRAM_*), printing instead:")
    log(f"NEW: {title}\n    {body.replace(chr(10), ' | ')}\n    {url}")


# ---------- main ----------

def check_once(dry_run=False):
    listings = fetch_listings()
    candidates = [l for l in listings if in_cities(l) and passes_extra_filters(l)]
    notified, rejected = load_state()
    now = time.time()

    if dry_run:
        to_check = candidates
    else:
        to_check = [
            l for l in candidates
            if str(l.get("id")) not in notified
            and now - rejected.get(str(l.get("id")), 0) > RECHECK_HOURS * 3600
        ]

    new_hits = 0
    for i, l in enumerate(to_check):
        lid = str(l.get("id"))
        title, body, url = describe(l)
        if i:
            time.sleep(1)  # be polite: one detail request per second
        try:
            reasons = priority_reasons(l)
        except Exception as e:
            log(f"Could not check {title} ({e}); will try again next run")
            continue

        if dry_run:
            print(f"  {'MATCH  ' if not reasons else 'skipped'}  {title}"
                  + (f"   [{'; '.join(reasons)}]" if reasons else ""))
            continue

        if reasons:
            rejected[lid] = now
            log(f"Skipped (priority): {title} [{'; '.join(reasons)}]")
            continue
        try:
            notify(title, body, url)
            notified.add(lid)
            rejected.pop(lid, None)
            new_hits += 1
        except Exception as e:  # keep going if one notification fails
            log(f"Notification failed: {e}")

    log(f"{len(listings)} listings total, {len(candidates)} in {', '.join(CITIES)}, "
        f"{len(to_check)} checked, {new_hits} new without priority")
    if not dry_run:
        live = {str(l.get("id")) for l in listings}
        rejected = {k: v for k, v in rejected.items() if k in live}  # forget closed listings
        save_state(notified, rejected)


def main():
    p = argparse.ArgumentParser(description="Roommatch Den Haag no-voorrang watcher")
    p.add_argument("--loop", action="store_true", help="keep running and check periodically")
    p.add_argument("--dry-run", action="store_true", help="show matches, don't notify")
    p.add_argument("--test", action="store_true", help="send a test notification")
    args = p.parse_args()

    if args.test:
        notify("Roommatch watcher test", "If you see this, notifications work.",
               "https://www.roommatch.nl/aanbod/studentenwoningen")
        return

    if not args.loop:
        check_once(args.dry_run)
        return

    log(f"Watching every {CHECK_EVERY_MINUTES} min. Ctrl+C to stop.")
    while True:
        try:
            check_once(args.dry_run)
        except Exception as e:
            log(f"Check failed (will retry): {e}")
        time.sleep(max(CHECK_EVERY_MINUTES, 5) * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
