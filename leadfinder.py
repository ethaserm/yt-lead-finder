#!/usr/bin/env python3
"""YouTube creator lead finder for the video editing outreach.

Finds YouTube channels with 10k-50k subscribers through the official YouTube Data API v3 and appends them to the
"Business Queue" tab of the outreach Google Sheet. config.json holds the search terms, state.json remembers the
rotation cursor between runs, GitHub Actions runs it on a schedule.

Discovery (official API only, never scraping YouTube pages):
  search.list        100 units  recent long-form uploads for the next search term in the rotation
  channels.list        1 unit   subscriber count, About description, uploads playlist (50 channels per call)
  playlistItems.list   1 unit   the channel's latest uploads
  videos.list          1 unit   their descriptions + durations (50 videos per call)

Email rule (non-negotiable): an address is only used if it is written as plain visible text in the channel's
description, in the description of one of its recent videos, or on a linked personal site / link-in-bio page that
can be fetched normally (robots.txt respected). YouTube's CAPTCHA-gated "View email address" button is never
touched, no third-party email finder is used, and nothing is ever guessed or constructed. A channel with no
plain-text email is skipped (config: record_no_contact).

YouTube API data is not kept longer than 30 days: queue rows older than `refresh_after_days` are re-checked through
the API (or deleted if they no longer qualify), and channel IDs in state.json expire after `seen_ttl_days`.

Run:  python leadfinder.py --dry-run   (no Sheet writes, rotation unchanged, writes output/*.csv)
      python leadfinder.py             (appends to the Sheet, saves state.json)
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
API_BASE = "https://www.googleapis.com/youtube/v3/"
QUOTA_COST = {"search": 100, "channels": 1, "playlistItems": 1, "videos": 1}
UA = "YTLeadFinder/1.0 (small-business outreach tool; respects robots.txt)"
HTML_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
SITE_TIMEOUT = (5, 10)
MAX_PAGE_BYTES = 1_500_000

# Link-in-bio pages: the creator's own page on a shared host. Matched on the host (subdomains included).
LINK_HUB_HOSTS = ("linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "solo.to", "linkin.bio", "allmylinks.com",
                  "taplink.cc", "campsite.bio", "hoo.be", "carrd.co", "linkr.bio", "msha.ke", "stan.store",
                  "direct.me", "linkfly.to", "link.me", "tap.bio", "withkoji.com")
# Never fetched as a "personal site": platforms, stores, sponsors' usual hosts, shorteners (destination unknown).
SKIP_HOSTS = ("youtube.com", "youtu.be", "google.com", "goo.gl", "forms.gle", "twitter.com", "x.com",
              "instagram.com", "tiktok.com", "facebook.com", "fb.com", "fb.me", "twitch.tv", "discord.gg",
              "discord.com", "patreon.com", "reddit.com", "amazon.com", "amazon.co.uk", "amzn.to", "a.co",
              "bit.ly", "tinyurl.com", "t.co", "ow.ly", "rebrand.ly", "shorturl.at", "cutt.ly", "spotify.com",
              "apple.com", "steampowered.com", "steamcommunity.com", "epicgames.com", "roblox.com",
              "minecraft.net", "playstation.com", "xbox.com", "nintendo.com", "streamelements.com",
              "streamlabs.com", "ko-fi.com", "paypal.com", "paypal.me", "cash.app", "venmo.com",
              "buymeacoffee.com", "throne.com", "gofundme.com", "kick.com", "threads.net", "snapchat.com",
              "pinterest.com", "linkedin.com", "github.com", "medium.com", "teespring.com", "spri.ng",
              "fourthwall.com", "gg.gg", "curseforge.com", "modrinth.com", "planetminecraft.com", "gamebanana.com",
              "nexusmods.com", "wikipedia.org", "fandom.com", "imdb.com", "bsky.app", "whatnot.com", "etsy.com",
              "shopify.com", "myshopify.com", "streamyard.com", "obsproject.com", "elgato.com")
NAME_STOP = {"the", "and", "official", "channel", "tv", "yt", "gaming", "games", "gamer", "plays", "play", "ttv",
             "live", "hd", "lets", "let's", "videos", "clips"}
TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "com.au", "net.au", "org.au",
                      "co.nz", "co.za", "com.br", "co.in", "co.jp", "com.mx"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.I)
MAILTO_RE = re.compile(r"mailto(?::|%3A)([^\"'?&<>\s\\]+)", re.I)
CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
CODE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<!--.*?-->", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
HREF_RE = re.compile(r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)
CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_\-]{22}")

CONTACT_WORDS = ("business", "inquir", "enquir", "contact", "email", "e-mail", "mail me", "sponsor", "collab",
                 "partnership", "booking", "management", "managed by", "work with", "press", "reach me", "reach out")
SPONSOR_WORDS = ("code ", "promo code", "% off", "discount", "coupon", "use my link", "affiliate", "sponsored by",
                 "download", "install", "sign up", "free trial", "support@", "help@")
JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "abuse", "privacy",
              "legal", "dmca", "copyright", "unsubscribe"}
JUNK_MARKERS = ("sentry", "wixpress", "example.", "@example", "noreply", "no-reply", "donotreply", "yourname",
                "youremail", "email@email", "name@")
JUNK_DOMAINS = ("youtube.com", "google.com", "linktr.ee", "beacons.ai", "sentry.io", "wixpress.com",
                "cloudflare.com", "domain.com", "email.com", "test.com")
FILE_EXT_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "woff", "woff2", "ico", "mp4", "webm"}

HEADER_ALIASES = {
    "name": {"channel name", "channel"},
    "niche": {"niche / category", "niche/category", "niche", "category"},
    "subs": {"subscriber count", "subscribers", "subs"},
    "url": {"channel url", "url", "channel link"},
    "email": {"contact email", "email"},
    "email_source": {"email source"},
    "status": {"status"},
    "source": {"source"},
    "date": {"date added", "added"},
    "country": {"country"},
    "last_upload": {"last upload"},
    "last_checked": {"last checked"},
    "why": {"why", "notes"},
}
CANONICAL = {
    "name": "Channel Name", "niche": "Niche / Category", "subs": "Subscriber Count", "url": "Channel URL",
    "email": "Contact Email", "email_source": "Email Source", "status": "Status", "source": "Source",
    "date": "Date Added", "country": "Country", "last_upload": "Last Upload", "last_checked": "Last Checked",
    "why": "Why",
}
QUEUE_KEYS = ["name", "niche", "subs", "url", "email", "email_source", "status", "source", "date", "country",
              "last_upload", "last_checked", "why"]
DEFAULT_HEADER = [CANONICAL[k] for k in QUEUE_KEYS]
SOURCE_LABEL = "Lead Finder (YouTube API)"
STATUS_PENDING = "Pending"
STATUS_NO_CONTACT = "Review - no contact"
STATUS_UK = "Review - UK channel (PECR)"
STATUS_NON_EN = "Review - non-English"


# --------------------------------------------------------------------------- small helpers
def log(msg):
    print(msg, flush=True)


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_flag(name, default=False):
    val = str(os.environ.get(name, "")).strip().lower()
    return default if not val else val in ("1", "true", "yes", "on")


def clean_secret(value):
    return (value or "").strip().strip('"').strip("'").strip()


def utc_now():
    return datetime.now(timezone.utc)


def pacific_today():
    """YouTube's daily quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # zoneinfo missing: UTC-8 is close enough for bookkeeping
        return (utc_now() - timedelta(hours=8)).date().isoformat()


