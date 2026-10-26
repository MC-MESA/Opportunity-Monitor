#!/usr/bin/env python3
"""
Opportunity page checker.

Reads urls.csv (columns: name,url), downloads each page, pulls out the
sentences that talk about opening/closing/deadlines/dates, and compares them
to the previous run. Writes:

  state.json   - memory of the last run (do not edit)
  changes.csv  - one row per opportunity, read by people on GitHub

key_lines only shows sentences about the present or future. Sentences whose
dates are all in the past (for example "Applications due May 10, 2025") are
hidden so nobody mistakes an old deadline for an open opportunity.

It never logs in anywhere and stores no personal data.
"""
import calendar
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# "Today" for all date comparisons. CHECKER_TODAY (YYYY-MM-DD) exists only for testing.
TODAY = (date.fromisoformat(os.environ["CHECKER_TODAY"])
         if os.environ.get("CHECKER_TODAY") else datetime.now(timezone.utc).date())

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
    r"applications?\s+(?:has|have)\s+closed|opens?\s+soon|coming\s+soon|"
    r"register\s+now|apply\s+now|registration\s+(?:is\s+)?(?:now\s+)?(?:open|closed)|"
    r"applications?\s+(?:are\s+)?(?:now\s+)?being\s+accepted)",
    re.I,
)
LOGIN_HINTS = ("login", "signin", "sign-in", "sso", "auth")


