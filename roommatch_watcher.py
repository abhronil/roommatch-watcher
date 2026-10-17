#!/usr/bin/env python3
"""
Roommatch watcher
-----------------
Sends a push notification to your phone for new Roommatch listings that are
  1. in Den Haag and have NO "voorrang" (priority) rule, or
  2. a "Direct offer" in Delft, Leiden or Den Haag (and have no priority rule).

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

# Cities to watch for listings without voorrang (as Roommatch writes them, case-insensitive)
CITIES = os.getenv("CITIES", "Den Haag").split(",")

# Cities to watch for "Direct offer" listings. These are reported even when they
# have a note like "Deze kamer is tijdelijk beschikbaar t/m ...", but still skipped
# when they have a real priority rule. Set to "" to turn this off.
DIRECT_OFFER_CITIES = [c for c in os.getenv("DIRECT_OFFER_CITIES", "Delft,Leiden,Den Haag").split(",") if c.strip()]

# Roommatch's internal name for the "Direct offer" model
DIRECT_OFFER_MODELS = {"reactiedatum"}

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

# Problem alerts: warn me when this many checks in a row fail, and repeat the
# warning at most every ALERT_REPEAT_HOURS while it stays broken.
ALERT_AFTER_FAILURES = int(os.getenv("ALERT_AFTER_FAILURES", "3"))
ALERT_REPEAT_HOURS = float(os.getenv("ALERT_REPEAT_HOURS", "12"))

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
SITE = "https://www.roommatch.nl"
PHOTO_SIZE = ".800f600"  # Roommatch serves resized photos: <photo url>.<width>f<height>

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
    listings = data.get("result") if isinstance(data, dict) else None
    if not isinstance(listings, list) or not listings:
        raise ValueError("Roommatch returned no listings (the site may have changed)")
    return listings


# ---------- filtering ----------

# Readable names for Roommatch priority rules (anything unknown is shown by its code)
RULE_NAMES = {
    "studentenHaaglanden1": "Voorrang eerstejaars studenten",
    "studentenUniversiteitLeiden1": "Voorrang eerstejaars studenten (Leiden)",
    "studentUitBuitenland": "Voorrang studenten uit het buitenland",
}


MODEL_NAMES = {
    "reactiedatum": "Direct offer",
    "inschrijfduur": "Inschrijfduur",
    "hospiteren": "Hospiteren",
    "hospitereninschrijfduur": "Hospiteren (inschrijfduur)",
}


def model_code(listing):
    return (((listing.get("model") or {}).get("modelCategorie") or {}).get("code") or "").strip()


def is_direct_offer(listing):
    return model_code(listing) in DIRECT_OFFER_MODELS


def listing_kind(listing):
    """'direct' for a direct offer we watch, 'regular' for a watched city, else None."""
    city = ((listing.get("city") or {}).get("name") or "").strip().lower()
    if is_direct_offer(listing) and city in [c.strip().lower() for c in DIRECT_OFFER_CITIES]:
        return "direct"
    if city in [c.strip().lower() for c in CITIES]:
        return "regular"
    return None


def passes_extra_filters(listing):
    if MAX_TOTAL_RENT and (listing.get("totalRent") or 0) > MAX_TOTAL_RENT:
        return False
    if ONLY_ZELFSTANDIG and not listing.get("isZelfstandig"):
        return False
    if SKIP_HOSPITEREN and (listing.get("model") or {}).get("isHospiteren"):
        return False
    return True


def info_reasons(listing, strict=None):
    """Priority mentioned in the short info line shown on the listing card."""
    strict = STRICT if strict is None else strict
    info = (listing.get("infoveldKort") or "").strip()
    if not info:
        return []
    if strict or any(w.strip().lower() in info.lower() for w in VOORRANG_WORDS if w.strip()):
        return [info]
    return []


def fetch_detail(listing_id):
    """Full listing data, the same data the listing's detail page shows (no login needed)."""
    detail_file = os.getenv("DETAIL_FILE")  # for testing: JSON {id: result}
    if detail_file:
        result = json.loads(Path(detail_file).read_text(encoding="utf-8")).get(str(listing_id))
    else:
        req = urllib.request.Request(
            DETAIL_API,
            data=urllib.parse.urlencode({"id": listing_id}).encode(),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not result.get("id"):
        raise ValueError("no listing data returned")
    if "sorteringBeleidsregels" not in result:
        # Without this field we can't tell whether there's priority, so don't guess
        raise ValueError("priority information missing (the site may have changed)")
    return result


def detail_reasons(d, strict=None):
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
    reasons += info_reasons(d, strict)
    return reasons


def priority_reasons(listing, kind="regular"):
    """[] means: no priority at all. Raises if the detail data can't be fetched.
    For direct offers the info line only counts when it mentions priority words,
    because it's usually just "tijdelijk beschikbaar t/m ..."."""
    strict = False if kind == "direct" else None
    return info_reasons(listing, strict) or detail_reasons(fetch_detail(listing.get("id")), strict)


def describe(listing):
    addr = f"{listing.get('street', '')} {listing.get('houseNumber', '')}"
    if listing.get("houseNumberAddition"):
        addr += f" {listing['houseNumberAddition']}"
    city = (listing.get("city") or {}).get("name", "")
    quarter = (listing.get("quarter") or {}).get("name", "")
    rent = listing.get("totalRent") or 0
    area = listing.get("areaDwelling") or 0
    kind = (listing.get("woningsoort") or {}).get("localizedNaam", "")
    code = model_code(listing)
    model = MODEL_NAMES.get(code, code)
    closing = listing.get("closingDate", "")
    available = (listing.get("availableFrom") or listing.get("availableFromDate") or "").strip()
    note = (listing.get("infoveldKort") or "").strip()
    url = DETAIL_URL.format(urlKey=listing.get("urlKey", ""))

    title = f"{addr.strip()}, {city} - EUR {rent:.0f}"
    if is_direct_offer(listing):
        title = "Direct offer: " + title
    lines = [
        f"{quarter}" if quarter else "",
        f"{kind}, {area} m2" if area else kind,
        f"Total rent: EUR {rent:.2f}",
        f"Type: {model}" if model else "",
        f"Available: {available}" if available else "",
        note,
        f"Closes: {closing}" if closing else "",
    ]
    body = "\n".join(l for l in lines if l)
    return title, body, url


def extras(listing, url):
    """Photo and buttons for the notification."""
    pictures = listing.get("pictures") or []
    photo = SITE + pictures[0]["uri"] + PHOTO_SIZE if pictures and pictures[0].get("uri") else None
    actions = [{"action": "view", "label": "Reageer", "url": url, "clear": True}]
    lat, lng = listing.get("latitude"), listing.get("longitude")
    if lat and lng:
        actions.append({"action": "view", "label": "Kaart",
                        "url": f"https://www.google.com/maps/search/?api=1&query={lat}%2C{lng}"})
    return photo, actions


# ---------- memory (so you only hear about each listing once) ----------
# notified: listings you already got a notification for
# rejected: listings skipped for priority, re-checked every RECHECK_HOURS in case
#           the priority is lifted later

RECHECK_HOURS = float(os.getenv("RECHECK_HOURS", "6"))


def load_state():
    """notified (set), rejected ({id: time}), health ({...})"""
    try:
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, dict):
            return (set(data.get("notified", [])), dict(data.get("rejected", {})),
                    dict(data.get("health", {})))
    except (FileNotFoundError, ValueError):
        pass
    return set(), {}, {}  # also resets the old format, whose entries all had priority


