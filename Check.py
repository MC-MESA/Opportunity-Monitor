#!/usr/bin/env python3
"""
Opportunity page checker.

Reads urls.csv (columns: name,url), downloads each page, pulls out the
sentences that talk about opening/closing/deadlines/dates, and compares them
to the previous run. Writes:

  state.json   - memory of the last run (do not edit)
  changes.csv  - one row per opportunity; Excel reads this file

It never logs in anywhere and stores no personal data.
"""
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URLS_FILE = Path("urls.csv")
STATE_FILE = Path("state.json")
OUTPUT_FILE = Path("changes.csv")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CollegeOpportunityTracker/1.0)",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30          # seconds to wait for a page
PAUSE = 1.0           # seconds between pages (be polite to websites)
MIN_TEXT_CHARS = 200  # less text than this = probably needs JavaScript/login
MAX_KEY_LINES = 15

MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"\b{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b{MONTHS}\.?,?\s+20\d\d\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.I,
)
KEYWORD_RE = re.compile(
    r"\b(open|opens|opening|opened|deadline|due|apply|applications?|"
    r"accepting|closed|closes|closing|rolling)\b",
    re.I,
)
STATUS_RE = re.compile(
    r"(applications?\s+(?:are\s+)?(?:now\s+)?(?:open|closed)|now\s+open|"
    r"now\s+accepting|currently\s+accepting|no\s+longer\s+accepting|"
    r"applications?\s+(?:has|have)\s+closed|opens?\s+soon|coming\s+soon)",
    re.I,
)
LOGIN_HINTS = ("login", "signin", "sign-in", "sso", "auth")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def fetch(url):
    """Return (http_status, html, final_url, error_message)."""
    last_error = ""
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            return r.status_code, r.text, r.url, ""
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            time.sleep(3)
    return 0, "", url, last_error


def extract(html):
    """Return (full_text, key_lines) from a page's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    raw = soup.get_text("\n")
    parts = re.split(r"(?<=[.!?])\s+|\n+", raw)
    key_lines, seen = [], set()
    for part in parts:
        line = clean(part)
        if len(line) < 12 or len(line) > 400:
            continue
        if (KEYWORD_RE.search(line) and DATE_RE.search(line)) or STATUS_RE.search(line):
            low = line.lower()
            if low not in seen:
                seen.add(low)
                key_lines.append(line[:250])
        if len(key_lines) >= MAX_KEY_LINES:
            break
    return clean(raw), key_lines


def digest(text):
    return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16]


def check_one(url, prev):
    """Check a single URL. Returns (result, basis, http_status, new_state)."""
    state = dict(prev) if prev else {}
    status, html, final_url, error = fetch(url)

    if error or status == 0:
        return "error", "", status, state
    if status in (401, 403, 429):
        return "manual", "", status, state          # blocked or login needed
    if status >= 400:
        return "error", "", status, state
    if any(h in final_url.lower() for h in LOGIN_HINTS) and final_url != url:
        return "manual", "", status, state          # redirected to a login page

    text, key_lines = extract(html)
    if len(text) < MIN_TEXT_CHARS:
        return "manual", "", status, state          # page builds itself with JavaScript

    if key_lines:
        basis, sig = "dates", digest(" | ".join(key_lines))
    else:
        basis, sig = "full page", digest(text[:20000])

    if not prev or "sig" not in prev:
        result = "new"                              # first time we've seen it
        state.update(sig=sig, key_lines=key_lines, last_changed="", prev_key_lines=[])
    elif sig != prev["sig"]:
        result = "changed"
        state.update(
            sig=sig,
            key_lines=key_lines,
            last_changed=now_iso(),
            prev_key_lines=prev.get("key_lines", []),
        )
    else:
        result = "ok"
    return result, basis, status, state


def main():
    if not URLS_FILE.exists():
        print("urls.csv not found")
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}

    with URLS_FILE.open(newline="", encoding="utf-8-sig") as f:
        rows = [
            {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(f)
        ]
    rows = [r for r in rows if r.get("url")]

    cache, out_rows, problems = {}, [], 0
    for i, row in enumerate(rows):
        name, url = row.get("name", ""), row["url"]
        if url in cache:
            result, basis, status = cache[url]
        elif not url.lower().startswith(("http://", "https://")):
            result, basis, status = "error", "", 0
        else:
            result, basis, status, new_state = check_one(url, state.get(url))
            state[url] = new_state
            cache[url] = (result, basis, status)
            if i < len(rows) - 1:
                time.sleep(PAUSE)
        if result in ("error", "manual"):
            problems += 1
        s = state.get(url, {})
        if url in cache or result == "error":
            s["last_checked"] = now_iso()
            state[url] = s
        out_rows.append({
            "name": name,
            "url": url,
            "check_result": result,
            "basis": basis,
            "last_checked": now_iso(),
            "last_changed": s.get("last_changed", ""),
            "http_status": status,
            "key_lines": " | ".join(s.get("key_lines", []))[:600],
            "previous_key_lines": " | ".join(s.get("prev_key_lines", []))[:600],
        })
        print(f"{result:8} {url}")

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), "utf-8")
    fields = ["name", "url", "check_result", "basis", "last_checked",
              "last_changed", "http_status", "key_lines", "previous_key_lines"]
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(out_rows)

    # If most pages failed, something is wrong (e.g. no internet) - fail loudly
    # so GitHub emails the repo admins.
    errors = sum(1 for r in out_rows if r["check_result"] == "error")
    if out_rows and errors / len(out_rows) > 0.5:
        print("More than half of the pages failed - marking this run as failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
