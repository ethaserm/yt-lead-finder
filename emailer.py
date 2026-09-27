#!/usr/bin/env python3
"""Cold-outreach emailer for the video editing side hustle. Runs on GitHub Actions, no Claude session involved.

Each run:
  1. Reads the outreach inbox over IMAP (same Gmail App Password) and logs new replies / bounces / opt-outs to the
     "Replies" tab, moving the matching "Outreach Tracker" row's Status on. Opt-outs and bounces are then never
     emailed again.
  2. Works out how many emails it may send right now: today's cap comes from the ramp on the Lists tab (15/day in
     week 1 and up from there), spread evenly over the day's scheduled runs so nothing goes out in one burst.
  3. Sends to "Business Queue" rows whose Status is Pending (after de-duplicating against Outreach Tracker and the
     suppression list), through Gmail SMTP with the dedicated outreach address + App Password, logs every send to
     Outreach Tracker and marks the queue row Emailed.

Hard gate: nothing is sent unless a template on the "Templates" tab is marked Ready? = Yes. The pitch and prices are
written by Ethan in the Sheet (private), not in this public repo.

Secrets: SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_SENDER_ADDRESS, GMAIL_APP_PASSWORD.
Run:  python emailer.py --dry-run   (reads everything, sends nothing, writes nothing; preview in output/)
      python emailer.py             (real run)
"""
import argparse
import email
import html
import email.policy
import imaplib
import math
import os
import random
import re
import smtplib
import string
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime

import leadfinder as lf

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - zoneinfo is in the stdlib from 3.9
    LONDON = timezone.utc

QUEUE_TAB, TRACKER_TAB, REPLIES_TAB, TEMPLATES_TAB, LISTS_TAB = (
    "Business Queue", "Outreach Tracker", "Replies", "Templates", "Lists")
TRACKER_HEADER = ["Channel Name", "Niche / Category", "Channel URL", "Contact Email", "Source", "Date Emailed",
                  "Follow-up 1 Sent", "Follow-up 2 Sent", "Status", "Response Date", "What They Bought", "Amount £",
                  "Notes", "Template Used", "Hook Type", "Message ID"]
REPLIES_HEADER = ["Date Received", "From Email", "Channel Name", "Subject", "Reply Snippet", "Type", "Handled?",
                  "Notes", "Message ID"]
TEMPLATE_FIELDS = {"channel_name", "niche", "subscribers", "subscribers_short"}
PLACEHOLDER_MARKERS = ("[not written", "[to be", "to be set", "tbd")
DEFAULT_RAMP = [(1, 15), (8, 20), (15, 30), (22, 40), (29, 50)]
AUTO_STATUSES = {"", "sent - awaiting reply", "follow-up 1 sent", "follow-up 2 sent", "auto-reply"}
OPT_OUT_RE = re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|messaging)|"
                        r"do not (email|contact)|don'?t (email|contact)|opt[- ]?out)\b", re.I)


def log(msg):
    print(msg, flush=True)


def env(name, default=""):
    return lf.clean_secret(os.environ.get(name, default))


def london_today(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(LONDON).date()


# --------------------------------------------------------------------------- sheet helpers
def find_row(values, label):
    label = label.lower()
    for i, row in enumerate(values[:10]):
        if any(str(c).strip().lower() == label for c in row):
            return i
    return None


def col(header, *names):
    wanted = [n.lower() for n in names]
    for i, cell in enumerate(header):
        if str(cell).strip().lower() in wanted:
            return i
    return None


def cell(row, i):
    return str(row[i]).strip() if i is not None and i < len(row) else ""


def col_letter(n):
    return lf._col_letter(n)


def is_yes(value):
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "ready", "on")


# --------------------------------------------------------------------------- templates
class Template:
    def __init__(self, name, ready, subject, body, hook):
        self.name, self.ready, self.subject, self.body, self.hook = name, ready, subject, body, hook

    def problems(self):
        issues = []
        if not self.subject or not self.body:
            issues.append("subject or body is empty")
        text = (self.subject + " " + self.body).lower()
        if any(m in text for m in PLACEHOLDER_MARKERS):
            issues.append("still has placeholder text")
        for part in (self.subject, self.body):
            try:
                fields = {f for _, f, _, _ in string.Formatter().parse(part) if f is not None}
            except ValueError as exc:
                issues.append(f"bad {{placeholder}} syntax ({exc})")
                continue
            unknown = fields - TEMPLATE_FIELDS
            if unknown:
                issues.append("unknown placeholder(s): " + ", ".join(sorted("{" + u + "}" for u in unknown)))
        return issues

    def render(self, lead):
        values = {
            "channel_name": lead["name"], "niche": lead["niche"] or "YouTube",
            "subscribers": f"{lead['subs']:,}" if lead["subs"] else "",
            "subscribers_short": short_count(lead["subs"]),
        }
        return self.subject.format_map(values).strip(), self.body.format_map(values).strip() + "\n"


