import json
import os
import sys
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leadfinder as lf  # noqa: E402

CFG = lf.load_config()
FREEMAIL = set(CFG["freemail"])
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def video(vid, days_ago=2, seconds=600, desc="", audio="en"):
    return {"id": vid, "snippet": {"publishedAt": lf.iso_z(NOW - timedelta(days=days_ago)), "description": desc,
                                   "defaultAudioLanguage": audio},
            "contentDetails": {"duration": f"PT{seconds // 60}M{seconds % 60}S"}}


def channel(cid, title="Pixel Pete", subs=23000, desc="", country="US", handle="@pixelpete", hidden=False, lang=""):
    return {"id": cid, "snippet": {"title": title, "description": desc, "customUrl": handle, "country": country,
                                   "defaultLanguage": lang},
            "statistics": {"subscriberCount": str(subs), "hiddenSubscriberCount": hidden, "videoCount": "120"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}


CID = "UC" + "a" * 22
CID2 = "UC" + "b" * 22
CID3 = "UC" + "c" * 22


class FakeResp:
    def __init__(self, text="", status=200, ctype="text/html", url=""):
        self.text, self.status_code, self.url = text, status, url
        self.headers = {"Content-Type": ctype}
        self.encoding = "utf-8"
        body = text.encode()

        class Raw:
            def read(_, n, decode_content=True):
                return body[:n]
        self.raw = Raw()

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.pages.get(url, FakeResp("", 404))


class FakeYT:
    """Stands in for lf.YouTube: canned search pages, channels and videos."""

    def __init__(self, pages=None, channels=None, uploads=None, videos=None, quota=None):
        self.pages = pages or {}
        self.chan = {c["id"]: c for c in (channels or [])}
        self.uploads = uploads or {}
        self.vids = videos or {}
        self.search_calls = []
        self.quota = quota

    def search(self, slot, window, token=None):
        self.search_calls.append((slot["term"], token))
        return self.pages.get((slot["term"], token), ([], None))

    def channels(self, ids):
        return [self.chan[i] for i in ids if i in self.chan]

    def recent_video_ids(self, uploads, count):
        return self.uploads.get(uploads, [])[:count]

    def videos(self, ids):
        return {i: self.vids[i] for i in ids if i in self.vids}


class NoMXRules(lf.EmailRules):
    def __init__(self, **kw):
        super().__init__(check_mx=False, **kw)


class TestParsing(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(lf.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(lf.parse_duration("PT45S"), 45)
        self.assertEqual(lf.parse_duration("P1DT1M"), 86460)
        self.assertEqual(lf.parse_duration("garbage"), 0)

    def test_sheet_dates(self):
        self.assertEqual(lf.parse_sheet_date("27/09/2026"), date(2026, 9, 27))
        self.assertEqual(lf.parse_sheet_date("2026-09-27"), date(2026, 9, 27))
        self.assertIsNone(lf.parse_sheet_date(""))

    def test_name_match_is_strict(self):
        keys = lf.name_keys("Pixel Pete Gaming", "@PixelPeteYT")
        self.assertTrue(lf.name_match("pixelpete", keys))
        self.assertTrue(lf.name_match("pixelpetebusiness", keys))
        game_keys = lf.name_keys("Game Reviews Daily", "@grd")
        self.assertFalse(lf.name_match("reviews", game_keys))  # a sponsor/generic site isn't "their" site
        self.assertFalse(lf.name_match("pete", keys))           # too short to trust

    def test_sheet_text_blocks_formulas(self):
        self.assertEqual(lf.sheet_text("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(lf.sheet_text(""), "")


class TestDescriptionEmails(unittest.TestCase):
    keys = lf.name_keys("Pixel Pete", "@pixelpete")

    def test_channel_description_labelled_first(self):
        desc = "Minecraft every week!\nfan mail: fans@gmail.com\nBusiness inquiries: pete.biz@gmail.com"
        got = lf.description_emails(desc, [], self.keys, FREEMAIL)
        self.assertEqual(got[0][0], "pete.biz@gmail.com")
        self.assertEqual(got[0][1], "channel description")
        self.assertIn("fans@gmail.com", [g[0] for g in got])

    def test_obfuscated_email(self):
        got = lf.description_emails("Contact: pete [at] pixelpete [dot] com", [], self.keys, FREEMAIL)
        self.assertEqual(got[0][0], "pete@pixelpete.com")

    def test_sponsor_email_in_video_rejected(self):
        vids = ["This video is sponsored by NordVPN! Use code PETE for 60% off. Questions? help@nordvpn.com"]
        self.assertEqual(lf.description_emails("", vids, self.keys, FREEMAIL), [])

    def test_unlabelled_one_off_video_email_rejected(self):
        vids = ["Thanks to randomdude@gmail.com for the seed"]
        self.assertEqual(lf.description_emails("", vids, self.keys, FREEMAIL), [])

    def test_labelled_video_email_accepted(self):
        vids = ["Today we beat the dragon\n\nFor business: managed@talentco.com"]
        got = lf.description_emails("", vids, self.keys, FREEMAIL)
        self.assertEqual(got, [("managed@talentco.com", "video description",
                                "labelled email in a recent video description")])

    def test_repeated_footer_accepted(self):
        vids = ["ep 1\nteam@crewhq.net", "ep 2\nteam@crewhq.net"]
        got = lf.description_emails("", vids, self.keys, FREEMAIL)
        self.assertEqual(got[0][0], "team@crewhq.net")
        self.assertIn("repeated in 2", got[0][2])

    def test_junk_filtered(self):
        desc = "noreply@youtube.com logo@2x.png you@example.com"
        self.assertEqual(lf.description_emails(desc, [], self.keys, FREEMAIL), [])


class TestLinks(unittest.TestCase):
    keys = lf.name_keys("Pixel Pete", "@pixelpete")

    def test_classify(self):
        self.assertEqual(lf.classify_link("https://twitter.com/pixelpete", self.keys), "")
        self.assertEqual(lf.classify_link("https://linktr.ee/pixelpete", self.keys), "hub")
        self.assertEqual(lf.classify_link("https://pete.carrd.co", self.keys), "hub")
        self.assertEqual(lf.classify_link("https://pixelpete.com/about", self.keys), "site")
        self.assertEqual(lf.classify_link("https://nordvpn.com/pete", self.keys), "")

    def test_linked_pages_rules(self):
        about = "Links: https://beacons.ai/pixelpete https://pixelpete.com https://store.steampowered.com/app/1"
        vids = ["collab with https://linktr.ee/someoneelse", "my links https://linktr.ee/pixelpete"]
        got = lf.linked_pages(about, vids, self.keys)
        self.assertEqual([u for u, _ in got], ["https://beacons.ai/pixelpete", "https://pixelpete.com",
                                               "https://linktr.ee/pixelpete"])

    def test_page_emails(self):
        key = 0x42
        plain = "cf@pixelpete.com"
        enc = "%02x" % key + "".join("%02x" % (ord(c) ^ key) for c in plain)
        page = ('<script>var cfg={"tracking":"ops@analytics.io","links":[{"url":"mailto:pete@gmail.com"}]}</script>'
                f'<p>Write to <b>hello@pixelpete.com</b></p><a data-cfemail="{enc}">[email&#160;protected]</a>')
        got = lf.page_emails(page)
        self.assertIn("pete@gmail.com", got)       # mailto button data
        self.assertIn("hello@pixelpete.com", got)  # visible text
        self.assertIn(plain, got)                  # cloudflare-protected visible email
        self.assertNotIn("ops@analytics.io", got)  # script data that isn't a mailto link


class TestSiteFetcher(unittest.TestCase):
    keys = lf.name_keys("Pixel Pete", "@pixelpete")

    def test_robots_disallow_is_respected(self):
        s = FakeSession({"https://linktr.ee/robots.txt": FakeResp("User-agent: *\nDisallow: /", ctype="text/plain"),
                         "https://linktr.ee/pixelpete": FakeResp("mailto:pete@gmail.com")})
        f = lf.SiteFetcher(s, 10)
        email, note, notes = lf.site_emails(f, [("https://linktr.ee/pixelpete", "hub")], self.keys, FREEMAIL)
        self.assertIsNone(email)
        self.assertIn("robots.txt", notes[0])
        self.assertNotIn("https://linktr.ee/pixelpete", s.calls)

    def test_site_follows_contact_page(self):
        s = FakeSession({
            "https://pixelpete.com/robots.txt": FakeResp("", 404),
            "https://pixelpete.com": FakeResp('<a href="/contact">Contact</a> <a href="https://nord.com">x</a>'),
            "https://pixelpete.com/contact": FakeResp("<p>biz@pixelpete.com or agency@otheragency.com</p>"),
        })
        f = lf.SiteFetcher(s, 10)
        email, note, _ = lf.site_emails(f, [("https://pixelpete.com", "site")], self.keys, FREEMAIL)
        self.assertEqual(email, "biz@pixelpete.com")
        self.assertIn("linked site pixelpete.com", note)

    def test_fetch_cap(self):
        f = lf.SiteFetcher(FakeSession({}), 0)
        self.assertEqual(f.get("https://x.com")[1], "fetch cap reached")


class TestQualify(unittest.TestCase):
    def cand(self, videos, **kw):
        c = lf.Candidate(channel(CID, **kw), {"niche": "Gaming", "term": "minecraft"})
        c.videos = videos
        return c

    def test_active_long_form_ok(self):
        c = self.cand([video("v1", 2, 700), video("v2", 9, 900), video("v3", 12, 30)])
        self.assertEqual(lf.qualify(c, CFG, NOW), "")
        self.assertEqual(c.last_upload, (NOW - timedelta(days=2)).date().isoformat())

    def test_inactive(self):
        c = self.cand([video("v1", 45, 700), video("v2", 60, 900)])
        self.assertEqual(lf.qualify(c, CFG, NOW), "inactive")

    def test_shorts_only(self):
        c = self.cand([video("v1", 1, 40), video("v2", 2, 55), video("v3", 3, 590)])
        self.assertEqual(lf.qualify(c, CFG, NOW), "shorts_only")

    def test_status_rules(self):
        c = self.cand([video("v1")], country="GB")
        self.assertEqual(lf.decide_status(c, CFG), lf.STATUS_NO_CONTACT)
        c.email = "a@gmail.com"
        self.assertEqual(lf.decide_status(c, CFG), lf.STATUS_UK)
        c = self.cand([video("v1", audio="es"), video("v2", audio="es")], country="US")
        c.email = "a@gmail.com"
        self.assertEqual(lf.decide_status(c, CFG), lf.STATUS_NON_EN)
        c = self.cand([video("v1")], country="")
        c.email = "a@gmail.com"
        self.assertEqual(lf.decide_status(c, CFG), lf.STATUS_PENDING)

    def test_band(self):
        self.assertEqual(lf.band_reason(lf.Candidate(channel(CID, subs=9999), {}), CFG), "out_of_band")
        self.assertEqual(lf.band_reason(lf.Candidate(channel(CID, subs=50001), {}), CFG), "out_of_band")
        self.assertEqual(lf.band_reason(lf.Candidate(channel(CID, subs=10000), {}), CFG), "")
        self.assertEqual(lf.band_reason(lf.Candidate(channel(CID, hidden=True), {}), CFG), "hidden_subs")


class TestQuotaAndApi(unittest.TestCase):
    def test_budget(self):
        state = {}
        q = lf.Quota(state, daily_budget=250, run_budget=1000)
        self.assertTrue(q.can(100))
        q.spend(200)
        self.assertFalse(q.can(100))
        self.assertTrue(q.can(1))
        self.assertEqual(state["quota"]["used"], 200)

    def test_new_day_resets(self):
        state = {"quota": {"day": "2000-01-01", "used": 9999}}
        q = lf.Quota(state, 9000, 700)
        self.assertEqual(q.q["used"], 0)

    def test_api_errors(self):
        state = {}
        quota = lf.Quota(state, 9000, 700)
        err = lambda reason, code: FakeResp(json.dumps({"error": {"errors": [{"reason": reason}]}}),  # noqa: E731
                                            code, "application/json")
        s = mock.Mock()
        s.get.return_value = err("quotaExceeded", 403)
        yt = lf.YouTube("SECRETKEY", s, quota)
        with self.assertRaises(lf.QuotaExhausted) as ctx:
            yt.channels([CID])
        self.assertNotIn("SECRETKEY", str(ctx.exception))
        self.assertEqual(state["quota"]["used"], 9000)  # marked exhausted for the rest of the day
        quota2 = lf.Quota({}, 9000, 700)
        s.get.return_value = err("invalidPageToken", 400)
        with self.assertRaises(lf.BadPageToken):
            lf.YouTube("K", s, quota2).search(lf.build_slots(CFG)[0], lf.new_window(CFG, NOW), "tok")

    def test_search_counts_units_and_params(self):
        quota = lf.Quota({}, 9000, 700)
        s = mock.Mock()
        s.get.return_value = FakeResp(json.dumps({"items": [{"snippet": {"channelId": CID}},
                                                            {"snippet": {"channelId": CID}}],
                                                  "nextPageToken": "T2"}), 200, "application/json")
        yt = lf.YouTube("K", s, quota)
        slot = lf.build_slots(CFG)[0]
        ids, token = yt.search(slot, lf.new_window(CFG, NOW))
        self.assertEqual((ids, token), ([CID], "T2"))
        self.assertEqual(quota.run_used, 100)
        params = s.get.call_args.kwargs["params"]
        self.assertEqual(params["type"], "video")
        self.assertEqual(params["videoCategoryId"], "20")
        self.assertIn(params["videoDuration"], ("medium", "long"))

    def test_run_budget_blocks_calls(self):
        quota = lf.Quota({}, 9000, 150)
        s = mock.Mock()
        s.get.return_value = FakeResp(json.dumps({"items": []}), 200, "application/json")
        yt = lf.YouTube("K", s, quota)
        slot = lf.build_slots(CFG)[0]
        yt.search(slot, lf.new_window(CFG, NOW))
        with self.assertRaises(lf.QuotaExhausted):
            yt.search(slot, lf.new_window(CFG, NOW))
        self.assertEqual(s.get.call_count, 1)


class TestRotation(unittest.TestCase):
    def test_slots_interleave_niches(self):
        cfg = dict(CFG, niches=[{"label": "Gaming", "category_id": "20", "terms": ["a", "b", "c"]},
                                {"label": "Tech", "category_id": "28", "terms": ["x"]}])
        slots = lf.build_slots(cfg)
        self.assertEqual([s["term"] for s in slots[:4]], ["a", "x", "b", "c"])
        self.assertEqual(len(slots), 4 * len(CFG["search"]["durations"]) * len(CFG["search"]["orders"]))

    def test_paging_then_advance_and_wrap(self):
        cfg = dict(CFG, niches=[{"label": "Gaming", "category_id": "20", "terms": ["a", "b"]}],
                   search=dict(CFG["search"], durations=["medium"], orders=["date"], pages_per_slot=2))
        slots = lf.build_slots(cfg)
        yt = FakeYT(pages={("a", None): ([CID], "P2"), ("a", "P2"): ([CID2], "P3"),
                           ("b", None): ([CID, CID3], None)})
        state = {"cursor": 0, "slot_pages": {}, "seen": {}, "cycle": 0}
        found = lf.run_searches(yt, state, cfg, slots, 3, known_ids=set())
        self.assertEqual([cid for cid, _ in found], [CID, CID2, CID3])
        self.assertEqual(yt.search_calls, [("a", None), ("a", "P2"), ("b", None)])
        # 'a' used its 2 pages, 'b' had no next page -> wrapped: next run starts a new cycle
        self.assertEqual(state["cursor"], 0)
        self.assertIsNone(state["window"])
        self.assertEqual(state["cycle"], 1)

    def test_known_and_seen_are_skipped(self):
        slots = lf.build_slots(CFG)
        yt = FakeYT(pages={(slots[0]["term"], None): ([CID, CID2, CID3], "N")})
        state = {"cursor": 0, "slot_pages": {}, "seen": {CID2: ["2026-09-26", "out_of_band"]}, "cycle": 0}
        found = lf.run_searches(yt, state, CFG, slots, 1, known_ids={CID})
        self.assertEqual([cid for cid, _ in found], [CID3])
        self.assertEqual(state["slot_pages"][lf.slot_key(slots[0])]["token"], "N")

    def test_relevance_searches_come_first(self):
        slots = lf.build_slots(CFG)
        n_terms = len(CFG["niches"][0]["terms"]) * len(CFG["search"]["durations"])
        self.assertEqual({s["order"] for s in slots[:n_terms]}, {"relevance"})

    def test_catch_up_after_dropped_runs(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        ago = lambda h: {"last_run": lf.iso_z(now - timedelta(hours=h))}  # noqa: E731
        self.assertEqual(lf.catch_up_searches({}, 3, 10, now), 3)             # first run: one hour's worth
        self.assertEqual(lf.catch_up_searches(ago(0.5), 3, 10, now), 2)       # half-hourly schedule
        self.assertEqual(lf.catch_up_searches(ago(3), 3, 10, now), 9)         # two runs were dropped
        self.assertEqual(lf.catch_up_searches(ago(20), 3, 10, now), 10)       # capped
        self.assertEqual(lf.catch_up_searches(ago(0.05), 3, 10, now), 1)      # always at least one
        self.assertEqual(lf.catch_up_searches({"last_run": "garbage"}, 3, 10, now), 3)

    def test_prune_seen(self):
        state = {"seen": {CID: ["2026-08-01", "out_of_band"], CID2: ["2026-09-20", "added"]}}
        self.assertEqual(lf.prune_seen(state, 30, date(2026, 9, 27)), 1)
        self.assertEqual(list(state["seen"]), [CID2])


class FakeWS:
    def __init__(self, values, sheet_id=7, row_count=1000):
        self.values = [list(r) for r in values]
        self.id = sheet_id
        self.row_count = row_count
        self.added = 0
        self.batch = []
        self.appended = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def update(self, **kw):
        self.values.insert(0, kw["values"][0])

    def update_cell(self, r, c, v):
        pass

    def batch_update(self, data, **kw):
        self.batch.extend(data)

    def append_rows(self, rows, **kw):
        assert kw.get("insert_data_option") == "OVERWRITE"  # inserting rows would shift the view formulas
        self.appended.extend(rows)

    def add_rows(self, n):
        self.added += n
        self.row_count += n


class FakeSH:
    def __init__(self, ws):
        self.ws, self.requests = ws, []

    def batch_update(self, body):
        self.requests.extend(body["requests"])
        for req in sorted(body["requests"], key=lambda r: -r["deleteDimension"]["range"]["startIndex"]):
            del self.ws.values[req["deleteDimension"]["range"]["startIndex"]]


class TestRefresh(unittest.TestCase):
    def test_stale_rows_refreshed_or_deleted(self):
        old, fresh = "01/08/2026", date.today().strftime("%d/%m/%Y")
        header = lf.DEFAULT_HEADER
        row = lambda cid, email, checked: ["Ch", "Gaming", "20000", lf.channel_url(cid), email, "channel description",  # noqa: E731
                                           "Pending", lf.SOURCE_LABEL, checked, "US", "", checked, ""]
        values = [header, row(CID, "a@gmail.com", old), row(CID2, "", old), row(CID3, "c@gmail.com", fresh)]
        ws = FakeWS(values)
        sh = FakeSH(ws)
        recent = (datetime.now(timezone.utc) - timedelta(days=1))
        vids = {"v1": dict(video("v1"), snippet={"publishedAt": lf.iso_z(recent), "description": ""}),
                "v2": dict(video("v2"), snippet={"publishedAt": lf.iso_z(recent), "description": ""})}
        yt = FakeYT(channels=[channel(CID, subs=31000, desc="Business: a@gmail.com")],
                    uploads={"UU" + CID[2:]: ["v1", "v2"]}, videos=vids)
        updated, deleted = lf.refresh_queue(yt, sh, ws, CFG, None, NoMXRules(), 40, dry_run=False)
        self.assertEqual((updated, deleted), (1, 1))
        self.assertEqual(sh.requests[0]["deleteDimension"]["range"]["startIndex"], 2)  # the no-email row (row 3)
        self.assertIn({"range": "C2", "values": [[31000]]}, ws.batch)
        self.assertEqual(len(ws.values), 3)

    def test_nothing_stale(self):
        today = date.today().strftime("%d/%m/%Y")
        ws = FakeWS([lf.DEFAULT_HEADER, ["Ch", "", "1", lf.channel_url(CID), "", "", "Pending", "", today, "", "",
                                         today, ""]])
        self.assertEqual(lf.refresh_queue(FakeYT(), FakeSH(ws), ws, CFG, None, NoMXRules(), 40, False), (0, 0))


class TestEvaluate(unittest.TestCase):
    def test_end_to_end(self):
        recent = datetime.now(timezone.utc) - timedelta(days=2)

        def vid(vid_id, desc=""):
            return {"id": vid_id, "snippet": {"publishedAt": lf.iso_z(recent), "description": desc},
                    "contentDetails": {"duration": "PT12M"}}
        chans = [
            channel(CID, "Pixel Pete", 23000, "Business inquiries: pete.biz@gmail.com"),
            channel(CID2, "Big Streamer", 900000),
            channel(CID3, "Quiet Quinn", 15000, "no email here", country="GB", handle="@quietquinn"),
        ]
        uploads = {"UU" + CID[2:]: ["a1", "a2"], "UU" + CID3[2:]: ["c1", "c2"]}
        vids = {"a1": vid("a1"), "a2": vid("a2"), "c1": vid("c1"), "c2": vid("c2")}
        yt = FakeYT(channels=chans, uploads=uploads, videos=vids)
        slot = {"niche": "Gaming", "term": "minecraft", "duration": "medium", "order": "date"}
        state = {"seen": {}}
        stats = Counter()
        found = [(CID, slot), (CID2, slot), (CID3, slot)]
        accepted = lf.evaluate(yt, found, CFG, state, None, NoMXRules(), 40, float("inf"), stats)
        by_id = {c.id: c for c in accepted}
        self.assertEqual(by_id[CID].status, lf.STATUS_PENDING)
        self.assertEqual(by_id[CID].email, "pete.biz@gmail.com")
        self.assertNotIn(CID3, by_id)                   # no plain-text email -> skipped, not added
        self.assertEqual(state["seen"][CID3][1], "no_contact")
        self.assertNotIn(CID2, by_id)
        self.assertEqual(state["seen"][CID2][1], "out_of_band")
        self.assertEqual(stats["added: Pending"], 1)
        row = lf.row_for(by_id[CID], lf.DEFAULT_HEADER)
        self.assertEqual(row[:3], ["'Pixel Pete", "'Gaming", 23000])
        self.assertEqual(row[3], lf.channel_url(CID))
        self.assertEqual(row[4], "'pete.biz@gmail.com")
        self.assertEqual(row[6], "Pending")

    def test_suppressed_and_known_emails(self):
        rules = NoMXRules(known_emails={"dupe@gmail.com"}, suppressed_emails={"gone@gmail.com"},
                          suppressed_domains={"optedout.com"})
        self.assertEqual(rules.check("dupe@gmail.com", FREEMAIL), "already in the sheet")
        self.assertIn("suppressed", rules.check("gone@gmail.com", FREEMAIL))
        self.assertIn("suppressed", rules.check("x@optedout.com", FREEMAIL))
        self.assertEqual(rules.check("new@gmail.com", FREEMAIL), "ok")


class TestSheetHelpers(unittest.TestCase):
    def test_load_known_and_suppression(self):
        tracker = FakeWS([["Channel Name", "Niche / Category", "Channel URL", "Contact Email"],
                          ["A", "Gaming", lf.channel_url(CID), "A@Gmail.com"]])
        replies = FakeWS([["Date Received", "From Email", "Channel Name", "Subject", "Reply Snippet", "Type"],
                          ["x", "boss@optout-co.com", "", "Re:", "please remove me", "Opt-out"],
                          ["x", "mailer-daemon@googlemail.com", "", "DSN", "fail", "Bounce", "", "Failed address: z@gmail.com"]])
        sh = mock.Mock()
        sh.worksheet.side_effect = lambda name: {"Outreach Tracker": tracker, "Replies": replies}[name]
        ids, emails = lf.load_known(sh, ["Outreach Tracker"])
        self.assertEqual((ids, emails), ({CID}, {"a@gmail.com"}))
        sup, doms = lf.load_suppression(sh, "Replies", FREEMAIL)
        self.assertIn("z@gmail.com", sup)
        self.assertEqual(doms, {"optout-co.com"})

    def test_append_grows_grid_and_overwrites(self):
        ws = FakeWS([lf.DEFAULT_HEADER] + [["x"]] * 8, row_count=10)
        lf.append_rows(ws, [["a"], ["b"]])
        self.assertEqual(ws.appended, [["a"], ["b"]])
        self.assertGreaterEqual(ws.row_count, 9 + 2 + 5)
        ws2 = FakeWS([lf.DEFAULT_HEADER], row_count=2000)
        lf.append_rows(ws2, [["a"]])
        self.assertEqual(ws2.added, 0)

    def test_prepare_queue_tab_dry_run_does_not_write(self):
        ws = FakeWS([])
        h, header, _ = lf.prepare_queue_tab(ws, write=False)
        self.assertEqual(header, lf.DEFAULT_HEADER)
        self.assertEqual(ws.values, [])


class TestRunDry(unittest.TestCase):
    def test_missing_secrets_is_a_quiet_skip(self):
        env = {"YOUTUBE_API_KEY": "", "SHEET_ID": "", "GOOGLE_SERVICE_ACCOUNT_JSON": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(lf.requests.Session, "get") as get:
            self.assertEqual(lf.run(dry_run=False, searches=1, max_new=1), 0)
            get.assert_not_called()
        env["YOUTUBE_API_KEY"] = "K"
        with mock.patch.dict(os.environ, env), mock.patch.object(lf.requests.Session, "get") as get:
            self.assertEqual(lf.run(dry_run=False, searches=1, max_new=1), 0)  # key but no Sheet secrets
            get.assert_not_called()

    def test_dry_run_without_sheet_keeps_rotation(self):
        state_path = os.path.join(lf.HERE, "state.json")
        original = open(state_path).read() if os.path.exists(state_path) else None
        try:
            with open(state_path, "w") as fh:
                json.dump({"cursor": 5, "slot_pages": {}, "seen": {}, "cycle": 1,
                           "window": lf.new_window(CFG)}, fh)
            search_page = FakeResp(json.dumps({"items": [{"snippet": {"channelId": CID}}]}), 200, "application/json")
            chan = FakeResp(json.dumps({"items": [channel(CID, desc="business: pete.biz@gmail.com")]}), 200,
                            "application/json")
            recent = lf.iso_z(datetime.now(timezone.utc) - timedelta(days=1))
            pl = FakeResp(json.dumps({"items": [{"contentDetails": {"videoId": "v1"}},
                                                {"contentDetails": {"videoId": "v2"}}]}), 200, "application/json")
            vids = FakeResp(json.dumps({"items": [
                {"id": "v1", "snippet": {"publishedAt": recent, "description": ""}, "contentDetails": {"duration": "PT9M"}},
                {"id": "v2", "snippet": {"publishedAt": recent, "description": ""}, "contentDetails": {"duration": "PT9M"}},
            ]}), 200, "application/json")

            def fake_get(url, **kw):
                return {"search": search_page, "channels": chan, "playlistItems": pl, "videos": vids}[url.rsplit("/", 1)[1]]
            env = {"YOUTUBE_API_KEY": "K", "CHECK_MX": "false", "SHEET_ID": "", "GOOGLE_SERVICE_ACCOUNT_JSON": ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(lf.requests.Session, "get", side_effect=fake_get):
                self.assertEqual(lf.run(dry_run=True, searches=1, max_new=10), 0)
            after = json.load(open(state_path))
            self.assertEqual(after["cursor"], 5)            # rotation untouched by a dry run
            self.assertEqual(after["quota"]["used"], 103)   # but real quota use is recorded
            csv_path = os.path.join(lf.HERE, "output", f"leads_{date.today().isoformat()}.csv")
            self.assertIn("Pixel Pete", open(csv_path).read())
        finally:
            if original is None:
                os.remove(state_path)
            else:
                open(state_path, "w").write(original)


if __name__ == "__main__":
    unittest.main()
