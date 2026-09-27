import os
import smtplib
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import emailer as em  # noqa: E402
import leadfinder as lf  # noqa: E402
from fakesheet import hub  # noqa: E402

CID = ["UC" + ch * 22 for ch in "abcdefgh"]
TODAY = em.london_today().isoformat()
READY = [["A", "Yes", "Quick idea for {channel_name}",
          "Hi {channel_name},\n\nLoved your {niche} videos ({subscribers_short} subs!).\n\nReply 'no thanks' to opt out.",
          "Niche / game"]]


def qrow(i, email, status="Pending", name=None, subs=23400):
    return [name or f"Channel {i}", "Gaming", str(subs), lf.channel_url(CID[i]), email, "channel description", status,
            lf.SOURCE_LABEL, "27/09/2026", "US", "25/09/2026", "27/09/2026", "note"]


def trow(name, url, email, day, status="Sent - Awaiting Reply", template="A", msgid=""):
    return [name, "Gaming", url, email, lf.SOURCE_LABEL, day, "", "", status, "", "", "", "", template, "x", msgid]


class FakeSMTP:
    sent, logged_in = [], []

    def __init__(self, *a, **kw):
        pass

    def login(self, user, pw):
        FakeSMTP.logged_in.append((user, pw))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


ENV = {"SHEET_ID": "x", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "GMAIL_SENDER_ADDRESS": "me@gmail.com",
       "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop", "MIN_GAP_SECONDS": "0", "MAX_GAP_SECONDS": "0"}


class TestSettings(unittest.TestCase):
    def test_lists_settings_parsed(self):
        sh = hub()
        got = lf.read_lists_settings(sh.worksheet("Lists").get_all_values())
        self.assertEqual(got["niches"], ["Gaming"])
        self.assertEqual(got["ramp"][:2], [(1, 15), (8, 20)])
        self.assertEqual(got["settings"]["subscriber min"], "10,000")
        self.assertEqual(got["settings"]["sender name"], "Ethan")

    def test_apply_settings_to_finder_config(self):
        cfg = lf.load_config()
        lists = {"settings": {"subscriber min": "12,000", "subscriber max": "40000"}, "niches": ["Gaming", "Tech reviews"]}
        lf.apply_lists_settings(cfg, lists)
        self.assertEqual((cfg["subscriber_min"], cfg["subscriber_max"]), (12000, 40000))
        self.assertEqual([n["label"] for n in cfg["niches"]], ["Gaming", "Tech reviews"])
        self.assertGreater(len(cfg["niches"][0]["terms"]), 10)          # gaming keeps its search terms
        self.assertEqual(cfg["niches"][1]["terms"], ["Tech reviews"])   # new niche searched by its name


class TestTemplates(unittest.TestCase):
    def test_placeholder_template_is_blocked(self):
        t = em.read_templates([["Template", "Ready?", "Subject", "Body", "Hook Type"],
                               ["A", "Yes", "[not written yet]", "[not written yet]", "x"]])[0]
        self.assertTrue(t.ready)
        self.assertIn("still has placeholder text", t.problems())

    def test_unknown_placeholder_is_blocked(self):
        t = em.Template("A", True, "Hi {first_name}", "Body", "x")
        self.assertIn("unknown placeholder(s): {first_name}", t.problems())

    def test_render(self):
        t = em.read_templates([["Template", "Ready?", "Subject", "Body", "Hook Type"]] + READY)[0]
        self.assertEqual(t.problems(), [])
        subj, body = t.render({"name": "Pixel Pete", "niche": "Gaming", "subs": 23400})
        self.assertEqual(subj, "Quick idea for Pixel Pete")
        self.assertIn("23.4k subs", body)
        self.assertEqual(em.short_count(12000), "12k")
        self.assertEqual(em.short_count(12345), "12.3k")

    def test_rotation_least_used_first(self):
        a, b = em.Template("A", True, "s", "b", "h"), em.Template("B", True, "s", "b", "h")
        self.assertEqual(em.pick_template([a, b], {"A": 3, "B": 1}).name, "B")
        self.assertEqual(em.pick_template([a, b], {}).name, "A")