def short_count(n):
    if not n:
        return ""
    if n >= 1000:  # YouTube style: 23,400 -> 23.4k, 12,000 -> 12k
        k = n / 1000
        return (f"{k:.1f}".rstrip("0").rstrip(".") if k < 100 else str(round(k))) + "k"
    return str(n)


def read_templates(values):
    h = find_row(values, "template")
    if h is None:
        return []
    hdr = values[h]
    i_name, i_ready, i_subj = col(hdr, "template"), col(hdr, "ready?", "ready"), col(hdr, "subject")
    i_body, i_hook = col(hdr, "body"), col(hdr, "hook type", "hook")
    out = []
    for row in values[h + 1:]:
        name = cell(row, i_name)
        if not name:
            continue
        out.append(Template(name, is_yes(cell(row, i_ready)), cell(row, i_subj),
                            str(row[i_body]) if i_body is not None and i_body < len(row) else "",
                            cell(row, i_hook) or "None (generic)"))
    return out


def pick_template(ready, use_counts):
    """Least-used ready template first, so several templates rotate evenly (a simple A/B split)."""
    return min(ready, key=lambda t: (use_counts.get(t.name, 0), ready.index(t)))


# --------------------------------------------------------------------------- tracker + ramp
class Tracker:
    def __init__(self, values):
        self.values = values
        self.h = find_row(values, "channel name")
        hdr = values[self.h] if self.h is not None else TRACKER_HEADER
        self.header = hdr
        self.i = {k: col(hdr, *names) for k, names in {
            "name": ("channel name",), "url": ("channel url",), "email": ("contact email",),
            "date": ("date emailed",), "status": ("status",), "resp": ("response date",),
            "template": ("template used", "subject variant"), "msgid": ("message id",)}.items()}
        self.emails, self.ids, self.name_email, self.use_counts = set(), set(), set(), {}
        self.dates, self.by_msgid, self.by_email = [], {}, {}
        for off, row in enumerate(values[(self.h or 0) + 1:] if self.h is not None else []):
            rownum = (self.h or 0) + 2 + off
            em = cell(row, self.i["email"]).lower()
            if em:
                self.emails.add(em)
                self.by_email.setdefault(em, rownum)
            self.ids.update(lf.CHANNEL_ID_RE.findall(cell(row, self.i["url"])))
            if em and cell(row, self.i["name"]):
                self.name_email.add((lf.norm(cell(row, self.i["name"])), em))
            t = cell(row, self.i["template"])
            if t:
                self.use_counts[t] = self.use_counts.get(t, 0) + 1
            d = lf.parse_sheet_date(cell(row, self.i["date"]))
            if d:
                self.dates.append(d)
            mid = cell(row, self.i["msgid"])
            if mid:
                self.by_msgid[mid.strip("<>").lower()] = rownum

    def row(self, rownum):
        return self.values[rownum - 1] if 0 < rownum <= len(self.values) else []

    def status_of(self, rownum):
        return cell(self.row(rownum), self.i["status"])


def daily_cap(lists, first_send, today):
    st = lists.get("settings", {})
    override = lf._to_int(st.get("daily cap override"))
    if override is not None:
        return override
    ramp = sorted(lists.get("ramp") or DEFAULT_RAMP)
    day_n = (today - first_send).days + 1 if first_send else 1
    cap = ramp[0][1]
    for from_day, c in ramp:
        if day_n >= from_day:
            cap = c
    return cap