def iso_z(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_sheet_date(value):
    """Dates come back from the Sheet as dd/mm/yyyy (display format) or ISO; returns a date or None."""
    value = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_duration(value):
    """ISO 8601 duration (PT1H2M3S / P1DT2H) -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", str(value or ""))
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def name_keys(title, handle=""):
    """Normalised identifiers for a channel: full title, handle, and the title minus filler words."""
    keys = set()
    for raw in (title, (handle or "").lstrip("@")):
        n = norm(raw)
        if len(n) >= 4:
            keys.add(n)
        words = [w for w in re.findall(r"[a-z0-9]+", str(raw or "").lower()) if w not in NAME_STOP]
        core = "".join(words)
        if len(core) >= 4:
            keys.add(core)
    return keys


def host_of(url):
    try:
        host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def registrable_domain(host):
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def host_matches(host, patterns):
    return any(host == p or host.endswith("." + p) for p in patterns)


def domain_of(email):
    return email.rpartition("@")[2].lower()


def sheet_text(value):
    """Force text in a USER_ENTERED write (a leading apostrophe is hidden by Sheets and stops formula injection)."""
    value = str(value or "")
    return "'" + value if value else ""


def channel_url(cid):
    return f"https://www.youtube.com/channel/{cid}"


# --------------------------------------------------------------------------- quota + API client
class QuotaExhausted(Exception):
    pass


class BadPageToken(Exception):
    pass


class ApiError(Exception):
    pass


class Quota:
    """Tracks units spent today (Pacific day, as YouTube counts them) in state.json and per run."""

    def __init__(self, state, daily_budget, run_budget):
        q = state.setdefault("quota", {})
        today = pacific_today()
        if q.get("day") != today:
            q.clear()
            q.update(day=today, used=0)
        self.q, self.daily, self.run_budget, self.run_used = q, daily_budget, run_budget, 0

    def can(self, cost):
        return self.q["used"] + cost <= self.daily and self.run_used + cost <= self.run_budget

    def spend(self, cost):
        self.q["used"] += cost
        self.run_used += cost

    def exhaust_today(self):
        self.q["used"] = max(self.q["used"], self.daily)


class YouTube:
    def __init__(self, key, session, quota):
        self.key, self.session, self.quota = key, session, quota

    def _get(self, endpoint, params):
        cost = QUOTA_COST[endpoint]
        for attempt in range(3):
            if not self.quota.can(cost):
                raise QuotaExhausted(f"quota budget reached before {endpoint}")
            try:
                resp = self.session.get(API_BASE + endpoint, params=dict(params, key=self.key), timeout=20)
            except requests.RequestException as exc:  # never log the URL: it carries the API key
                self.quota.spend(cost)
                if attempt == 2:
                    raise ApiError(f"{endpoint}: network error {type(exc).__name__}")
                time.sleep(2 * (attempt + 1))
                continue
            self.quota.spend(cost)
            if resp.status_code == 200:
                return resp.json()
            reason = ""
            try:
                err = resp.json().get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
            except ValueError:
                pass
            if resp.status_code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
                self.quota.exhaust_today()
                raise QuotaExhausted(f"YouTube says {reason}")
            if resp.status_code == 400 and reason == "invalidPageToken":
                raise BadPageToken()
            if resp.status_code == 404:
                return {"items": []}
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise ApiError(f"{endpoint}: HTTP {resp.status_code} {reason}")
        raise ApiError(f"{endpoint}: gave up")

    def search(self, slot, window, page_token=None):
        params = {
            "part": "snippet", "type": "video", "q": slot["term"], "maxResults": 50, "order": slot["order"],
            "videoDuration": slot["duration"], "publishedAfter": window["published_after"],
            "publishedBefore": window["published_before"],
            "fields": "nextPageToken,items(snippet(channelId,channelTitle))",
        }
        for key, val in (("videoCategoryId", slot.get("category_id")), ("relevanceLanguage", slot.get("lang")),
                         ("regionCode", slot.get("region")), ("pageToken", page_token)):
            if val:
                params[key] = val
        data = self._get("search", params)
        ids = []
        for item in data.get("items", []):
            cid = item.get("snippet", {}).get("channelId")
            if cid and cid not in ids:
                ids.append(cid)
        return ids, data.get("nextPageToken")

    def channels(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", {
                "part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50]), "maxResults": 50,
                "fields": "items(id,snippet(title,description,customUrl,country,defaultLanguage),"
                          "statistics(subscriberCount,hiddenSubscriberCount,videoCount),"
                          "contentDetails(relatedPlaylists(uploads)))",
            })
            out.extend(data.get("items", []))
        return out

    def recent_video_ids(self, uploads_playlist, count):
        if not uploads_playlist:
            return []
        data = self._get("playlistItems", {
            "part": "contentDetails", "playlistId": uploads_playlist, "maxResults": count,
            "fields": "items(contentDetails(videoId,videoPublishedAt))",
        })
        return [i["contentDetails"]["videoId"] for i in data.get("items", []) if i.get("contentDetails", {}).get("videoId")]

    def videos(self, ids):
        out = {}
        for i in range(0, len(ids), 50):
            data = self._get("videos", {
                "part": "snippet,contentDetails", "id": ",".join(ids[i:i + 50]), "maxResults": 50,
                "fields": "items(id,snippet(publishedAt,description,defaultAudioLanguage,defaultLanguage),"
                          "contentDetails(duration))",
            })
            for item in data.get("items", []):
                out[item["id"]] = item
        return out


# --------------------------------------------------------------------------- email extraction
def is_junk_email(email):
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1]
    return bool(
        not local or not domain or len(email) > 80 or tld in FILE_EXT_TLDS or local in JUNK_LOCAL
        or any(m in email for m in JUNK_MARKERS)
        or any(domain == j or domain.endswith("." + j) for j in JUNK_DOMAINS)
        or re.search(r"\d+x\d*$", local) or local.startswith("u00")
    )


def clean_email(raw):
    email = unquote(html.unescape(str(raw))).strip().strip(".,;:!?)(<>[]{}'\"").lower()
    return email if EMAIL_RE.fullmatch(email) and not is_junk_email(email) else ""


def deobfuscate(text):
    """'name [at] gmail [dot] com' -> 'name@gmail.com' (bracketed forms only)."""
    out = re.sub(r"\s*[\[({]\s*at\s*[\])}]\s*", "@", text, flags=re.I)
    return re.sub(r"\s*[\[({]\s*dot\s*[\])}]\s*", ".", out, flags=re.I)


def emails_in_text(text):
    """[(email, line_index)] in order of appearance, from plain text (a description)."""
    found, seen = [], set()
    lines = str(text or "").splitlines()
    for idx, line in enumerate(lines):
        for candidate in EMAIL_RE.findall(line) + EMAIL_RE.findall(deobfuscate(line)):
            email = clean_email(candidate)
            if email and email not in seen:
                seen.add(email)
                found.append((email, idx))
    return found, lines


def near_words(lines, idx, words):
    window = " ".join(lines[max(0, idx - 1): idx + 1]).lower()
    return any(w in window for w in words)


def name_match(piece, keys, min_len=5):
    """True if `piece` (a domain label, email local part or link slug) is clearly the channel's own name:
    it contains a full channel key, or is a big part (>=60%) of one. Stops 'reviews.com' matching 'Game Reviews Daily'."""
    piece = norm(piece)
    if len(piece) < min_len:
        return False
    return any((k in piece) or (piece in k and len(piece) >= 0.6 * len(k)) for k in keys if len(k) >= min_len)


def email_matches_channel(email, keys):
    local, _, domain = email.partition("@")
    label = registrable_domain(domain).split(".")[0]
    return name_match(local, keys) or name_match(label, keys)


def description_emails(channel_desc, video_descs, keys, freemail):
    """Candidate (email, source, note) from the About text and recent video descriptions, best first."""
    out = []
    emails, lines = emails_in_text(channel_desc)
    labelled = [(e, i) for e, i in emails if near_words(lines, i, CONTACT_WORDS)]
    for email, idx in labelled + [x for x in emails if x not in labelled]:
        tag = "labelled" if (email, idx) in labelled else "unlabelled"
        out.append((email, "channel description", f"{tag} email in the channel description"))

    per_video = []
    counts = Counter()
    for desc in video_descs:
        emails, lines = emails_in_text(desc)
        per_video.append((emails, lines))
        counts.update({e for e, _ in emails})
    taken = {e for e, _, _ in out}
    for emails, lines in per_video:
        for email, idx in emails:
            if email in taken:
                continue
            sponsor_line = near_words(lines, idx, SPONSOR_WORDS) and not email_matches_channel(email, keys)
            if sponsor_line:
                continue
            if near_words(lines, idx, CONTACT_WORDS):
                note = "labelled email in a recent video description"
            elif counts[email] >= 2:
                note = f"email repeated in {counts[email]} recent video descriptions"
            elif email_matches_channel(email, keys):
                note = "email matching the channel name in a video description"
            else:
                continue
            taken.add(email)
            out.append((email, "video description", note))
    return out


def urls_in_text(text):
    urls = []
    for raw in URL_RE.findall(str(text or "")):
        url = raw.rstrip(".,;:!?)")
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in urls:
            urls.append(url)
    return urls


def classify_link(url, keys):
    """'hub' (link-in-bio page), 'site' (personal site matching the channel name) or '' (ignore)."""
    host = host_of(url)
    if not host or host_matches(host, SKIP_HOSTS):
        return ""
    if host_matches(host, LINK_HUB_HOSTS):
        return "hub"
    label = registrable_domain(host).split(".")[0]
    return "site" if len(label) >= 6 and name_match(label, keys) else ""


def hub_slug_matches(url, keys):
    host = host_of(url)
    parsed = urlparse(url)
    slug = norm(parsed.path.strip("/").split("/")[0]) if parsed.path.strip("/") else ""
    if not slug and host.count(".") >= 2:  # name.carrd.co style
        slug = norm(host.split(".")[0])
    return name_match(slug, keys, min_len=4)


def linked_pages(channel_desc, video_descs, keys):
    """Links worth fetching: hubs + name-matching sites from the About text, name-matching hubs from videos."""
    links = []
    about = urls_in_text(channel_desc)
    hubs = [u for u in about if classify_link(u, keys) == "hub"]
    for url in about:
        kind = classify_link(url, keys)
        if kind == "site" or (kind == "hub" and (len(hubs) == 1 or hub_slug_matches(url, keys))):
            links.append((url, kind))
    for desc in video_descs:
        for url in urls_in_text(desc):
            if classify_link(url, keys) == "hub" and hub_slug_matches(url, keys) and url not in [u for u, _ in links]:
                links.append((url, "hub"))
    seen_hosts, unique = set(), []
    for url, kind in links:
        key = (host_of(url), urlparse(url).path.rstrip("/").lower())
        if key not in seen_hosts:
            seen_hosts.add(key)
            unique.append((url, kind))
    return unique


def decode_cf_email(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def page_emails(page_html):
    """Emails a visitor can see on a page: visible text, mailto: links (also inside page data), Cloudflare-protected."""
    found = []

    def add(raw):
        email = clean_email(raw)
        if email and email not in found:
            found.append(email)

    for raw in MAILTO_RE.findall(page_html):
        add(raw)
    body = CODE_RE.sub(" ", page_html)
    text = html.unescape(TAG_RE.sub(" ", body))
    for raw in EMAIL_RE.findall(text) + EMAIL_RE.findall(deobfuscate(text)):
        add(raw)
    for hexstr in CF_EMAIL_RE.findall(page_html):
        add(decode_cf_email(hexstr))
    return found


class SiteFetcher:
    """Polite fetcher for linked sites: robots.txt respected, small pages only, capped per run."""

    def __init__(self, session, max_fetches):
        self.session, self.max_fetches, self.fetches = session, max_fetches, 0
        self._robots = {}

    def allowed(self, url):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.session.get(base + "/robots.txt", headers=HTML_HEADERS, timeout=SITE_TIMEOUT)
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(resp.text.splitlines())
            except requests.RequestException:
                rp.disallow_all = True  # can't read robots.txt -> don't crawl
            self._robots[base] = rp
        return self._robots[base].can_fetch(UA, url)

    def get(self, url):
        if self.fetches >= self.max_fetches:
            return None, "fetch cap reached"
        if not self.allowed(url):
            return None, "robots.txt disallows"
        self.fetches += 1
        resp = None
        try:
            resp = self.session.get(url, headers=HTML_HEADERS, timeout=SITE_TIMEOUT, stream=True)
            if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "html").lower():
                return None, f"HTTP {resp.status_code}"
            final_host = host_of(getattr(resp, "url", url) or url)
            if final_host and host_matches(final_host, SKIP_HOSTS):
                return None, "redirected to a platform"
            raw = resp.raw.read(MAX_PAGE_BYTES, decode_content=True)
            return raw.decode(resp.encoding or "utf-8", errors="replace"), ""
        except (requests.RequestException, OSError) as exc:
            return None, type(exc).__name__
        finally:
            if resp is not None and hasattr(resp, "close"):
                resp.close()


def contact_links(page_html, base_url):
    base_domain = registrable_domain(host_of(base_url))
    out = []
    for href, label in HREF_RE.findall(page_html):
        url = urljoin(base_url, href.strip())
        text = (href + " " + TAG_RE.sub(" ", label)).lower()
        if (url.startswith("http") and registrable_domain(host_of(url)) == base_domain
                and re.search(r"contact|about|business|collab|work-with|press|sponsor", text) and url not in out):
            out.append(url)
    return out


def site_emails(fetcher, links, keys, freemail, max_pages=3):
    """(email, note) from linked hubs/sites. Emails must be freemail, on the site's own domain or name-matching."""
    notes, pages = [], 0
    for url, kind in links:
        if pages >= max_pages:
            break
        page, why = fetcher.get(url)
        pages += 1
        if page is None:
            notes.append(f"{host_of(url)} skipped ({why})")
            continue
        candidates = page_emails(page)
        if not candidates and kind == "site":
            for sub in contact_links(page, url)[:1]:
                if pages >= max_pages:
                    break
                sub_page, _ = fetcher.get(sub)
                pages += 1
                if sub_page:
                    candidates = page_emails(sub_page)
        site_domain = registrable_domain(host_of(url))
        for email in candidates:
            dom = registrable_domain(domain_of(email))
            if kind == "hub" or dom == site_domain or dom in freemail or email_matches_channel(email, keys):
                return email, f"email on linked {'link-in-bio page' if kind == 'hub' else 'site'} {host_of(url)}", notes
        notes.append(f"no usable email on {host_of(url)}")
    return None, "", notes


# --------------------------------------------------------------------------- MX check
_mx_cache = {}


def mx_status(domain, freemail=()):
    domain = domain.lower().strip(".")
    if domain in freemail:
        return "ok"
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        import dns.exception
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime, resolver.timeout = 6, 3
        try:
            answers = resolver.resolve(domain, "MX")
            status = "ok" if any(r.exchange.to_text() != "." for r in answers) else "none"
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            status = "none"
        except (dns.exception.DNSException, OSError):
            status = "error"
    except ImportError:
        status = "error"
    _mx_cache[domain] = status
    return status


# --------------------------------------------------------------------------- channel evaluation
class Candidate:
    def __init__(self, item, slot):
        sn, st = item.get("snippet", {}), item.get("statistics", {})
        self.id = item["id"]
        self.title = sn.get("title", "").strip()
        self.handle = sn.get("customUrl", "")
        self.description = sn.get("description", "")
        self.country = (sn.get("country") or "").upper()
        self.language = (sn.get("defaultLanguage") or "").lower()
        self.hidden_subs = bool(st.get("hiddenSubscriberCount"))
        self.subs = int(st.get("subscriberCount") or 0)
        self.uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        self.slot = slot or {}
        self.niche = self.slot.get("niche", "")
        self.video_ids, self.videos = [], []
        self.email, self.email_source, self.status, self.last_upload = "", "", "", ""
        self.notes = []

    @property
    def keys(self):
        return name_keys(self.title, self.handle)


def band_reason(c, cfg):
    if c.hidden_subs:
        return "hidden_subs"
    if not cfg["subscriber_min"] <= c.subs <= cfg["subscriber_max"]:
        return "out_of_band"
    return ""


def qualify(c, cfg, today=None):
    """Activity + long-form check on the recent uploads. Returns '' if it fits, else a reason."""
    q = cfg["qualify"]
    today = today or utc_now()
    published = [parse_iso(v.get("snippet", {}).get("publishedAt")) for v in c.videos]
    published = [p for p in published if p]
    if not published:
        return "no_uploads"
    latest = max(published)
    c.last_upload = latest.date().isoformat()
    if (today - latest).days > q["active_within_days"]:
        return "inactive"
    long_form = sum(1 for v in c.videos
                    if parse_duration(v.get("contentDetails", {}).get("duration")) > q["long_form_seconds"])
    if long_form < q["min_long_form_recent"]:
        return "shorts_only"
    c.notes.append(f"{long_form}/{len(c.videos)} recent uploads long-form")
    return ""


def is_non_english(c):
    if c.language and not c.language.startswith("en"):
        return True
    audio = [(v.get("snippet", {}).get("defaultAudioLanguage") or "").lower() for v in c.videos]
    audio = [a for a in audio if a]
    return bool(audio) and sum(1 for a in audio if not a.startswith("en")) > len(audio) / 2


def decide_status(c, cfg):
    if not c.email:
        return STATUS_NO_CONTACT
    if c.country == "GB" and cfg.get("uk_policy", "review") == "review":
        return STATUS_UK
    if is_non_english(c) and cfg.get("non_english_policy", "review") == "review":
        return STATUS_NON_EN
    return STATUS_PENDING


def choose_email(c, cfg, fetcher, rules):
    """Pick the first candidate that passes suppression + MX. Sets c.email / c.email_source."""
    freemail = set(cfg["freemail"])
    descs = [v.get("snippet", {}).get("description", "") for v in c.videos]
    candidates = description_emails(c.description, descs, c.keys, freemail)
    for email, source, note in candidates:
        verdict = rules.check(email, freemail)
        if verdict == "ok":
            c.email, c.email_source = email, source
            c.notes.append(note + ", MX ok")
            return
        c.notes.append(f"{source} email rejected ({verdict})")
    if fetcher is None:
        return
    links = linked_pages(c.description, descs, c.keys)
    if not links:
        return
    email, note, fetch_notes = site_emails(fetcher, links, c.keys, freemail)
    c.notes.extend(fetch_notes)
    if email:
        verdict = rules.check(email, freemail)
        if verdict == "ok":
            c.email, c.email_source = email, "linked site"
            c.notes.append(note + ", MX ok")
        else:
            c.notes.append(f"linked-site email rejected ({verdict})")


class EmailRules:
    def __init__(self, known_emails=(), suppressed_emails=(), suppressed_domains=(), check_mx=True):
        self.known = set(known_emails)
        self.suppressed = set(suppressed_emails)
        self.suppressed_domains = set(suppressed_domains)
        self.check_mx = check_mx

    def check(self, email, freemail):
        if email in self.known:
            return "already in the sheet"
        if email in self.suppressed or registrable_domain(domain_of(email)) in self.suppressed_domains:
            return "suppressed (bounced / opted out)"
        if self.check_mx:
            mx = mx_status(domain_of(email), freemail)
            if mx != "ok":
                return "no MX record" if mx == "none" else "MX lookup failed"
        return "ok"


# --------------------------------------------------------------------------- rotation + state
def build_slots(cfg):
    """Every (niche, term, duration, order) search, interleaved so smaller niches aren't starved."""
    s = cfg["search"]
    niches = cfg["niches"]
    longest = max((len(n["terms"]) for n in niches), default=0)
    slots = []
    for duration in s["durations"]:
        for order in s["orders"]:
            for i in range(longest):
                for n in niches:
                    if i < len(n["terms"]):
                        slots.append({
                            "niche": n["label"], "term": n["terms"][i], "category_id": n.get("category_id", ""),
                            "duration": duration, "order": order, "lang": s.get("relevance_language", ""),
                            "region": s.get("region_code", ""),
                        })
    return slots


def slot_key(slot):
    return f"{slot['niche']}|{slot['term']}|{slot['duration']}|{slot['order']}"


def new_window(cfg, now=None):
    now = now or utc_now()
    return {"published_after": iso_z(now - timedelta(days=cfg["search"]["window_days"])),
            "published_before": iso_z(now), "started": now.date().isoformat()}


def load_json(name, default):
    path = os.path.join(HERE, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def load_config():
    return load_json("config.json", {})


def load_state():
    state = load_json("state.json", {})
    state.setdefault("cursor", 0)
    state.setdefault("slot_pages", {})
    state.setdefault("seen", {})
    state.setdefault("cycle", 0)
    return state


def save_state(state):
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")


def prune_seen(state, ttl_days, today=None):
    today = today or date.today()
    keep = {}
    for cid, (day, reason) in state.get("seen", {}).items():
        d = parse_sheet_date(day)
        if d and (today - d).days < ttl_days:
            keep[cid] = [day, reason]
    dropped = len(state.get("seen", {})) - len(keep)
    state["seen"] = keep
    return dropped


def mark_seen(state, cid, reason, today=None):
    state["seen"][cid] = [(today or date.today()).isoformat(), reason]


def advance(state, slots):
    state["cursor"] = state.get("cursor", 0) + 1
    if state["cursor"] >= len(slots):
        state["cursor"] = 0
        state["window"] = None  # next run starts a new cycle with a fresh publish window
        state["slot_pages"] = {}


def run_searches(yt, state, cfg, slots, searches, known_ids):
    """Runs up to `searches` search.list calls following the rotation. Returns [(channel_id, slot)] (new only)."""
    if not state.get("window"):
        state["window"] = new_window(cfg)
        state["cycle"] = state.get("cycle", 0) + 1
        state["slot_pages"] = {}
        log(f"new search cycle #{state['cycle']}: uploads {state['window']['published_after'][:10]} to "
            f"{state['window']['published_before'][:10]}")
    found, found_ids = [], set()
    pages_per_slot = cfg["search"]["pages_per_slot"]
    for _ in range(searches):
        if not slots:
            break
        slot = slots[state["cursor"] % len(slots)]
        key = slot_key(slot)
        progress = state["slot_pages"].get(key, {"pages": 0, "token": None})
        try:
            ids, token = yt.search(slot, state["window"], progress.get("token"))
        except BadPageToken:
            log(f"  search '{slot['term']}' ({slot['duration']}, {slot['order']}): stale page token, moving on")
            advance(state, slots)
            continue
        new = [cid for cid in ids if cid not in known_ids and cid not in state["seen"] and cid not in found_ids]
        for cid in new:
            found_ids.add(cid)
            found.append((cid, slot))
        pages = progress["pages"] + 1
        log(f"  search '{slot['term']}' ({slot['duration']}, {slot['order']}) page {pages}: "
            f"{len(ids)} channels, {len(new)} new")
        if token and pages < pages_per_slot and new:
            state["slot_pages"][key] = {"pages": pages, "token": token}
        else:
            state["slot_pages"].pop(key, None)
            advance(state, slots)
    return found


# --------------------------------------------------------------------------- Google Sheet
def find_header(values):
    for idx, row in enumerate(values[:10]):
        if any(str(c).strip().lower() in HEADER_ALIASES["name"] for c in row):
            return idx
    return None


def col_index(header, key):
    aliases = HEADER_ALIASES[key]
    for i, cell in enumerate(header):
        if str(cell).strip().lower() in aliases:
            return i
    return None


def open_sheet():
    import gspread
    raw = clean_secret(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    sheet_id = clean_secret(os.environ.get("SHEET_ID"))
    if not raw or not sheet_id:
        return None
    gc = gspread.service_account_from_dict(json.loads(raw))
    return gc.open_by_key(sheet_id)


def prepare_queue_tab(ws, write=True):
    """(header_row_index, header, values). Writes a header to an empty tab / adds missing columns when write=True."""
    values = ws.get_all_values()
    h = find_header(values)
    if h is None:
        if write:
            ws.update(range_name="A1", values=[DEFAULT_HEADER])
        return 0, list(DEFAULT_HEADER), [DEFAULT_HEADER]
    header = list(values[h])
    for key in QUEUE_KEYS:
        if col_index(header, key) is None:
            header.append(CANONICAL[key])
            if write:
                ws.update_cell(h + 1, len(header), CANONICAL[key])
                log(f"  added column '{CANONICAL[key]}' to the queue tab")
    return h, header, values


def load_known(sh, tabs):
    """Channel IDs and emails already in the queue / tracker (never re-added)."""
    ids, emails = set(), set()
    for tab in tabs:
        try:
            values = sh.worksheet(tab).get_all_values()
        except Exception as exc:  # noqa: BLE001 - a missing tab shouldn't stop the run
            log(f"  WARNING: couldn't read '{tab}' ({type(exc).__name__})")
            continue
        for row in values:
            for cell in row:
                ids.update(CHANNEL_ID_RE.findall(cell))
                emails.update(e.lower() for e in EMAIL_RE.findall(cell))
        log(f"  known from '{tab}': {max(len(values) - 1, 0)} rows")
    return ids, emails


def _to_int(value):
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits) if digits else None


def read_lists_settings(values):
    """Settings Ethan edits on the Lists tab. `values` is the tab's get_all_values().
    Returns {"settings": {lowercased name: value}, "ramp": [(from_day, cap)], "niches": [labels]}."""
    out = {"settings": {}, "ramp": [], "niches": []}

    def find(label):
        for r, row in enumerate(values):
            for c, cell in enumerate(row):
                if str(cell).strip().lower() == label:
                    return r, c
        return None

    def cell(r, c):
        return str(values[r][c]).strip() if r < len(values) and c < len(values[r]) else ""

    hit = find("setting")
    if hit:
        r, c = hit
        for rr in range(r + 1, len(values)):
            name = cell(rr, c)
            if not name:
                break
            out["settings"][name.lower()] = cell(rr, c + 1)
    hit = find("ramp: from day")
    if hit:
        r, c = hit
        for rr in range(r + 1, len(values)):
            day, cap = _to_int(cell(rr, c)), _to_int(cell(rr, c + 1))
            if day is None or cap is None:
                break
            out["ramp"].append((day, cap))
    hit = find("niche / category")
    if hit:
        r, c = hit
        for rr in range(r + 1, len(values)):
            name = cell(rr, c)
            if not name:
                break
            out["niches"].append(name)
    return out


def load_lists_settings(sh, tab="Lists"):
    try:
        return read_lists_settings(sh.worksheet(tab).get_all_values())
    except Exception as exc:  # noqa: BLE001 - fall back to config.json
        log(f"  WARNING: couldn't read the '{tab}' tab ({type(exc).__name__}); using config.json defaults")
        return {"settings": {}, "ramp": [], "niches": []}


def apply_lists_settings(cfg, lists):
    """Subscriber range and niches from the Lists tab override config.json. A niche listed on the sheet that has no
    search terms in config.json is searched by its own name (no category filter)."""
    st = lists.get("settings", {})
    lo, hi = _to_int(st.get("subscriber min")), _to_int(st.get("subscriber max"))
    if lo is not None and hi is not None and lo < hi:
        cfg["subscriber_min"], cfg["subscriber_max"] = lo, hi
    wanted = [n for n in lists.get("niches", []) if n.strip()]
    if wanted:
        known = {n["label"].strip().lower(): n for n in cfg["niches"]}
        cfg["niches"] = [known.get(w.strip().lower(), {"label": w.strip(), "category_id": "", "terms": [w.strip()]})
                         for w in wanted]
    return cfg


def load_suppression(sh, tab, freemail):
    """Every address in the Replies tab (replies, bounces, opt-outs); domains too for opt-outs (not freemail)."""
    emails, domains = set(), set()
    try:
        values = sh.worksheet(tab).get_all_values()
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: couldn't read '{tab}' ({type(exc).__name__})")
        return emails, domains
    for row in values[1:]:
        row_emails = {e.lower() for cell in row for e in EMAIL_RE.findall(cell)}
        emails |= row_emails
        if any(str(c).strip().lower() == "opt-out" for c in row):
            domains |= {registrable_domain(domain_of(e)) for e in row_emails
                        if domain_of(e) not in freemail and not domain_of(e).startswith("googlemail")}
    return emails, domains


def append_rows(ws, rows):
    """Append without inserting rows (OVERWRITE into the empty rows below the table), so the pre-formatted rows,
    dropdowns and the other tabs' whole-column formulas are left exactly as they are. Grows the grid if needed."""
    used = len(ws.get_all_values())
    spare = ws.row_count - used
    if spare < len(rows) + 5:
        ws.add_rows(len(rows) + 200 - max(spare, 0))
    ws.append_rows(rows, value_input_option="USER_ENTERED", insert_data_option="OVERWRITE", table_range="A1")


def row_for(c, header, today=None):
    today = (today or date.today()).isoformat()
    why = "; ".join([f"found via '{c.slot.get('term', '')}' ({c.slot.get('duration', '')}, "
                     f"{c.slot.get('order', '')})"] + c.notes)
    values = {
        "name": sheet_text(c.title), "niche": sheet_text(c.niche), "subs": c.subs, "url": channel_url(c.id),
        "email": sheet_text(c.email), "email_source": sheet_text(c.email_source), "status": c.status,
        "source": SOURCE_LABEL, "date": today, "country": sheet_text(c.country), "last_upload": c.last_upload,
        "last_checked": today, "why": sheet_text(why[:450]),
    }
    row = [""] * len(header)
    for key, val in values.items():
        i = col_index(header, key)
        if i is not None:
            row[i] = val
    return row


# --------------------------------------------------------------------------- 30-day refresh of queue rows
def stale_rows(values, h, header, refresh_days, today=None):
    """[(sheet_row_number, channel_id, has_email, status)] for queue rows not checked through the API recently."""
    today = today or date.today()
    url_i, email_i = col_index(header, "url"), col_index(header, "email")
    checked_i, date_i = col_index(header, "last_checked"), col_index(header, "date")
    status_i = col_index(header, "status")
    out = []
    for offset, row in enumerate(values[h + 1:]):
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
        ids = CHANNEL_ID_RE.findall(cell(url_i))
        if not ids:
            continue
        checked = parse_sheet_date(cell(checked_i)) or parse_sheet_date(cell(date_i))
        if checked is None or (today - checked).days >= refresh_days:
            out.append((h + 2 + offset, ids[0], bool(cell(email_i)), cell(status_i)))
    return out


def is_open_status(status):
    """Rows still waiting on something (Pending / Review ...). Emailed / Skipped rows are finished: their permanent
    record is the Outreach Tracker, so once stale they're simply removed from the queue."""
    s = (status or "").strip().lower()
    return s in ("", "pending") or s.startswith("review")


def refresh_queue(yt, sh, ws, cfg, fetcher, rules, max_rows, dry_run):
    """Re-check queue rows older than refresh_after_days through the API: update them, or delete if they no longer
    qualify (or never had an email). Keeps stored YouTube API data inside the 30-day limit."""
    h, header, values = prepare_queue_tab(ws, write=not dry_run)
    stale = stale_rows(values, h, header, cfg["refresh_after_days"])
    if not stale:
        return 0, 0
    delete_ids = {cid for _, cid, _, status in stale if not is_open_status(status)}
    stale = [s for s in stale if is_open_status(s[3])][:max_rows]
    delete_ids |= {cid for _, cid, has_email, _ in stale if not has_email}
    check = [cid for _, cid, has_email, _ in stale if has_email]
    updates = {}
    if check:
        items = {i["id"]: i for i in yt.channels(check)}
        for cid in check:
            item = items.get(cid)
            if not item:
                delete_ids.add(cid)
                continue
            c = Candidate(item, {})
            if band_reason(c, cfg):
                delete_ids.add(cid)
                continue
            c.video_ids = yt.recent_video_ids(c.uploads, cfg["qualify"]["recent_videos"])
            vids = yt.videos(c.video_ids) if c.video_ids else {}
            c.videos = [vids[v] for v in c.video_ids if v in vids]
            if qualify(c, cfg):
                delete_ids.add(cid)
                continue
            refresh_rules = EmailRules((), rules.suppressed, rules.suppressed_domains, rules.check_mx)
            choose_email(c, cfg, fetcher, refresh_rules)
            if not c.email:
                delete_ids.add(cid)
                continue
            updates[cid] = c
    if dry_run:
        log(f"  refresh (dry run): {len(updates)} rows would be refreshed, {len(delete_ids)} deleted")
        return len(updates), len(delete_ids)
    # Re-read right before writing so row numbers are current (the emailer deletes rows too).
    values = ws.get_all_values()
    url_i = col_index(header, "url")
    row_of = {}
    for offset, row in enumerate(values[h + 1:]):
        ids = CHANNEL_ID_RE.findall(row[url_i]) if url_i is not None and url_i < len(row) else []
        if ids:
            row_of.setdefault(ids[0], h + 2 + offset)
    today = date.today().isoformat()
    data = []
    for cid, c in updates.items():
        r = row_of.get(cid)
        if not r:
            continue
        for key, val in (("subs", c.subs), ("email", sheet_text(c.email)), ("email_source", sheet_text(c.email_source)),
                         ("last_upload", c.last_upload), ("last_checked", today)):
            i = col_index(header, key)
            if i is not None:
                data.append({"range": f"{_col_letter(i + 1)}{r}", "values": [[val]]})
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    rows = sorted({row_of[cid] for cid in delete_ids if cid in row_of}, reverse=True)
    if rows:
        requests_ = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                                    "startIndex": r - 1, "endIndex": r}}} for r in rows]
        sh.batch_update({"requests": requests_})
    log(f"  refreshed {len(updates)} queue rows, deleted {len(rows)} that no longer qualify / had no email")
    return len(updates), len(rows)


