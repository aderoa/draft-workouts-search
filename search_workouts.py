"""
Serper.dev → email digest.

Runs the configured query against the Serper.dev API (which returns real
Google search results), filters results to only those NOT seen in previous
runs (tracked in seen_urls.json), and emails the new results as an HTML
digest.

Why Serper instead of Google's Custom Search JSON API: Google deprecated
the "Search the entire web" option for new Programmable Search Engines in
March 2026 — new engines are limited to 50 specific domains. Serper.dev
returns true Google search results without that restriction.

Environment variables required:
  SERPER_API_KEY     — API key from serper.dev
  SMTP_USERNAME      — Gmail address to send FROM
  SMTP_PASSWORD      — Gmail app password (NOT the regular password)
  EMAIL_TO           — Recipient email address

The seen_urls.json file is committed back to the repo by the workflow so
results are deduplicated across runs.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ─── Configuration ───────────────────────────────────────────────
QUERY = 'draft workout -nfl'

# Time-restrict to past day. Serper supports Google's `tbs` parameter
# directly. 'qdr:d' = past 24 hours, 'qdr:d2' isn't a valid Google value
# (Google only takes h/d/w/m/y). We'll use 'qdr:d' for last 24h. With
# twice-daily runs and the dedup cache catching duplicates, missing the
# 24-hour boundary by a few hours isn't a real risk.
TIME_RESTRICT = 'qdr:d'

# Number of results to request. Serper returns up to 100 per call. 20 is
# plenty for the day window — most days have 0-5 actual workout reports.
NUM_RESULTS = 20

# Localization — request US/English to match what you'd see at google.com
GOOGLE_LOCALE = 'us'
GOOGLE_LANG = 'en'

SEEN_FILE = 'seen_urls.json'
# Cap to keep file size bounded — once we hit this, oldest URLs roll off
SEEN_CAP = 500


# ─── Serper.dev call ─────────────────────────────────────────────
def search(query: str, api_key: str, num: int = 20) -> list[dict]:
    """Hit the Serper.dev search API. Returns list of organic-result dicts."""
    url = 'https://google.serper.dev/search'
    headers = {
        'X-API-KEY': api_key,
        'Content-Type': 'application/json',
    }
    payload = {
        'q': query,
        'num': num,
        'tbs': TIME_RESTRICT,
        'gl': GOOGLE_LOCALE,
        'hl': GOOGLE_LANG,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get('organic', [])


# ─── Deduplication via JSON file ─────────────────────────────────
def load_seen() -> dict:
    """Returns dict: url -> timestamp_first_seen."""
    if not Path(SEEN_FILE).exists():
        return {}
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_seen(seen: dict) -> None:
    """Persist seen URLs. If we exceed SEEN_CAP, drop oldest entries."""
    if len(seen) > SEEN_CAP:
        sorted_items = sorted(seen.items(), key=lambda kv: kv[1])
        seen = dict(sorted_items[-SEEN_CAP:])
    with open(SEEN_FILE, 'w') as f:
        json.dump(seen, f, indent=2, sort_keys=True)


# ─── Email rendering + sending ───────────────────────────────────
def render_html(query: str, items: list[dict], total_seen: int) -> str:
    """Build the HTML body for the digest email."""
    if not items:
        body = (
            '<p style="color:#666">No new results in the last 24 hours.</p>'
            f'<p style="color:#999;font-size:.85em">Tracking {total_seen} URLs total.</p>'
        )
    else:
        rows = []
        for it in items:
            title = (it.get('title') or '(no title)').replace('<', '&lt;')
            url = it.get('link', '#')
            snippet = (it.get('snippet') or '').replace('<', '&lt;')
            # Serper exposes the source domain via `source` (sometimes); fall
            # back to parsing it out of the URL.
            display = it.get('source') or ''
            if not display and url:
                # Cheap host extraction
                try:
                    display = url.split('/')[2]
                except IndexError:
                    display = ''
            date = it.get('date', '')
            date_html = f' · <span style="color:#888">{date}</span>' if date else ''
            rows.append(f'''
                <div style="margin:0 0 1.5em;padding:0 0 1em;border-bottom:1px solid #eee">
                    <div style="font-size:1.05em;font-weight:600;margin-bottom:.2em">
                        <a href="{url}" style="color:#1a73e8;text-decoration:none">{title}</a>
                    </div>
                    <div style="font-size:.78em;color:#5e5e5e;margin-bottom:.4em">{display}{date_html}</div>
                    <div style="font-size:.92em;color:#333;line-height:1.4">{snippet}</div>
                </div>
            ''')
        body = ''.join(rows)

    now = datetime.now(timezone(timedelta(hours=-7))).strftime('%a %b %d, %I:%M %p PT')
    return f'''
        <html>
        <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:1.5em">
            <h2 style="color:#1a1a2e;margin:0 0 .3em">Draft Workout Search</h2>
            <div style="color:#666;font-size:.88em;margin-bottom:1.5em">
                Query: <code style="background:#f3f4f6;padding:1px 5px;border-radius:3px">{query}</code> · {now} · {len(items)} new result{'s' if len(items) != 1 else ''}
            </div>
            {body}
            <div style="margin-top:2em;padding-top:1em;border-top:1px solid #eee;font-size:.78em;color:#999">
                Sent automatically by HoopsMatic / draft-workout-search workflow. {total_seen} URLs in dedup cache.
            </div>
        </body>
        </html>
    '''


def send_email(subject: str, html_body: str,
               smtp_user: str, smtp_pass: str, to_addr: str) -> None:
    """Send via Gmail SMTP. Uses TLS on port 587."""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = to_addr
    msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


# ─── Main flow ───────────────────────────────────────────────────
def main() -> int:
    required = {
        'SERPER_API_KEY': os.environ.get('SERPER_API_KEY', ''),
        'SMTP_USERNAME':  os.environ.get('SMTP_USERNAME', ''),
        'SMTP_PASSWORD':  os.environ.get('SMTP_PASSWORD', ''),
        'EMAIL_TO':       os.environ.get('EMAIL_TO', ''),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f'ERROR: missing env vars: {missing}', file=sys.stderr)
        return 1

    print(f'Query: "{QUERY}"')
    items = search(QUERY, required['SERPER_API_KEY'], NUM_RESULTS)
    print(f'Got {len(items)} total results from Serper')

    seen = load_seen()
    new_items = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in items:
        url = it.get('link')
        if not url:
            continue
        if url in seen:
            continue
        new_items.append(it)
        seen[url] = now_iso

    print(f'After dedup: {len(new_items)} NEW results')

    save_seen(seen)

    # Always email so successful runs are visible — empty digests confirm
    # the workflow is healthy.
    subject_count = f'({len(new_items)} new)' if new_items else '(no new)'
    subject = f'NBA draft workouts {subject_count}'
    html = render_html(QUERY, new_items, len(seen))

    try:
        send_email(subject, html,
                   required['SMTP_USERNAME'],
                   required['SMTP_PASSWORD'],
                   required['EMAIL_TO'])
        print(f'✓ Sent digest to {required["EMAIL_TO"]}')
    except Exception as e:
        print(f'ERROR sending email: {e}', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