class TestBudget(unittest.TestCase):
    def tracker(self, days):
        return em.Tracker([em.TRACKER_HEADER] + [trow("c", lf.channel_url(CID[0]), f"x{i}@gmail.com", d)
                                                   for i, d in enumerate(days)])

    def test_ramp(self):
        lists = {"settings": {}, "ramp": [(1, 15), (8, 20), (15, 30), (22, 40), (29, 50)]}
        today = date(2026, 10, 1)
        self.assertEqual(em.daily_cap(lists, None, today), 15)
        self.assertEqual(em.daily_cap(lists, today - timedelta(days=7), today), 20)
        self.assertEqual(em.daily_cap(lists, today - timedelta(days=40), today), 50)
        self.assertEqual(em.daily_cap({"settings": {"daily cap override": "5"}, "ramp": []}, None, today), 5)

    def test_spread_over_the_day(self):
        lists = {"settings": {}, "ramp": [(1, 15)]}
        window = em.parse_window("12-21")
        noon = datetime(2026, 10, 1, 12, 41, tzinfo=timezone.utc)
        cap, sent, allowed = em.send_budget(lists, self.tracker([]), noon, window, 6)
        self.assertEqual((cap, sent, allowed), (15, 0, 2))           # 15 over 10 runs -> 2 now
        late = datetime(2026, 10, 1, 21, 41, tzinfo=timezone.utc)
        today = em.london_today(late).strftime("%d/%m/%Y")
        cap, sent, allowed = em.send_budget(lists, self.tracker([today] * 13), late, window, 6)
        self.assertEqual((sent, allowed), (13, 2))                   # last run sends what's left
        cap, sent, allowed = em.send_budget(lists, self.tracker([today] * 15), late, window, 6)
        self.assertEqual(allowed, 0)                                 # cap reached


class TestPickLeads(unittest.TestCase):
    def test_dedupe_and_suppression(self):
        queue = [lf.DEFAULT_HEADER,
                 qrow(0, "fresh@gmail.com"),
                 qrow(1, "done@gmail.com"),                          # already in tracker (same channel) -> Emailed
                 qrow(2, "dupe@gmail.com"),                          # email contacted via another channel
                 qrow(3, "bounced@gmail.com"),
                 qrow(4, "not-an-email"),
                 qrow(5, "second@gmail.com", status="Review - UK channel (PECR)"),
                 qrow(6, "fresh@gmail.com"),                         # same email as row 0
                 qrow(7, "another@gmail.com")]
        tracker = em.Tracker([em.TRACKER_HEADER,
                              trow("Channel 1", lf.channel_url(CID[1]), "done@gmail.com", "20/09/2026"),
                              trow("Other", lf.channel_url("UC" + "z" * 22), "dupe@gmail.com", "20/09/2026")])
        to_send, fixes = em.pick_leads(queue, tracker, {"bounced@gmail.com"}, set(), limit=5)
        self.assertEqual([l["email"] for l in to_send], ["fresh@gmail.com", "another@gmail.com"])
        got = {cid: status for cid, status, _ in fixes}
        self.assertEqual(got, {CID[1]: "Emailed", CID[2]: "Skipped", CID[3]: "Skipped", CID[4]: "Skipped",
                               CID[6]: "Skipped"})
        self.assertNotIn(CID[5], got)                                # Review rows are never touched

    def test_limit(self):
        queue = [lf.DEFAULT_HEADER] + [qrow(i, f"c{i}@gmail.com") for i in range(5)]
        to_send, _ = em.pick_leads(queue, em.Tracker([em.TRACKER_HEADER]), set(), set(), limit=2)
        self.assertEqual(len(to_send), 2)


class TestMessage(unittest.TestCase):
    def test_headers(self):
        msg = em.build_message("me@gmail.com", "Ethan", "them@gmail.com", "Hello", "Body\n")
        self.assertEqual(msg["From"], "Ethan <me@gmail.com>")
        self.assertIn("@gmail.com>", msg["Message-ID"])
        self.assertEqual(msg["List-Unsubscribe"], "<mailto:me@gmail.com?subject=unsubscribe>")
        self.assertEqual(msg.get_content(), "Body\n")