DEADLINE_WORD_RE = re.compile(r"\b(deadline|due|close|closes|closing|by)\b", re.I)
FULL_DATE_RE = re.compile(
    rf"\b({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I
)
MONTH_NUMBER = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")
NUMERIC_MD_RE = re.compile(r"\b\d{1,2}/\d{1,2}\b")
ISO_DATE_RE = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
MONTH_YEAR_RE = re.compile(rf"\b({MONTHS})\.?,?\s+(20\d\d)\b", re.I)
MONTH_DAY_RE = re.compile(rf"\b({MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)
SCHOOL_YEAR_RE = re.compile(r"\b(20\d\d)\s*[-\u2013/]\s*(20\d\d|\d\d)\b")
YEAR_RE = re.compile(r"(?<![\d,$.])\b(20(?:1[5-9]|[2-9]\d))\b(?![,.]?\d)")


def _blank(text, matches):
    for m in matches:
        text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    return text


def line_dates(line):
    """Find the dates written in a sentence.

    Returns (known, unclear). `known` holds the LAST day of each period we can
    place on the calendar: a full date counts as that day, "Month YYYY" as the
    end of that month, and a bare year (or school year like 2025-26) as the end
    of that year. `unclear` is True when the sentence has a date we cannot place,
    such as "April 1" with no year.
    """
    known, unclear, work = [], False, line

    def add(y, mo, d):
        nonlocal unclear
        try:
            known.append(date(y, mo, d))
        except ValueError:
            unclear = True

    ms = list(FULL_DATE_RE.finditer(work))
    for m in ms:
        add(int(m.group(3)), MONTH_NUMBER[m.group(1)[:3].lower()], int(m.group(2)))
    work = _blank(work, ms)

    ms = list(NUMERIC_DATE_RE.finditer(work))
    for m in ms:
        y = int(m.group(3))
        add(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
    work = _blank(work, ms)

    ms = list(ISO_DATE_RE.finditer(work))
    for m in ms:
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    work = _blank(work, ms)

    ms = list(MONTH_YEAR_RE.finditer(work))
    for m in ms:
        y, mo = int(m.group(2)), MONTH_NUMBER[m.group(1)[:3].lower()]
        add(y, mo, calendar.monthrange(y, mo)[1])
    work = _blank(work, ms)

    ms = list(SCHOOL_YEAR_RE.finditer(work))
    for m in ms:
        start, end = int(m.group(1)), m.group(2)
        end_year = int(end) if len(end) == 4 else (start // 100) * 100 + int(end)
        if end_year < start:
            unclear = True
        else:
            add(end_year, 12, 31)
    work = _blank(work, ms)

    ms = list(MONTH_DAY_RE.finditer(work)) + list(NUMERIC_MD_RE.finditer(work))
    if ms:
        unclear = True
    work = _blank(work, ms)

    for m in YEAR_RE.finditer(work):
        add(int(m.group(1)), 12, 31)
    return known, unclear


def is_past_line(line, today=None):
    """True only when every date in the sentence is before today.
    Sentences with no dates, or with a date we cannot place, are never hidden."""
    today = today or TODAY
    known, unclear = line_dates(line)
    return bool(known) and not unclear and all(d < today for d in known)


def visible_lines(lines, today=None):
    """The sentences worth showing: present or future, or without a clear date."""
    return [l for l in lines if not is_past_line(l, today)]


def date_warning(key_lines):
    """If a page says applications are open but also lists a deadline that has
    already passed, return a short warning. A person decides which is right
    (the page may simply have a typo in the year)."""
    if not any(OPEN_RE.search(line) for line in key_lines):
        return ""
    today = TODAY
    for line in key_lines:
        if not DEADLINE_WORD_RE.search(line):
            continue
        for m in FULL_DATE_RE.finditer(line):
            try:
                d = date(int(m.group(3)), MONTH_NUMBER[m.group(1)[:3].lower()], int(m.group(2)))
            except (ValueError, KeyError):
                continue
            if d < today:
                return ("Page says applications are open but lists a past date "
                        f"({m.group(0)}). Check which is correct.")
    return ""


def now_iso():
    return TODAY.isoformat()


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
    """Return (main_text, key_lines).

    key_lines are searched across the WHOLE page, including banners in the
    header or footer (that is where "Applications now open!" usually lives).
    main_text leaves out header/footer/menus and is used to decide whether the
    page has real content and as the fallback fingerprint.
    """
    soup = BeautifulSoup(html, "html.parser")
    extras = []
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        extras.append(meta["content"])
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    whole = BeautifulSoup(str(soup), "html.parser")
    for tag in soup(["header", "footer", "nav"]):
        tag.decompose()
    main_text = clean(soup.get_text("\n"))

    raw_all = "\n".join(extras) + "\n" + whole.get_text("\n")
    parts = re.split(r"(?<=[.!?])\s+|\n+", raw_all)
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
    return main_text, key_lines


OPEN_RE = re.compile(
    r"(?:\bnow\s+open\b|\b(?:applications?|registration)\s+(?:is|are)\s+open\b|"
    r"\b(?:now|currently)\s+accepting\b|\bbeing\s+accepted\b|"
    r"\bapply\s+now\b|\bregister\s+now\b)",
    re.I,
)
CLOSED_RE = re.compile(
    r"(?:\bclosed\b|\bno\s+longer\s+accepting\b|\bnot\s+(?:currently\s+)?accepting\b|"
    r"\bdeadline\s+(?:has\s+)?passed\b)",
    re.I,
)


def signal_for(all_lines, shown):
    """What the page's own wording suggests right now (a hint, not a verdict)."""
    if not all_lines:
        return ""
    if not shown:
        return "past dates only"      # every date-like sentence is already in the past
    text = " | ".join(shown)
    is_open, is_closed = bool(OPEN_RE.search(text)), bool(CLOSED_RE.search(text))
    if is_open and is_closed:
        return "mixed"
    if is_open:
        return "open"
    if is_closed:
        return "closed"
    return "dates only"


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
    if len(text) < MIN_TEXT_CHARS and not key_lines:
        state["was_manual"] = True
        return "manual", "", status, state          # page builds itself with JavaScript

    if key_lines:
        basis, sig = "dates", digest(" | ".join(key_lines))
    else:
        basis, sig = "full page", digest(text[:20000])

    was_manual = bool(prev and prev.get("was_manual"))
    state.pop("was_manual", None)
    if not prev or "sig" not in prev:
        if was_manual and key_lines:
            result = "changed"                      # was unreadable, now shows date/status wording
            state.update(sig=sig, key_lines=key_lines, last_changed=now_iso(), prev_key_lines=[])
        else:
            result = "new"                          # first time we've seen it
            state.update(sig=sig, key_lines=key_lines, last_changed="", prev_key_lines=[])
    elif sig != prev["sig"]:
        old_view = visible_lines(prev.get("key_lines", []))
        new_view = visible_lines(key_lines)
        if (key_lines or prev.get("key_lines")) and old_view == new_view:
            result = "ok"                           # only old, already-passed dates were edited
            state.update(sig=sig, key_lines=key_lines)
        else:
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


def problem_note(result, status):
    """Plain-English reason for rows the checker could not read."""
    if result == "error":
        if status in (404, 410):
            return "Page not found (404). The link may have moved; look for the new address."
        if status >= 400:
            return f"The website returned an error ({status}). Check the link in a browser."
        return "Could not connect to the website. Check the link in a browser."
    if result == "manual":
        if status in (401, 403, 429):
            return "The website blocks automated checks or needs a login. Check this page by hand."
        return ("The page has almost no readable text (it may load its content with "
                "JavaScript or need a login). Check this page by hand.")
    return ""


def main():
    if not URLS_FILE.exists():
        print("urls.csv not found")
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}

    with URLS_FILE.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        found = [(c or "").strip() for c in (reader.fieldnames or [])]
        rows = [
            {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in reader
        ]
    # Accept common header spellings for the address column (urls, website, link ...)
    for r in rows:
        if not r.get("url"):
            for alias in ("urls", "website", "link", "links", "web address", "address"):
                if r.get(alias):
                    r["url"] = r[alias]
                    break
    rows = [r for r in rows if r.get("url")]
    if not rows:
        print("ERROR: urls.csv has no usable rows, so nothing was checked.")
        print("The FIRST line of urls.csv must be exactly:  name,url")
        print("Every line after it must look like:  Program name,https://website")
        print("The first line found was: " + ",".join(found))
        sys.exit(1)
    print(f"Checking {len(rows)} pages from urls.csv")

    cache, out_rows = {}, []
    for i, row in enumerate(rows):
        name, url = row.get("name", ""), row["url"]
        invalid = not url.lower().startswith(("http://", "https://"))
        if invalid:
            result, basis, status = "error", "", 0
        elif url in cache:
            result, basis, status = cache[url]
        else:
            result, basis, status, new_state = check_one(url, state.get(url))
            state[url] = new_state
            cache[url] = (result, basis, status)
            if i < len(rows) - 1:
                time.sleep(PAUSE)
        s = state.get(url, {})
        if url in cache or result == "error":
            s["last_checked"] = now_iso()
            state[url] = s
        readable = result not in ("error", "manual")
        all_lines = s.get("key_lines", []) if readable else []
        shown = visible_lines(all_lines)
        if invalid:
            note = "The Website cell is not a web address. It must start with http:// or https://"
        elif readable:
            note = date_warning(all_lines)        # looks at ALL sentences, including old ones
        else:
            note = problem_note(result, status)
        out_rows.append({
            "name": name,
            "url": url,
            "check_result": result,
            "basis": basis,
            "signal": signal_for(all_lines, shown) if readable else "",
            "last_checked": now_iso(),
            "last_changed": s.get("last_changed", ""),
            "key_lines": " | ".join(shown)[:600],
            "previous_key_lines": " | ".join(s.get("prev_key_lines", []))[:600],
            "warning": note,
        })
        print(f"{result:8} {status:<4} {url}")

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), "utf-8")
    fields = ["name", "url", "check_result", "basis", "signal", "last_checked",
              "last_changed", "key_lines", "previous_key_lines", "warning"]
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