def save_state(notified, rejected, health):
    SEEN_FILE.write_text(json.dumps(
        {"notified": sorted(notified), "rejected": rejected, "health": health}))


# ---------- notifications ----------

def notify(title, body, url, photo=None, actions=None, tags=("house",), priority=4):
    sent = False
    if NTFY_TOPIC:
        msg = {"topic": NTFY_TOPIC, "title": title, "message": body,
               "tags": list(tags), "priority": priority}
        if url:
            msg["click"] = url
        if photo:
            msg["attach"] = photo
            msg["filename"] = "room.jpg"
        if actions:
            msg["actions"] = actions
        req = urllib.request.Request(
            NTFY_SERVER.rstrip("/") + "/",
            data=json.dumps(msg).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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
    log(f"SENT: {title}\n    {body.replace(chr(10), ' | ')}\n    {url or ''}")


# ---------- main ----------

def check_once(notified, rejected, dry_run=False):
    """One check. Raises if Roommatch can't be read at all.
    Returns (number checked, number of failed detail lookups)."""
    listings = fetch_listings()
    candidates = [l for l in listings if listing_kind(l) and passes_extra_filters(l)]
    now = time.time()

    if dry_run:
        to_check = candidates
    else:
        to_check = [
            l for l in candidates
            if str(l.get("id")) not in notified
            and now - rejected.get(str(l.get("id")), 0) > RECHECK_HOURS * 3600
        ]

    new_hits = errors = 0
    last_error = None
    for i, l in enumerate(to_check):
        lid = str(l.get("id"))
        title, body, url = describe(l)
        if i:
            time.sleep(1)  # be polite: one detail request per second
        try:
            reasons = priority_reasons(l, listing_kind(l))
        except Exception as e:
            errors += 1
            last_error = e
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
            photo, actions = extras(l, url)
            notify(title, body, url, photo, actions)
            notified.add(lid)
            rejected.pop(lid, None)
            new_hits += 1
        except Exception as e:  # keep going if one notification fails
            errors += 1
            last_error = e
            log(f"Notification failed: {e}")

    log(f"{len(listings)} listings total, {len(candidates)} watched "
        f"(no-voorrang: {', '.join(CITIES)}; direct offers: {', '.join(DIRECT_OFFER_CITIES) or 'off'}), "
        f"{len(to_check)} checked, {new_hits} new to notify")
    if not dry_run:
        live = {str(l.get("id")) for l in listings}
        for k in [k for k in rejected if k not in live]:  # forget closed listings
            del rejected[k]
    if to_check and errors == len(to_check):
        raise RuntimeError(f"every listing check failed, last error: {last_error}")
    return len(to_check), errors


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "unknown"


def run_and_watch_health(dry_run=False):
    """Runs one check and keeps track of failures. Returns True if it worked."""
    notified, rejected, health = load_state()
    now = time.time()
    try:
        check_once(notified, rejected, dry_run)
        ok, error = True, None
    except Exception as e:
        ok, error = False, e
        log(f"CHECK FAILED: {e}")
    if dry_run:
        return ok

    if ok:
        if health.get("alerted"):
            try:
                notify("Roommatch watcher works again",
                       f"Checks are running normally again since {fmt_time(now)}. "
                       f"It was down since {fmt_time(health.get('first_failure'))}.",
                       None, tags=("white_check_mark",), priority=3)
            except Exception as e:
                log(f"Could not send recovery notice: {e}")
        # last_ok_day changes once a day, so the memory file (and the repository)
        # sees activity every day, which keeps GitHub's schedule from pausing
        health = {"last_ok": now, "last_ok_day": datetime.now().strftime("%Y-%m-%d")}
    else:
        health["failures"] = health.get("failures", 0) + 1
        health.setdefault("first_failure", now)
        health["last_error"] = str(error)[:300]
        repeat_due = now - health.get("last_alert", 0) > ALERT_REPEAT_HOURS * 3600
        if health["failures"] >= ALERT_AFTER_FAILURES and repeat_due:
            try:
                notify("Roommatch watcher has a problem",
                       f"The last {health['failures']} checks failed, so you might miss listings.\n"
                       f"Error: {str(error)[:200]}\n"
                       f"Last successful check: {fmt_time(health.get('last_ok'))}",
                       "https://github.com/abhronil/roommatch-watcher/actions",
                       tags=("warning",), priority=5)
                health["alerted"] = True
                health["last_alert"] = now
            except Exception as e:
                log(f"Could not send problem alert: {e}")
    save_state(notified, rejected, health)
    return ok


def main():
    p = argparse.ArgumentParser(description="Roommatch watcher: no-voorrang listings and direct offers")
    p.add_argument("--loop", action="store_true", help="keep running and check periodically")
    p.add_argument("--dry-run", action="store_true", help="show matches, don't notify")
    p.add_argument("--test", action="store_true", help="send a test notification")
    args = p.parse_args()

    if args.test:
        # Show what a real notification looks like, using a current listing
        try:
            sample = next(l for l in fetch_listings() if listing_kind(l))
            title, body, url = describe(sample)
            photo, actions = extras(sample, url)
            notify("TEST - " + title, "This is how a match will look (just a test).\n" + body,
                   url, photo, actions)
        except Exception as e:
            log(f"Could not build a sample listing ({e}), sending a plain test")
            notify("Roommatch watcher test", "If you see this, notifications work.",
                   "https://www.roommatch.nl/aanbod/studentenwoningen")
        return

    if not args.loop:
        # exit code 1 marks the run as failed (red cross on GitHub) when it broke
        sys.exit(0 if run_and_watch_health(args.dry_run) else 1)

    log(f"Watching every {CHECK_EVERY_MINUTES} min. Ctrl+C to stop.")
    while True:
        run_and_watch_health(args.dry_run)
        time.sleep(max(CHECK_EVERY_MINUTES, 5) * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