def raw_mail(frm, subject, body, msgid, extra=""):
    return (f"From: {frm}\r\nTo: me@gmail.com\r\nSubject: {subject}\r\nMessage-ID: <{msgid}>\r\n"
            f"Date: Mon, 28 Sep 2026 10:00:00 +0000\r\n{extra}Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


class TestReplies(unittest.TestCase):
    def setUp(self):
        self.tracker = em.Tracker([em.TRACKER_HEADER,
                                   trow("Pixel Pete", lf.channel_url(CID[0]), "pete@gmail.com", "27/09/2026",
                                        msgid="abc123@gmail.com"),
                                   trow("Quiet Quinn", lf.channel_url(CID[1]), "quinn@gmail.com", "27/09/2026"),
                                   trow("Gone", lf.channel_url(CID[2]), "gone@nowhere.io", "27/09/2026"),
                                   trow("Away", lf.channel_url(CID[3]), "away@gmail.com", "27/09/2026")])

    def test_classify_and_match(self):
        msgs = [
            # reply from the creator's manager, matched through In-Reply-To
            raw_mail("Manager <boss@agency.com>", "Re: Quick idea", "Sounds good, what's the price?\n\nOn Sun wrote:\n> hi",
                     "r1@agency.com", "In-Reply-To: <abc123@gmail.com>\r\n"),
            raw_mail("quinn@gmail.com", "Re: Quick idea", "Please remove me from your list", "r2@gmail.com"),
            raw_mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
                     "Address not found. Your message wasn't delivered to gone@nowhere.io because the address "
                     "couldn't be found.", "r3@google.com", "X-Failed-Recipients: gone@nowhere.io\r\n"),
            raw_mail("away@gmail.com", "Automatic reply: Quick idea", "I'm away", "r4@gmail.com",
                     "Auto-Submitted: auto-replied\r\n"),
            raw_mail("news@shop.com", "Big sale", "50% off", "r5@shop.com"),                    # unrelated -> ignored
            raw_mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Delay)",
                     "Delivery incomplete, will retry", "r6@google.com"),                          # delay -> ignored
            raw_mail("quinn@gmail.com", "Re: Quick idea", "dupe", "r2@gmail.com"),                 # same Message-ID
        ]
        rows, updates = em.process_messages(msgs, "me@gmail.com", self.tracker, set())
        kinds = [(r[2], r[5]) for r in rows]
        self.assertEqual(kinds, [("Pixel Pete", "Reply"), ("Quiet Quinn", "Opt-out"), ("Gone", "Bounce"),
                                 ("Away", "Auto-reply")])
        self.assertEqual(rows[0][7], "Contacted address: pete@gmail.com")
        self.assertEqual(rows[2][7], "Failed address: gone@nowhere.io")
        self.assertEqual([(r, s) for r, s, _ in updates], [(2, "Replied - Needs Review"),
                                                           (3, "Opt-out / Do Not Contact"), (4, "Bounced"),
                                                           (5, "Auto-reply")])
        self.assertEqual(updates[0][2], "2026-09-28")

    def test_manual_status_not_overwritten_but_opt_out_wins(self):
        self.assertEqual(em.next_status("Replied - Interested", "Reply"), "")
        self.assertEqual(em.next_status("Replied - Interested", "Opt-out"), "Opt-out / Do Not Contact")
        self.assertEqual(em.next_status("Bought", "Opt-out"), "")


