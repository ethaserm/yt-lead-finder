#!/usr/bin/env python3
"""Search tuning: runs the same few search terms through several search settings and reports, for each one, how
many channels land in the subscriber band, qualify, and show a public email.

Nothing is written to the Sheet or to state.json. Uses roughly 100 quota units per term per variant
(5 variants x 3 terms ~= 1,600 units with the follow-up channel/video checks). Logs never print email addresses.
"""
import os
import sys
import time
from collections import Counter

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import leadfinder as lf  # noqa: E402

VARIANTS = [
    ("medium + date (current first slots)", {"duration": "medium", "order": "date"}),
    ("medium + relevance", {"duration": "medium", "order": "relevance"}),
    ("long + relevance", {"duration": "long", "order": "relevance"}),
    ("medium + viewCount", {"duration": "medium", "order": "viewCount"}),
    ("medium + relevance + region US", {"duration": "medium", "order": "relevance", "region": "US"}),
]


def main():
    key = lf.clean_secret(os.environ.get("YOUTUBE_API_KEY"))
    if not key:
        lf.log("::warning::YOUTUBE_API_KEY not set - nothing to compare.")
        return 0
    terms = [t.strip() for t in os.environ.get("TERMS", "minecraft,fortnite,horror games").split(",") if t.strip()]
    cfg = lf.load_config()
    session = requests.Session()
    quota = lf.Quota({}, lf.env_int("DAILY_QUOTA_BUDGET", 9000), lf.env_int("RUN_QUOTA_BUDGET", 3000))
    yt = lf.YouTube(key, session, quota)
    fetcher = lf.SiteFetcher(session, 200)
    window = lf.new_window(cfg)
    base = {"niche": "Gaming", "category_id": "20", "lang": cfg["search"].get("relevance_language", ""), "region": ""}
    lf.log(f"terms: {', '.join(terms)} (page 1 of each); uploads since {window['published_after'][:10]}; "
           f"band {cfg['subscriber_min']:,}-{cfg['subscriber_max']:,}")

    rows = []
    for name, extra in VARIANTS:
        found, seen = [], set()
        try:
            for term in terms:
                slot = {**base, "term": term, **extra}
                ids, _ = yt.search(slot, window)
                for cid in ids:
                    if cid not in seen:
                        seen.add(cid)
                        found.append((cid, slot))
            stats = Counter()
            rules = lf.EmailRules(check_mx=True)
            accepted = lf.evaluate(yt, found, cfg, {"seen": {}}, fetcher, rules, 200, time.monotonic() + 150, stats)
        except (lf.QuotaExhausted, lf.ApiError) as exc:
            lf.log(f"{name}: stopped ({exc})")
            break
        in_band = stats["in subscriber band"]
        qualified = in_band - stats["inactive"] - stats["shorts_only"]
        pending = stats[f"added: {lf.STATUS_PENDING}"]
        rows.append((name, len(found), in_band, qualified, len(accepted), pending))
        lf.log(f"\n== {name}")
        for k in sorted(stats):
            lf.log(f"   {k}: {stats[k]}")
        for c in accepted:  # names only, never emails
            lf.log(f"   + {c.title} ({c.subs:,} subs, {c.country or '??'}) -> {c.status}")

    lf.log("\nSUMMARY  (channels found -> in band -> active long-form -> public email -> Pending/English)")
    for name, n, band, qual, mail, pend in rows:
        pct = f"{100 * band / n:.0f}%" if n else "-"
        lf.log(f"  {name:38s} {n:4d} -> {band:3d} ({pct:>4s}) -> {qual:3d} -> {mail:3d} -> {pend:3d}")
    lf.log(f"quota used: {quota.run_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
