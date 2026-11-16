#!/usr/bin/env python3
"""ICAI Western Region / Nashik batch monitor.

Checks all course options on the public Launch Batch Details page and sends
Telegram notifications only for newly discovered batches. First successful
check records a baseline so existing batches don't trigger false 'new' alerts.
""" 
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

SITE_URL = "https://www.icaionlineregistration.org/launchbatchdetail.aspx"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "debug"))
POLL_MINUTES = max(5, int(os.environ.get("POLL_MINUTES", "5")))
LOG = logging.getLogger("icai_monitor")

EMPTY_PATTERNS = (
    "no records found", "no record found", "no data found",
    "no records to display", "no record to display", "no batch found",
    "no batches found", "no batch available", "no batches available",
    "no result found", "no results found", "no matching records",
    "no data available", "no data to display", "no data exists",
    "no batches to display", "no batches to show", "no batch launched",
    "no batch has been launched", "currently no batches", "batch details not available",
)
PLACEHOLDER_PATTERNS = (
    "select", "--select--", "-- select --", "choose", "choose one", "please select",
)
TRANSIENT_COLUMNS = (
    "seat", "capacity", "balance", "vacant", "available", "booking",
    "status", "registered", "waitlist", "serial", "sr no", "s.no",
)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def simplify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).casefold()).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def options_of(select) -> list[dict[str, Any]]:
    return select.locator("option").evaluate_all(
        "els => els.map(e => ({label: (e.textContent || '').trim(), value: e.value, disabled: e.disabled}))"
    )


def is_placeholder(opt: dict[str, Any]) -> bool:
    label = simplify(opt["label"])
    return bool(opt["disabled"] or not label or label in PLACEHOLDER_PATTERNS
                or label.startswith("select ") or label.startswith("please select "))


def find_select(page: Page, field: str):
    """Find the ASP.NET dropdown by id/name, falling back to displayed order."""
    selects = page.locator("select")
    count = selects.count()
    if count < 3:
        raise RuntimeError(f"Expected Region / Pou / Course selects, found {count}.")

    tokens = {"region": ("region", "rgn"),
              "pou": ("pou", "organising", "organizing", "branch"),
              "course": ("course",)}[field]
    matches = []
    for i in range(count):
        node = selects.nth(i)
        descriptor = simplify((node.get_attribute("id") or "") + " " +
                              (node.get_attribute("name") or ""))
        if any(token in descriptor for token in tokens):
            matches.append(i)
    if len(matches) == 1:
        return selects.nth(matches[0])
    if count == 3:
        return selects.nth({"region": 0, "pou": 1, "course": 2}[field])
    raise RuntimeError(f"Could not uniquely identify {field} dropdown; {count} selects found.")