def _col_letter(n):
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


# --------------------------------------------------------------------------- orchestration
def evaluate(yt, found, cfg, state, fetcher, rules, max_new, deadline, stats):
    """channels.list -> band filter -> recent uploads -> qualify -> email. Returns accepted Candidates."""
    slot_of = dict(found)
    items = yt.channels([cid for cid, _ in found])
    returned = {i["id"] for i in items}
    for cid, _ in found:
        if cid not in returned:
            mark_seen(state, cid, "not_returned")
    in_band = []
    for item in items:
        c = Candidate(item, slot_of.get(item["id"]))
        reason = band_reason(c, cfg)
        if reason:
            stats[reason] += 1
            mark_seen(state, c.id, reason)
        else:
            in_band.append(c)
    stats["in subscriber band"] += len(in_band)
    in_band = in_band[:max_new]
    for c in in_band:
        c.video_ids = yt.recent_video_ids(c.uploads, cfg["qualify"]["recent_videos"])
    all_vids = [v for c in in_band for v in c.video_ids]
    vids = yt.videos(all_vids) if all_vids else {}
    qualified = []
    for c in in_band:
        c.videos = [vids[v] for v in c.video_ids if v in vids]
        reason = qualify(c, cfg)
        if reason:
            stats[reason] += 1
            mark_seen(state, c.id, reason)
        else:
            qualified.append(c)

    def work(c):
        if time.monotonic() > deadline:
            return c, "time"
        choose_email(c, cfg, fetcher, rules)
        return c, ""

    accepted = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for c, skipped in pool.map(work, qualified):
            if skipped:
                stats["not checked (time)"] += 1
                continue
            if c.email:
                if c.email in rules.known:
                    stats["email already claimed this run"] += 1
                    continue
                rules.known.add(c.email)
            elif not cfg.get("record_no_contact", True):
                stats["no plain-text email"] += 1
                mark_seen(state, c.id, "no_contact")
                continue
            c.status = decide_status(c, cfg)
            stats[f"added: {c.status}"] += 1
            mark_seen(state, c.id, "added")
            accepted.append(c)
    return accepted