def parse_window(spec):
    """'12-21' -> [12..21] (UTC hours the send workflow is scheduled for)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return list(range(24))
    a, b = int(m.group(1)), int(m.group(2))
    return list(range(a, b + 1)) if a <= b else list(range(a, 24)) + list(range(0, b + 1))


def send_budget(lists, tracker, now, window, max_per_run):
    """(today's cap, sent today, allowed this run). The day's remaining allowance is split evenly over the runs
    still scheduled today, so e.g. 15/day over 10 runs goes out 1-2 at a time."""
    today = london_today(now)
    sent_today = sum(1 for d in tracker.dates if d == today)
    cap = daily_cap(lists, min(tracker.dates) if tracker.dates else None, today)
    left = max(cap - sent_today, 0)
    hour = now.astimezone(timezone.utc).hour
    runs_left = sum(1 for h in window if h >= hour) or 1
    this_run = min(math.ceil(left / runs_left), max_per_run, left)
    return cap, sent_today, this_run


# --------------------------------------------------------------------------- queue selection
def pick_leads(values, tracker, sup_emails, sup_domains, limit):
    """Pending rows in sheet order. Returns (to_send, fixes); fixes are (channel_id, status, note) for rows that must
    not be emailed (already contacted, suppressed, unusable)."""
    h = lf.find_header(values)
    if h is None:
        return [], []
    hdr = values[h]
    ix = {k: lf.col_index(hdr, k) for k in ("name", "niche", "subs", "url", "email", "status", "source")}
    to_send, fixes, seen = [], [], set()
    for row in values[h + 1:]:
        if cell(row, ix["status"]).lower() != "pending":
            continue
        ids = lf.CHANNEL_ID_RE.findall(cell(row, ix["url"]))
        if not ids:
            continue
        cid, em = ids[0], cell(row, ix["email"]).lower()
        name = cell(row, ix["name"])
        lead = {"id": cid, "name": name, "niche": cell(row, ix["niche"]), "url": cell(row, ix["url"]),
                "email": em, "source": cell(row, ix["source"]) or lf.SOURCE_LABEL,
                "subs": lf._to_int(cell(row, ix["subs"])) or 0}
        if not lf.EMAIL_RE.fullmatch(em or "") or lf.is_junk_email(em):
            fixes.append((cid, "Skipped", "emailer: contact email looks unusable"))
        elif cid in tracker.ids and em in tracker.emails:
            fixes.append((cid, "Emailed", "emailer: already in Outreach Tracker"))
        elif em in tracker.emails or cid in tracker.ids or (lf.norm(name), em) in tracker.name_email:
            fixes.append((cid, "Skipped", "emailer: already contacted"))
        elif em in sup_emails or lf.registrable_domain(lf.domain_of(em)) in sup_domains:
            fixes.append((cid, "Skipped", "emailer: bounced / opted out before"))
        elif em in seen:
            fixes.append((cid, "Skipped", "emailer: same email as another queued channel"))
        elif len(to_send) < limit:
            seen.add(em)
            to_send.append(lead)
    return to_send, fixes


# --------------------------------------------------------------------------- sending
def build_message(sender, sender_name, to, subject, body):
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or "gmail.com")
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(body)
    return msg


class SendFailed(Exception):
    pass


class SendLimit(Exception):
    pass


class Mailer:
    """Gmail SMTP with an App Password. Host/port/SSL can be overridden (SMTP_HOST etc.) for local testing."""

    def __init__(self, sender, password):
        self.sender, self.password = sender, password.replace(" ", "")
        self.host = env("SMTP_HOST", "smtp.gmail.com")
        self.port = int(env("SMTP_PORT", "465"))
        self.ssl = lf.env_flag("SMTP_SSL", True)
        self.conn = None

    def __enter__(self):
        if self.ssl:
            self.conn = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            self.conn = smtplib.SMTP(self.host, self.port, timeout=30)
        if self.password:
            self.conn.login(self.sender, self.password)
        return self

    def __exit__(self, *exc):
        try:
            self.conn.quit()
        except Exception:  # noqa: BLE001
            pass

    def send(self, msg):
        try:
            self.conn.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendFailed("recipient refused by Gmail") from exc
        except smtplib.SMTPDataError as exc:
            if exc.smtp_code in (421, 450, 451, 452, 550) and b"limit" in (exc.smtp_error or b"").lower():
                raise SendLimit(f"Gmail sending limit ({exc.smtp_code})") from exc
            raise SendFailed(f"SMTP {exc.smtp_code}") from exc


# --------------------------------------------------------------------------- replies (IMAP)
def plain_body(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part else ""
        if part is not None and part.get_content_type() == "text/html":
            text = html.unescape(lf.TAG_RE.sub(" ", lf.CODE_RE.sub(" ", text)))
        return text or ""
    except Exception:  # noqa: BLE001 - odd MIME shouldn't stop the run
        payload = msg.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def strip_quoted(body):
    m = re.search(r"\n(On .{5,200}wrote:|-{2,} ?Original Message|From: .+\n(Sent|Date): )", body, re.I)
    text = body[:m.start()] if m else body
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def classify(msg, from_addr, body):
    subject = str(msg.get("Subject", "") or "")
    if re.search(r"mailer-daemon|postmaster", from_addr, re.I) or re.search(
            r"delivery status notification|undeliverable|undelivered|mail delivery (failed|subsystem)|"
            r"returned mail|failure notice", subject, re.I):
        return "Delay" if re.search(r"\(delay\)|delivery incomplete|will retry|temporar",
                                    subject + " " + body[:400], re.I) else "Bounce"
    auto = str(msg.get("Auto-Submitted", "") or "").lower()
    prec = str(msg.get("Precedence", "") or "").lower()
    if ((auto and auto != "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond") or prec == "auto_reply"
            or re.match(r"\s*(automatic reply|auto[- ]?reply|autoreply|out of (the )?office)", subject, re.I)):
        return "Auto-reply"
    if OPT_OUT_RE.search(strip_quoted(body)[:800]):
        return "Opt-out"
    return "Reply"


def failed_address(msg, raw_text, body):
    header = str(msg.get("X-Failed-Recipients", "") or "").strip()
    if header:
        return header.split(",")[0].strip().lower()
    m = re.search(r"Final-Recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?", raw_text, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"(?:delivered to|message to|address(?: not found)?:?|recipient)\s*<?([A-Za-z0-9._%+\-]+@"
                  r"[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?", body, re.I)
    return m.group(1).lower() if m else ""


def next_status(current, kind):
    cur = (current or "").strip()
    auto = cur.lower() in AUTO_STATUSES
    if kind == "Opt-out":
        return "" if cur in ("Bought", "Opt-out / Do Not Contact") else "Opt-out / Do Not Contact"
    if not auto:
        return ""
    return {"Reply": "Replied - Needs Review", "Bounce": "Bounced",
            "Auto-reply": "" if cur == "Auto-reply" else "Auto-reply"}.get(kind, "")


def safe(value):
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def process_messages(raw_messages, me, tracker, known_ids):
    """raw_messages: list of bytes. Returns (new Replies rows, tracker updates [(row, status, response_date)])."""
    rows, updates = [], []
    for raw in raw_messages:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        mid = str(msg.get("Message-ID", "") or "").strip().strip("<>").lower()
        if not mid or mid in known_ids:
            continue
        from_addr = parseaddr(str(msg.get("From", "")))[1].lower()
        if from_addr == me:
            continue
        body = plain_body(msg)
        kind = classify(msg, from_addr, body)
        if kind == "Delay":
            continue
        refs = re.findall(r"<([^>]+)>", " ".join(str(msg.get(h, "") or "") for h in ("In-Reply-To", "References")))
        rownum = next((tracker.by_msgid[r.lower()] for r in refs if r.lower() in tracker.by_msgid), None)
        related = from_addr
        if kind == "Bounce":
            related = failed_address(msg, raw.decode("utf-8", "replace"), body)
            rownum = rownum or tracker.by_email.get(related)
        rownum = rownum or tracker.by_email.get(from_addr)
        if not rownum:
            continue  # not about this outreach (newsletters, Google alerts...)
        known_ids.add(mid)
        try:
            received = parsedate_to_datetime(str(msg.get("Date"))).astimezone(LONDON)
        except (TypeError, ValueError):
            received = datetime.now(LONDON)
        contacted = cell(tracker.row(rownum), tracker.i["email"]).lower()
        note = f"Failed address: {related}" if kind == "Bounce" and related else (
            f"Contacted address: {contacted}" if contacted and contacted != from_addr else "")
        rows.append([received.strftime("%Y-%m-%d %H:%M"), safe(from_addr),
                     safe(cell(tracker.row(rownum), tracker.i["name"])), safe(str(msg.get("Subject", ""))[:200]),
                     safe(re.sub(r"\s+", " ", strip_quoted(body)).strip()[:300]), kind, "No", note, mid])
        status = next_status(tracker.status_of(rownum), kind)
        if status:
            resp = received.date().isoformat() if kind in ("Reply", "Opt-out") else ""
            updates.append((rownum, status, resp))
            row = tracker.row(rownum)
            if tracker.i["status"] is not None and tracker.i["status"] < len(row):
                row[tracker.i["status"]] = status  # so a second message in this run sees the new status
    return rows, updates


def fetch_inbox(sender, password, days):
    imap = imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com"))
    try:
        imap.login(sender, password.replace(" ", ""))
        imap.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, f"(SINCE {since})")
        ids = (data[0] or b"").split()[-200:] if typ == "OK" else []
        out = []
        for num in ids:
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            if typ == "OK" and parts and isinstance(parts[0], tuple):
                out.append(parts[0][1])
        return out
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def check_replies(sh, sender, password, dry_run, days=14):
    tracker_ws, replies_ws = sh.worksheet(TRACKER_TAB), sh.worksheet(REPLIES_TAB)
    tracker = Tracker(tracker_ws.get_all_values())
    rvals = replies_ws.get_all_values()
    rh = find_row(rvals, "message id")
    i_mid = col(rvals[rh], "message id") if rh is not None else None
    known = {cell(r, i_mid).strip("<>").lower() for r in rvals[(rh or 0) + 1:]} if i_mid is not None else set()
    raw = fetch_inbox(sender, password, days)
    rows, updates = process_messages(raw, sender.lower(), tracker, known)
    log(f"inbox: {len(raw)} messages in the last {days} days, {len(rows)} new replies/bounces/opt-outs for the "
        f"tracker, {len(updates)} status changes")
    if dry_run or not (rows or updates):
        return
    if rows:
        lf.append_rows(replies_ws, rows)
    data = []
    for rownum, status, resp in updates:
        data.append({"range": f"{col_letter(tracker.i['status'] + 1)}{rownum}", "values": [[status]]})
        if resp and tracker.i["resp"] is not None and not cell(tracker.row(rownum), tracker.i["resp"]):
            data.append({"range": f"{col_letter(tracker.i['resp'] + 1)}{rownum}", "values": [[resp]]})
    if data:
        tracker_ws.batch_update(data, value_input_option="USER_ENTERED")


# --------------------------------------------------------------------------- queue status write-back
def apply_queue_fixes(ws, fixes):
    """fixes: [(channel_id, status, note)]. Re-reads the queue so row numbers are current, then one batch write."""
    if not fixes:
        return 0
    values = ws.get_all_values()
    h = lf.find_header(values)
    hdr = values[h]
    i_url, i_status, i_why = lf.col_index(hdr, "url"), lf.col_index(hdr, "status"), lf.col_index(hdr, "why")
    row_of = {}
    for off, row in enumerate(values[h + 1:]):
        ids = lf.CHANNEL_ID_RE.findall(cell(row, i_url))
        if ids and cell(row, i_status).lower() == "pending":
            row_of.setdefault(ids[0], (h + 2 + off, cell(row, i_why)))
    data = []
    for cid, status, note in fixes:
        if cid not in row_of:
            continue
        r, why = row_of[cid]
        data.append({"range": f"{col_letter(i_status + 1)}{r}", "values": [[status]]})
        if i_why is not None:
            data.append({"range": f"{col_letter(i_why + 1)}{r}",
                         "values": [[lf.sheet_text((why + "; " if why else "") + note)[:480]]]})
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    return len(data)


# --------------------------------------------------------------------------- main
def run(dry_run, max_per_run, window):
    sheet_ok = env("SHEET_ID") and env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sender, password = env("GMAIL_SENDER_ADDRESS"), env("GMAIL_APP_PASSWORD")
    if not sheet_ok:
        log("::warning::Setup not finished - SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON secrets missing. Nothing done.")
        return 0
    if not dry_run and not (sender and password):
        log("::warning::Setup not finished - GMAIL_SENDER_ADDRESS / GMAIL_APP_PASSWORD secrets missing. Nothing sent.")
        return 0
    sh = lf.open_sheet()
    lists = lf.read_lists_settings(sh.worksheet(LISTS_TAB).get_all_values())
    st = lists["settings"]
    if is_yes(st.get("emailer paused")) and not dry_run:
        log("Emailer paused on the Lists tab - nothing sent.")
        return 0

    if sender and password and not st.get("check replies", "yes").lower().startswith("n"):
        try:
            check_replies(sh, sender, password, dry_run)
        except Exception as exc:  # noqa: BLE001 - leads in the queue were never emailed, so sending stays safe
            log(f"::warning::Couldn't check the inbox over IMAP ({type(exc).__name__}: {str(exc)[:120]}); "
                "continuing without it")

    tracker_ws, queue_ws = sh.worksheet(TRACKER_TAB), sh.worksheet(QUEUE_TAB)
    tracker = Tracker(tracker_ws.get_all_values())
    cfg = lf.load_config()
    sup_emails, sup_domains = lf.load_suppression(sh, REPLIES_TAB, set(cfg["freemail"]))
    templates = read_templates(sh.worksheet(TEMPLATES_TAB).get_all_values())
    ready = [t for t in templates if t.ready and not t.problems()]
    for t in templates:
        if t.ready and t.problems():
            log(f"::warning::Template '{t.name}' is marked Ready but can't be used: {'; '.join(t.problems())}")

    now = datetime.now(timezone.utc)
    cap, sent_today, allowed = send_budget(lists, tracker, now, window, max_per_run)
    to_send, fixes = pick_leads(queue_ws.get_all_values(), tracker, sup_emails, sup_domains, allowed)
    log(f"today's cap {cap} (ramp), sent today {sent_today}, this run may send {allowed}; "
        f"{len(to_send)} lead(s) picked, {len(fixes)} queue row(s) to tidy (dupes / suppressed)")
    if not ready:
        log("::notice::No template on the Templates tab is marked Ready? = Yes - nothing will be sent until Ethan "
            "writes and approves the pitch.")

    preview, sent = [], 0
    os.makedirs(os.path.join(lf.HERE, "output"), exist_ok=True)
    if dry_run or not ready or not to_send:
        for lead in to_send:
            t = pick_template(ready, tracker.use_counts) if ready else None
            subj, body = t.render(lead) if t else ("(no ready template)", "(no ready template)\n")
            preview.append(f"To: {lead['email']}\nChannel: {lead['name']} ({lead['subs']:,} subs)\n"
                           f"Template: {t.name if t else '-'}\nSubject: {subj}\n\n{body}\n{'-' * 60}\n")
            log(f"  would email: {lead['name']} ({lead['subs']:,} subs) with template {t.name if t else '-'}")
        with open(os.path.join(lf.HERE, "output", "emails_preview.txt"), "w", encoding="utf-8") as fh:
            fh.write("".join(preview) or "(nothing to send this run)\n")
        if not dry_run:
            apply_queue_fixes(queue_ws, fixes)
        log(f"{'dry run - ' if dry_run else ''}0 sent")
        return 0

    sender_name = st.get("sender name", "")
    done = []
    try:
        with Mailer(sender, password) as mailer:
            for n, lead in enumerate(to_send):
                t = pick_template(ready, tracker.use_counts)
                subject, body = t.render(lead)
                msg = build_message(sender, sender_name, lead["email"], subject, body)
                try:
                    mailer.send(msg)
                except SendFailed as exc:
                    log(f"  ! {lead['name']}: {exc}")
                    fixes.append((lead["id"], "Skipped", f"emailer: {exc}"))
                    continue
                sent += 1
                tracker.use_counts[t.name] = tracker.use_counts.get(t.name, 0) + 1
                today = london_today().isoformat()
                row = [lf.sheet_text(lead["name"]), lf.sheet_text(lead["niche"]), lead["url"],
                       lf.sheet_text(lead["email"]), lf.sheet_text(lead["source"]), today, "", "",
                       "Sent - Awaiting Reply", "", "", "", "", lf.sheet_text(t.name), lf.sheet_text(t.hook),
                       lf.sheet_text(str(msg["Message-ID"]).strip("<>"))]
                lf.append_rows(tracker_ws, [row])
                done.append((lead["id"], "Emailed", f"emailed {today} ({t.name})"))
                log(f"  sent: {lead['name']} ({lead['subs']:,} subs) - template {t.name}")
                if n < len(to_send) - 1:
                    time.sleep(random.randint(int(env("MIN_GAP_SECONDS", "45")), int(env("MAX_GAP_SECONDS", "120"))))
    except SendLimit as exc:
        log(f"::warning::Stopping: {exc}")
    except smtplib.SMTPAuthenticationError:
        log("::error::Gmail rejected the login - check GMAIL_SENDER_ADDRESS / GMAIL_APP_PASSWORD (2-Step "
            "Verification must be on and the App Password must be for this account).")
        apply_queue_fixes(queue_ws, done + fixes)
        return 1
    apply_queue_fixes(queue_ws, done + fixes)
    log(f"{sent} sent, {len(fixes)} queue rows tidied, {max(cap - sent_today - sent, 0)} left today")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read everything, send nothing, write nothing")
    ap.add_argument("--max-per-run", type=int, default=lf.env_int("MAX_PER_RUN", 6))
    args = ap.parse_args()
    window = parse_window(os.environ.get("SEND_WINDOW_UTC", "12-21"))
    sys.exit(run(args.dry_run or lf.env_flag("DRY_RUN", False), args.max_per_run, window))


if __name__ == "__main__":
    main()
