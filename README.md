# YT lead finder + emailer (video editing outreach)

Two scheduled GitHub Actions workflows, no Claude session involved anywhere:

| Workflow | Script | Schedule | What it does |
|---|---|---|---|
| **Lead finder** | `leadfinder.py` | :23 and :53 past every hour (UTC) | Finds YouTube channels in the subscriber range (10k–50k by default) through the official YouTube Data API v3, takes a contact email only if it's written publicly, de-duplicates against the whole Sheet, adds new leads to **Business Queue**. |
| **Emailer** | `emailer.py` | :08 and :38 past the hour, 12:00–21:59 UTC | Checks the outreach inbox for replies/bounces/opt-outs, then sends its share of today's cap to `Pending` leads through Gmail SMTP, logs each send to **Outreach Tracker**, marks the queue row `Emailed`. |

Both have **Run workflow** buttons (`workflow_dispatch`) with a **dry run** tick box that's on by default.

The control panel is the Google Sheet `YT_Editing_Hub`: settings (subscriber range, niches, sending ramp, pause switch, sender name) live on the **Lists** tab and the email copy on the **Templates** tab, so nothing about the pitch or prices is in this public repo.

Everything is standalone: its own Google Cloud project (**YT Editing Outreach**, id `youtube-hub-500506`), its own service account, Sheet, repo and outreach Gmail. Nothing is shared with any other project.

## Rules built in

- **Official API only.** Discovery is `search.list` → `channels.list` → `playlistItems.list` / `videos.list`. youtube.com pages are never scraped, and YouTube's CAPTCHA-gated "View email address" button is never touched.
- **Emails are never guessed.** An address is used only if it's plain visible text in the channel description, a recent video description (labelled as a contact, repeated as a footer, or clearly the creator's own — sponsor lines like "use code…" / "support@brand" are ignored), or on a linked site/link-in-bio page fetched normally with `robots.txt` respected (Linktree's robots.txt blocks automated fetches, so Linktree links are skipped). The domain must have a real MX record. **No plain-text email → the channel is skipped.**
- **Qualified leads only:** uploaded in the last 30 days and at least 2 of the last 6 uploads over 3 minutes (Shorts-only channels don't need intros).
- **UK channels** go to `Review - UK channel (PECR)` (UK individuals/sole traders need consent for cold email); non-English channels to `Review - non-English`. The emailer never touches `Review` rows.
- **YouTube's 30-day data rule:** queue rows are re-checked through the API after 28 days, or removed if they no longer qualify or were already emailed. Channel IDs in `state.json` expire after 30 days.
- **Nothing is sent** until a row on the Templates tab is marked `Ready? = Yes` — and a template with placeholder text or an unknown `{placeholder}` is refused.
- **Never re-emailed:** anyone already in Outreach Tracker (same email, channel or name+email), anyone who bounced or opted out (Replies tab — opted-out company domains too).
- Logs never print email addresses (the repo and its Actions logs are public).

## Budgets

- **YouTube quota** (free, 10,000 units/day, no billing): ~3 searches an hour × ~105 units (100 for the search + the channel/video checks) ≈ 7,500 units/day. GitHub delays or drops many scheduled runs, so each run sizes itself by the time since the last run that actually happened (`SEARCHES_PER_HOUR` × hours, max `MAX_SEARCHES_PER_RUN`). `DAILY_QUOTA_BUDGET=9000` is a hard stop; the quota day resets at midnight Pacific. The rotation cursor lives in `state.json` and is committed back after every run, so each run covers new ground.
- **Search order:** every search term is tried with `order=relevance` before `order=date` - on a side-by-side test (the *Compare search settings* workflow) relevance found about twice as many 10k–50k channels with a public email.
- **Sending ramp** (brand-new Gmail): 15/day in week 1 → 20 → 30 → 40 → 50, counted from the first email ever sent (edit the table on the Lists tab, or set *Daily cap override*). Each run sends `ceil(left today ÷ runs left today)`, max 6, with 45–120 s between emails; a dropped run just makes the next one's share bigger.
- **Actions minutes:** free (public repo).

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Status |
|---|---|
| `SHEET_ID` | set |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → project **YT Editing Outreach** → IAM & Admin → Service accounts → create one for this pipeline (no roles needed) → Keys → Add key → JSON. Paste the whole file in here, then share the Sheet with that service account's email as Editor. |
| `YOUTUBE_API_KEY` | Google Cloud Console → project **YT Editing Outreach** → APIs & Services → Credentials → Create credentials → API key → restrict it to YouTube Data API v3. The YouTube, Sheets and Drive APIs are already enabled in that project. No billing needed. |
| `GMAIL_SENDER_ADDRESS` | the dedicated outreach Gmail address |
| `GMAIL_APP_PASSWORD` | on that account: turn on 2-Step Verification, then create an App Password at myaccount.google.com/apppasswords (16 characters). Used for both SMTP (sending) and IMAP (reading replies). |

Until the secrets are there, scheduled runs skip with a warning instead of failing.

## Going live checklist

1. Add the secrets above.
2. *Actions → Lead finder → Run workflow* (dry run ticked) → check the log, then untick for a real run.
3. Write the pitch on the **Templates** tab (placeholders: `{channel_name}` `{niche}` `{subscribers}` `{subscribers_short}`; include an opt-out line and a postal address for US law), set `Ready?` to `Yes`.
4. *Actions → Emailer → Run workflow* (dry run ticked) → check who it would email. From then on the schedule does the rest. Pause any time with *Emailer paused = Yes* on the Lists tab.

## Tuning

- **Lists tab:** subscriber min/max, niches to target (column A — a niche that has no search terms in `config.json` is searched by its own name), ramp, pause, sender name, reply checking.
- **config.json:** search terms per niche, activity rules, UK / non-English handling (`review` or anything else to let them through), `record_no_contact`.
- **Workflow env:** `SEARCHES_PER_HOUR` / `MAX_SEARCHES_PER_RUN` (or `SEARCHES_PER_RUN` for a fixed number), budgets, `SEND_WINDOW_UTC` + `RUNS_PER_HOUR` (must match the emailer cron), `MAX_PER_RUN`, gaps between sends.
- **Compare search settings** (manual workflow): tries a few terms with different search settings and reports which finds the most usable leads. Uses ~1,600 quota units.
- Tests: `python -m pytest tests`.