def run(dry_run, searches, max_new):
    started = time.monotonic()
    deadline = started + env_int("RUN_BUDGET_SECONDS", 300)
    cfg, state = load_config(), load_state()
    key = clean_secret(os.environ.get("YOUTUBE_API_KEY"))
    missing = [n for n in ("YOUTUBE_API_KEY", "SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")
               if not clean_secret(os.environ.get(n))]
    if not key or (missing and not dry_run):
        # Not configured yet: skip quietly (a warning, not a failed run + failure email every 2 hours).
        log(f"::warning::Setup not finished - missing repo secret(s): {', '.join(missing)}. Nothing was done. "
            "Add them under Settings > Secrets and variables > Actions.")
        return 0
    session = requests.Session()
    quota = Quota(state, env_int("DAILY_QUOTA_BUDGET", 9000), env_int("RUN_QUOTA_BUDGET", 700))
    yt = YouTube(key, session, quota)
    fetcher = SiteFetcher(session, env_int("MAX_SITE_FETCHES", 60))
    freemail = set(cfg["freemail"])
    dropped = prune_seen(state, cfg["seen_ttl_days"])
    log(f"quota used today (Pacific): {quota.q['used']}/{quota.daily}; seen channels: {len(state['seen'])} "
        f"(expired {dropped})")

    sh = ws = None
    known_ids, known_emails, sup_emails, sup_domains = set(), set(), set(), set()
    try:
        sh = open_sheet()
    except Exception as exc:  # noqa: BLE001
        log(f"could not open the Sheet ({type(exc).__name__}: {str(exc)[:200]})")
        if not dry_run:
            return 1
    if sh is not None:
        import gspread
        try:
            ws = sh.worksheet(cfg["queue_tab"])
        except gspread.WorksheetNotFound:
            ws = None if dry_run else sh.add_worksheet(cfg["queue_tab"], rows=1000, cols=len(DEFAULT_HEADER))
        apply_lists_settings(cfg, load_lists_settings(sh, cfg.get("lists_tab", "Lists")))
        log(f"  targeting {cfg['subscriber_min']:,}-{cfg['subscriber_max']:,} subs; niches: "
            f"{', '.join(n['label'] for n in cfg['niches'])}")
        known_ids, known_emails = load_known(sh, [cfg["queue_tab"], cfg["tracker_tab"]])
        sup_emails, sup_domains = load_suppression(sh, cfg["replies_tab"], freemail)
        log(f"  suppression: {len(sup_emails)} addresses, {len(sup_domains)} domains")
    elif not dry_run:
        log("SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON not set - add them as repo secrets.")
        return 1
    else:
        log("dry run without Sheet access: no de-duplication against the Sheet")

    rules = EmailRules(known_emails, sup_emails, sup_domains, check_mx=env_flag("CHECK_MX", True))
    stats = Counter()
    accepted = []
    try:
        if ws is not None:
            refresh_queue(yt, sh, ws, cfg, fetcher, rules, env_int("REFRESH_PER_RUN", 40), dry_run)
        slots = build_slots(cfg)
        found = run_searches(yt, state, cfg, slots, searches, known_ids)
        stats["new channels from search"] = len(found)
        if found:
            accepted = evaluate(yt, found, cfg, state, fetcher, rules, max_new, deadline, stats)
    except QuotaExhausted as exc:
        log(f"stopping early: {exc}")
    except ApiError as exc:
        log(f"stopping early: YouTube API error ({exc})")

    header = DEFAULT_HEADER
    if ws is not None and accepted:
        _, header, _ = prepare_queue_tab(ws, write=not dry_run)
    rows = [row_for(c, header) for c in accepted]

    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    out_path = os.path.join(HERE, "output", f"leads_{date.today().isoformat()}.csv")
    with open(out_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(DEFAULT_HEADER)
        for c in accepted:
            writer.writerow([v[1:] if isinstance(v, str) and v.startswith("'") else v for v in row_for(c, DEFAULT_HEADER)])

    log("filter results:")
    for k in sorted(stats):
        log(f"  {k}: {stats[k]}")
    for c in accepted:  # emails stay out of the log
        log(f"  + {c.title} ({c.subs:,} subs) -> {c.status}" + (f" via {c.email_source}" if c.email else ""))
    pending = sum(1 for c in accepted if c.status == STATUS_PENDING)
    log(f"new rows: {len(rows)} (Pending: {pending}); quota used this run: {quota.run_used}, "
        f"today: {quota.q['used']}; sites fetched: {fetcher.fetches}")

    if rows and ws is not None and not dry_run:
        append_rows(ws, rows)
        log(f"appended {len(rows)} rows to '{cfg['queue_tab']}'")
    if dry_run:
        # Rotation/seen stay untouched so a dry run changes nothing, but quota really was spent.
        saved = load_state()
        saved["quota"] = state["quota"]
        save_state(saved)
    else:
        save_state(state)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no Sheet writes, rotation unchanged")
    ap.add_argument("--searches", type=int, default=env_int("SEARCHES_PER_RUN", 3))
    ap.add_argument("--max-new", type=int, default=env_int("MAX_NEW_PER_RUN", 40))
    args = ap.parse_args()
    sys.exit(run(args.dry_run or env_flag("DRY_RUN", False), args.searches, args.max_new))


if __name__ == "__main__":
    main()