class TestRun(unittest.TestCase):
    def setUp(self):
        FakeSMTP.sent, FakeSMTP.logged_in = [], []

    def run_emailer(self, sh, dry=False, inbox=(), smtp=FakeSMTP, window="0-23"):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(lf, "open_sheet", return_value=sh), \
                mock.patch.object(em, "fetch_inbox", return_value=list(inbox)), \
                mock.patch.object(em.smtplib, "SMTP_SSL", smtp):
            return em.run(dry, max_per_run=6, window=em.parse_window(window))

    def test_template_not_ready_sends_nothing(self):
        sh = hub(queue_rows=[qrow(0, "a@gmail.com"), qrow(1, "b@gmail.com")])
        self.assertEqual(self.run_emailer(sh), 0)
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(len(sh.worksheet("Outreach Tracker").get_all_values()), 1)
        self.assertEqual([r[6] for r in sh.worksheet("Business Queue").get_all_values()[1:]], ["Pending", "Pending"])

    def test_sends_logs_and_marks_rows(self):
        queue = [qrow(i, f"c{i}@gmail.com", name=f"Creator {i}") for i in range(4)] + \
                [qrow(4, "c9@gmail.com", status="Review - non-English")]
        sh = hub(queue_rows=queue, templates=READY)
        self.assertEqual(self.run_emailer(sh, window="0-0"), 0)    # one run left today -> whole allowance (max 6)
        self.assertEqual(len(FakeSMTP.sent), 4)
        self.assertEqual(FakeSMTP.logged_in, [("me@gmail.com", "abcdefghijklmnop")])
        msg = FakeSMTP.sent[0]
        self.assertEqual((msg["To"], msg["Subject"]), ("c0@gmail.com", "Quick idea for Creator 0"))
        self.assertEqual(msg["From"], "Ethan <me@gmail.com>")
        tracker = sh.worksheet("Outreach Tracker").get_all_values()
        self.assertEqual(len(tracker), 5)
        self.assertEqual(tracker[1][:4], ["Creator 0", "Gaming", lf.channel_url(CID[0]), "c0@gmail.com"])
        self.assertEqual((tracker[1][5], tracker[1][8], tracker[1][13]), (TODAY, "Sent - Awaiting Reply", "A"))
        self.assertEqual(tracker[1][15], str(msg["Message-ID"]).strip("<>"))
        statuses = [r[6] for r in sh.worksheet("Business Queue").get_all_values()[1:]]
        self.assertEqual(statuses, ["Emailed"] * 4 + ["Review - non-English"])
        # a second run the same day doesn't re-send anyone
        FakeSMTP.sent = []
        self.run_emailer(sh, window="0-0")
        self.assertEqual(FakeSMTP.sent, [])

    def test_daily_cap_respected(self):
        tracker = [trow(f"Old {i}", lf.channel_url("UC" + f"{i:022d}"), f"o{i}@gmail.com", TODAY) for i in range(14)]
        sh = hub(queue_rows=[qrow(i, f"c{i}@gmail.com") for i in range(5)], tracker_rows=tracker, templates=READY)
        self.run_emailer(sh, window="0-0")
        self.assertEqual(len(FakeSMTP.sent), 1)                     # 15/day on day 1, 14 already sent today

    def test_dry_run_writes_nothing(self):
        sh = hub(queue_rows=[qrow(0, "a@gmail.com"), qrow(1, "b@gmail.com")], templates=READY)
        before = {n: ws.get_all_values() for n, ws in sh.tabs.items()}
        self.assertEqual(self.run_emailer(sh, dry=True, window="0-0"), 0)
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual({n: ws.get_all_values() for n, ws in sh.tabs.items()}, before)
        preview = open(os.path.join(lf.HERE, "output", "emails_preview.txt")).read()
        self.assertIn("Subject: Quick idea for Channel 0", preview)

    def test_paused(self):
        sh = hub(queue_rows=[qrow(0, "a@gmail.com")], templates=READY, settings={"Emailer paused": "Yes"})
        self.run_emailer(sh, window="0-0")
        self.assertEqual(FakeSMTP.sent, [])

    def test_opt_out_in_inbox_blocks_the_send(self):
        tracker = [trow("Quinn", lf.channel_url(CID[5]), "quinn@gmail.com", "20/09/2026")]
        sh = hub(queue_rows=[qrow(0, "quinn@gmail.com", name="Quinn Again")], tracker_rows=tracker, templates=READY)
        inbox = [raw_mail("quinn@gmail.com", "Re: hi", "unsubscribe please", "o1@gmail.com")]
        self.run_emailer(sh, inbox=inbox, window="0-0")
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(sh.worksheet("Replies").get_all_values()[1][5], "Opt-out")
        self.assertEqual(sh.worksheet("Outreach Tracker").get_all_values()[1][8], "Opt-out / Do Not Contact")
        self.assertEqual(sh.worksheet("Business Queue").get_all_values()[1][6], "Skipped")

    def test_bad_app_password(self):
        class BadLogin(FakeSMTP):
            def login(self, user, pw):
                raise smtplib.SMTPAuthenticationError(535, b"bad")
        sh = hub(queue_rows=[qrow(0, "a@gmail.com")], templates=READY)
        self.assertEqual(self.run_emailer(sh, smtp=BadLogin, window="0-0"), 1)
        self.assertEqual(sh.worksheet("Business Queue").get_all_values()[1][6], "Pending")

    def test_missing_secrets_skip(self):
        with mock.patch.dict(os.environ, {"SHEET_ID": "", "GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            self.assertEqual(em.run(False, 6, em.parse_window("12-21")), 0)
        env = dict(ENV, GMAIL_APP_PASSWORD="")
        with mock.patch.dict(os.environ, env), mock.patch.object(lf, "open_sheet") as op:
            self.assertEqual(em.run(False, 6, em.parse_window("12-21")), 0)
            op.assert_not_called()


class TestSheetAccess(unittest.TestCase):
    def test_no_access_names_the_service_account(self):
        import gspread
        key = '{"client_email": "bot@proj.iam.gserviceaccount.com"}'
        gc = mock.Mock()
        gc.open_by_key.side_effect = PermissionError()
        env = dict(ENV, GOOGLE_SERVICE_ACCOUNT_JSON=key)
        with mock.patch.dict(os.environ, env), mock.patch.object(gspread, "service_account_from_dict", return_value=gc):
            with self.assertRaises(PermissionError) as ctx:
                lf.open_sheet()
            self.assertIn("bot@proj.iam.gserviceaccount.com", str(ctx.exception))
            self.assertEqual(em.run(True, 6, em.parse_window("12-21")), 1)


class TestFinderQueueCleanup(unittest.TestCase):
    def test_emailed_rows_removed_after_28_days(self):
        old = "01/08/2026"
        sh = hub(queue_rows=[qrow(0, "a@gmail.com", status="Emailed")[:11] + [old, "x"],
                             qrow(1, "b@gmail.com", status="Skipped")[:11] + [old, "x"]])
        ws = sh.worksheet("Business Queue")
        for r in ws.values[1:]:
            r[8] = old
        updated, deleted = lf.refresh_queue(None, sh, ws, lf.load_config(), None, lf.EmailRules(check_mx=False), 40,
                                            dry_run=False)
        self.assertEqual((updated, deleted), (0, 2))                 # no API calls needed (yt=None)
        self.assertEqual(len(ws.get_all_values()), 1)


if __name__ == "__main__":
    unittest.main()