def target_option(options: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    valid = [o for o in options if not is_placeholder(o)]
    if field == "region":
        exact = {"western region", "western", "western india regional council", "wirc", "wro"}
        matches = [o for o in valid if simplify(o["label"]) in exact]
        if not matches:
            matches = [o for o in valid if "western" in simplify(o["label"])
                       or re.search(r"\b(wirc|wro)\b", simplify(o["label"]))]
    elif field == "pou":
        matches = [o for o in valid if re.search(r"\b(nashik|nasik)\b", simplify(o["label"]))]
    else:
        raise ValueError(field)
    return matches[0] if matches else None


def wait_for_target(page: Page, field: str, seconds: int = 25) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    last_options: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            last_options = options_of(find_select(page, field))
            result = target_option(last_options, field)
            if result:
                return result
        except Exception:  # transient ASP.NET postback / DOM replacement
            pass
        page.wait_for_timeout(600)
    labels = [o["label"] for o in last_options]
    raise RuntimeError(f"{field} option not found. Current options: {labels}")


def select_and_settle(page: Page, field: str, opt: dict[str, Any]) -> None:
    LOG.info("Selecting %s: %s", field, opt["label"])
    find_select(page, field).select_option(value=opt["value"], timeout=20000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(1200)  # allow ASP.NET postback/UpdatePanel to settle


def get_courses(page: Page) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 25
    courses: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            courses = [o for o in options_of(find_select(page, "course")) if not is_placeholder(o)]
            if courses:
                break
        except Exception:
            pass
        page.wait_for_timeout(600)
    if not courses:
        raise RuntimeError("Course dropdown was not populated after choosing Nashik.")
    non_all = [o for o in courses if simplify(o["label"]) not in {"all", "all courses"}]
    return non_all or courses


def inspect_search_controls(page: Page) -> list[dict[str, str]]:
    """Return non-secret descriptors of potentially clickable form controls.

    The ICAI form may use image submits or unlabeled ASP.NET controls,
    which the previous watcher did not inspect.
    """
    try:
        return page.locator('input[type="submit"], input[type="button"], input[type="image"], button, a[role="button"]').evaluate_all(
            """els => els.filter(e => {
              const s = getComputedStyle(e);
              return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length > 0;
            }).map(e => ({
              tag: e.tagName.toLowerCase(), type: (e.type || '').toLowerCase(),
              id: e.id || '', name: e.name || '',
              value: (e.value || '').slice(0,80),
              label: ((e.textContent || '').trim()).slice(0,80),
              title: (e.title || '').slice(0,80), alt: (e.alt || '').slice(0,80),
              aria: (e.getAttribute('aria-label') || '').slice(0,80),
              src: (e.getAttribute('src') || '').split('/').pop().slice(0,80),
            }))"""
        )
    except Exception:
        return []


def search_action_score(action: dict[str, str]) -> int:
    """Score a form button without clicking navigation/login controls."""
    label = simplify(' '.join(str(action.get(k, '')) for k in
                              ('id', 'name', 'value', 'label', 'title', 'alt', 'aria', 'src')))
    compact = label.replace(' ', '')
    if (re.search(r"\b(login|logout|register|registration|cancel|reset|sign in|sign out|home|back)\b", label)
            or any(blocked in compact for blocked in ('login', 'logout', 'register', 'cancel', 'reset', 'signin', 'signout'))):
        return -100
    if re.search(r"\b(search|show|view|find|fetch|display|load|go|submit|lookup|batchdetails|btnsearch|btnshow)\b", label):
        return 100
    if 'batch' in label and any(word in label for word in ('detail', 'list', 'launch')):
        return 90
    if action.get('type') in ('submit','image') and not label:
        return 10  # common icon-only ASP.NET search button
    return 0


def click_search_if_present(page: Page) -> bool:
    """Click a batch-search control, including image and icon-only buttons."""
    selector = 'input[type="submit"], input[type="button"], input[type="image"], button, a[role="button"]'
    actions = page.locator(selector)
    descriptors = inspect_search_controls(page)
    LOG.info("Available visible form actions: %s", json.dumps(descriptors, ensure_ascii=False))
    candidates: list[tuple[int,int]] = []
    for i in range(actions.count()):
        node = actions.nth(i)
        try:
            if not node.is_visible() or not node.is_enabled():
                continue
            info = node.evaluate("""e => ({tag:e.tagName.toLowerCase(), type:(e.type||'').toLowerCase(),
                id:e.id||'',name:e.name||'',value:(e.value||'').slice(0,80),
                label:((e.textContent||'').trim()).slice(0,80),
                title:(e.title||'').slice(0,80),alt:(e.alt||'').slice(0,80),
                aria:(e.getAttribute('aria-label')||'').slice(0,80),
                src:(e.getAttribute('src')||'').split('/').pop().slice(0,80)})""")
            score = search_action_score(info)
            if score > 0:
                candidates.append((score,i))
        except Exception:
            continue
    if not candidates:
        LOG.warning('No button matching batch/search could be identified; checking for auto-updating results')
        return False
    candidates.sort(reverse=True)
    best_score, best_index = candidates[0]
    # For ambiguous, icon-only submits, only click when it is the sole candidate.
    if best_score == 10 and len(candidates) != 1:
        LOG.warning('Multiple unlabeled submit/image buttons; not guessing which one starts search')
        return False
    try:
        LOG.info('Clicking candidate batch search control (score=%s, index=%s)', best_score, best_index)
        actions.nth(best_index).click(timeout=15000)
        try:
            page.wait_for_load_state('domcontentloaded', timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        return True
    except Exception as exc:
        LOG.warning('Potential batch search control did not click successfully: %s', str(exc)[:160])
        return False


def parse_batch_html(html: str, source_url: str = SITE_URL) -> tuple[list[dict[str, Any]], bool]:
    """Extract data rows only from batch-result tables, never from the whole page.

    Returns (rows, confident_results_area_found).  The second value matters:
    zero results must never silently be treated as a successful check of a
    page whose markup changed.
    """
    soup = BeautifulSoup(html, "html.parser")
    full_text = simplify(soup.get_text(" ", strip=True))
    explicit_empty = any(phrase in full_text for phrase in EMPTY_PATTERNS)
    candidates: list[tuple[int, list[dict[str, Any]]]] = []

    for table in soup.find_all("table"):
        direct_rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        if not direct_rows:
            continue
        header_index = -1
        headers: list[str] = []
        score = 0
        for i, tr in enumerate(direct_rows[:5]):
            cells = tr.find_all(["td", "th"], recursive=False)
            labels = [clean(c.get_text(" ", strip=True)) for c in cells]
            keys = [simplify(x) for x in labels]
            if not 2 <= len(keys) <= 25:
                continue
            has_batch = any("batch" in x for x in keys)
            has_date = any("date" in x or "from" == x or "to" == x for x in keys)
            has_other = any(any(k in x for k in ("course", "pou", "time", "status", "venue", "seat", "capacity", "duration")) for x in keys)
            candidate_score = 4 * has_batch + 2 * has_date + has_other
            # A result grid must have at least a batch-related header and some additional metadata.
            if has_batch and (has_date or has_other) and candidate_score > score:
                score = candidate_score
                headers, header_index = labels, i
        if header_index < 0:
            continue

        batch_rows: list[dict[str, Any]] = []
        for tr in direct_rows[header_index + 1:]:
            cells = tr.find_all(["td", "th"], recursive=False)
            vals = [clean(c.get_text(" ", strip=True)) for c in cells]
            if len(vals) != len(headers) or not any(vals):
                continue
            if any(phrase in simplify(" ".join(vals)) for phrase in EMPTY_PATTERNS):
                continue
            if vals == headers:
                continue
            if len([v for v in vals if v]) < 2:
                continue
            fields = dict(zip(headers, vals))
            links = [urljoin(source_url, a["href"]) for a in tr.find_all("a", href=True)
                     if not a["href"].startswith(("#", "javascript:"))]
            batch_rows.append({"fields": fields, "links": list(dict.fromkeys(links))})
        candidates.append((score, batch_rows))

    if not candidates:
        return [], explicit_empty
    # Tables can be nested or duplicated. Keep candidates with maximum score.
    maximum = max(score for score, _ in candidates)
    rows = [row for score, batch_rows in candidates if score == maximum for row in batch_rows]
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        serial = json.dumps(row, ensure_ascii=False, sort_keys=True)
        dedup[serial] = row
    return list(dedup.values()), True



def wait_for_batch_results(page: Page, seconds: int = 20) -> tuple[list[dict[str, Any]], bool, str]:
    """Wait for the *result* rather than assuming ASP.NET has settled in 1.2 seconds.

    Some ICAI postbacks fetch the batch results asynchronously. Parse all frames
    (if the site uses an embedded results grid) but only accept a recognized
    batch grid or an explicit empty-result message. Unknown pages remain errors.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            for frame in page.frames:
                html = frame.content()
                batches, recognized = parse_batch_html(html, frame.url or page.url)
                if recognized:
                    return batches, True, frame.url or page.url
        except Exception:
            pass  # A frame may be navigating during a WebForms update.
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(800)
    return [], False, page.url


def describe_unrecognized_html(html: str) -> str:
    """Report compact, non-secret visible markup hints in logs for debugging."""
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.body or soup
    text = clean(body.get_text(' ', strip=True))
    tables = []
    for table in soup.select('table')[:10]:
        head = []
        for tr in table.select('tr')[:2]:
            vals = [clean(c.get_text(' ', strip=True))[:60]
                    for c in tr.find_all(['td', 'th'], recursive=False)]
            if vals:
                head.append(vals[:12])
        if head:
            tables.append(head)
    return json.dumps({'body_tail': text[-1300:], 'tables': tables[:6]}, ensure_ascii=False)


def batch_key(course: str, batch: dict[str, Any]) -> str:
    fields = batch["fields"]
    normalized = {simplify(k): clean(v) for k, v in fields.items()}
    strong_id = next((v for k, v in normalized.items()
                      if v and any(tag in k for tag in ("batch number", "batch no", "batch id", "batch code"))), None)
    if not strong_id:
        strong_id = next((v for k, v in normalized.items()
                          if v and k in ("batch", "batch name")), None)
    if strong_id:
        identity = {"course": simplify(course), "batch_id": simplify(strong_id)}
    else:
        identity = {"course": simplify(course), "fields": {
            k: simplify(v) for k, v in normalized.items()
            if not any(transient in k for transient in TRANSIENT_COLUMNS)
        }}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def capture_batches(diagnostic: bool = False) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)
            region = wait_for_target(page, "region")
            select_and_settle(page, "region", region)
            pou = wait_for_target(page, "pou")
            select_and_settle(page, "pou", pou)
            courses = get_courses(page)
            LOG.info("Discovered %s Nashik course options: %s", len(courses), [x["label"] for x in courses])
            if diagnostic:
                print("Region:", region["label"], "\nPOU:", pou["label"])
                print("Courses:", [x["label"] for x in courses])

            for course in courses:
                # Rebuild the filters each pass: ASP.NET postbacks may change fields/options.
                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)
                select_and_settle(page, "region", wait_for_target(page, "region"))
                select_and_settle(page, "pou", wait_for_target(page, "pou"))
                selected_courses = get_courses(page)
                match = next((o for o in selected_courses if simplify(o["label"]) == simplify(course["label"])), None)
                if not match:
                    raise RuntimeError(f"Course option disappeared after page reload: {course['label']}")
                select_and_settle(page, "course", match)
                page.locator("#btn_getlist").click(timeout=20000)
                rows, valid, result_url = wait_for_batch_results(page, seconds=20)
                if not valid:
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    safe_label = re.sub(r"[^a-z0-9_-]", "_", simplify(course["label"]))[:50]
                    html = page.content()
                    (DEBUG_DIR / f"unrecognized_{safe_label}.html").write_text(html, encoding="utf-8")
                    for i, frame in enumerate(page.frames):
                        if frame == page.main_frame:
                            continue
                        try:
                            (DEBUG_DIR / f"unrecognized_{safe_label}_frame_{i}.html").write_text(frame.content(), encoding="utf-8")
                        except Exception:
                            pass
                    page.screenshot(path=str(DEBUG_DIR / f"unrecognized_{safe_label}.png"), full_page=True)
                    LOG.error("ICAI unrecognized page after selecting %s: %s", course['label'],
                              describe_unrecognized_html(html))
                    LOG.error("Visible form action details: %s", json.dumps(inspect_search_controls(page)))
                    try:
                        selected = page.locator('select').evaluate_all(
                            "els => els.map(e => ({id:e.id, value:e.value, selected:(e.selectedOptions[0] || {}).textContent || ''}))")
                        LOG.error("Selected dropdown values at failure: %s", json.dumps(selected))
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Could not recognize a batch results table or a 'no batches' message for {course['label']} "
                        "after waiting 20 seconds. See the logged page summary and /data/debug/ screenshot/HTML. "
                        "No alerts or state changes were made."
                    )
                LOG.info("%s: %d batch rows", course["label"], len(rows))
                for batch in rows:
                    batch["course"] = course["label"]
                    batch["region"] = region["label"]
                    batch["pou"] = pou["label"]
                    collected[batch_key(course["label"], batch)] = batch

            return list(collected.values())
        finally:
            context.close()
            browser.close()


def telegram_send(text: str, *, chat_id: str | None = None, required: bool = True) -> None:
    """Send with secret in server environment only; never expose token in errors."""
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    recipient = (chat_id or os.getenv('TELEGRAM_CHAT_ID', '')).strip()
    if not token or not recipient:
        if required:
            raise RuntimeError('Configure the Telegram bot token and recipient chat ID.')
        return
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
            'chat_id': recipient, 'text': text, 'disable_web_page_preview': True,
        }, timeout=20)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError('Telegram transport error; verify the token and internet connection.') from None
    if not response.ok or not payload.get('ok'):
        description = str(payload.get('description', 'Unknown Telegram API error'))[:200]
        raise RuntimeError(f'Telegram could not deliver the message: {description}')


def telegram_recent_chats() -> list[dict[str, str]]:
    """Find private conversations that have interacted with the bot.

    Telegram getUpdates only lists recent updates, and does not work if a
    webhook is active. This never returns or persists the bot token.
    """
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Set TELEGRAM_BOT_TOKEN in the hosting environment first.')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/getUpdates',
                                 json={'limit': 100, 'timeout': 0}, timeout=20)
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('Telegram lookup failed. Check internet access and your bot token.') from None
    if not response.ok or not payload.get('ok'):
        raise RuntimeError('Telegram could not retrieve conversations. If a webhook is enabled, disable it first.')
    chats = {}
    for update in payload.get('result', []):
        msg = (update.get('message') or update.get('edited_message') or
               update.get('my_chat_member') or update.get('callback_query', {}).get('message') or {})
        chat = msg.get('chat') or {}
        if chat.get('type') != 'private' or not isinstance(chat.get('id'), int):
            continue
        user = msg.get('from') or {}
        label = clean(' '.join(str(chat.get(x) or user.get(x) or '') for x in ('first_name','last_name')))
        label = label or ('@' + chat['username'] if chat.get('username') else 'Private Telegram chat')
        chats[str(chat['id'])] = {'id': str(chat['id']), 'name': label[:80]}
    return list(chats.values())


def format_batch(batch: dict[str, Any]) -> str:
    lines = ["🚨 NEW ICAI NASHIK BATCH", f"Course: {batch['course']}",
             "Region: Western Region", "POU: Nashik", ""]
    for heading, value in batch["fields"].items():
        if clean(value):
            lines.append(f"{heading}: {value}")
    if batch["links"]:
        lines.extend(["", "Batch link: " + batch["links"][0]])
    lines.extend(["", "Check/register: " + SITE_URL])
    return "\n".join(lines)[:3900]


def read_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        raise RuntimeError(f"Invalid state file: {STATE_FILE}")
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def run_once(*, dry_run: bool = False, diagnostic: bool = False) -> int:
    # Fail early if Telegram isn't configured; do not consume browser work then fail.
    if not dry_run and (not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID")):
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
    batches = capture_batches(diagnostic=diagnostic)
    current = {batch_key(b["course"], b): b for b in batches}
    LOG.info("Total matching batches: %d", len(current))
    if dry_run:
        print(json.dumps(batches, indent=2, ensure_ascii=False))
        print("DRY RUN: no Telegram message and no state changes.")
        return len(current)

    previous = read_state()
    if previous is None:
        new_state = {"schema": 1, "started_at_utc": utc_now(),
                     "seen": {k: {"first_seen_utc": utc_now(), "batch": b} for k, b in current.items()}}
        telegram_send("✅ ICAI Nashik batch monitor is active.\n"
                      "Region: Western Region | POU: Nashik | All courses\n"
                      f"Existing batches saved: {len(current)}.\n"
                      "You'll be alerted only when a NEW batch appears.\n" + SITE_URL)
        save_state(new_state)
        LOG.info("Saved first-run baseline (%d batches).", len(current))
        return 0

    unseen = [(key, batch) for key, batch in current.items() if key not in previous["seen"]]
    for key, batch in unseen:
        telegram_send(format_batch(batch))
        LOG.info("Alert delivered for %s / %s", batch["course"], key)
        # Save after each successful delivery. If another send fails, retries won't
        # replay the already-delivered messages on a persistent local host.
        previous["seen"][key] = {"first_seen_utc": utc_now(), "batch": batch}
        save_state(previous)
    LOG.info("New batches: %d", len(unseen))
    return len(unseen)


def main() -> int:
    parser = argparse.ArgumentParser(description="ICAI Nashik batch Telegram alerts")
    parser.add_argument("--test-alert", action="store_true", help="Send a Telegram test message without scraping")
    parser.add_argument("--dry-run", action="store_true", help="Show detected batches; don't send or store")
    parser.add_argument("--diagnose", action="store_true", help="Print dropdown matching details")
    parser.add_argument("--watch", action="store_true", help="Stay running and check every POLL_MINUTES")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if args.test_alert:
        telegram_send("✅ ICAI Nashik Telegram test successful.\n" + SITE_URL)
        print("Test message delivered.")
        return 0
    if not args.watch:
        run_once(dry_run=args.dry_run, diagnostic=args.diagnose)
        return 0
    errors = 0
    while True:
        try:
            run_once(dry_run=args.dry_run, diagnostic=args.diagnose)
            if errors:
                telegram_send("✅ ICAI Nashik monitoring has recovered.", required=False)
            errors = 0
        except Exception as exc:
            errors += 1
            LOG.exception("Monitoring check failed (%d consecutive failures): %s", errors, exc)
            if errors in (1, 12, 72):
                try:
                    telegram_send(f"⚠️ ICAI Nashik monitor check failed ({errors} time(s)).\n"
                                  f"Error: {str(exc)[:450]}", required=False)
                except Exception:
                    LOG.exception("Could not send failure notification")
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.exception("Check failed: %s", exc)
        sys.exit(1)
