# YT lead finder (video editing outreach)

Finds YouTube channels with **10k–50k subscribers** that upload long-form videos regularly, takes a contact email **only if the creator has written it publicly**, and appends them to the **Business Queue** tab of the `YT_Editing_Hub` Google Sheet. Runs on GitHub Actions every 2 hours. Same architecture as `es-lead-finder` (config.json + state.json rotation + Sheets + a Claude scheduled task that does the emailing).

## How a channel gets in

1. **Search (official YouTube Data API v3 only).** Each run does 3 `search.list` calls (100 units each) for the next terms in the rotation in `config.json`: recent uploads (last 30 days), medium/long videos only (so Shorts-only channels never show up), English relevance, Gaming category for the gaming terms. The rotation cursor and page tokens live in `state.json`, committed back after every run, so nothing is rescanned. When the rotation wraps, a new cycle starts with a fresh 30-day window.
2. **Subscriber band.** `channels.list` (1 unit per 50 channels) → keep 10,000–50,000, skip hidden counts.
3. **Active + long-form.** `playlistItems.list` + `videos.list` (1 unit each) for the latest 6 uploads: last upload within 30 days and at least 2 of them over 3 minutes.
4. **Email, never guessed:**
   - plain text in the channel description (About), labelled ones ("Business inquiries: …") first;
   - plain text in a recent video description, but only when it's labelled as a contact, repeated across several descriptions (a footer), or clearly the creator's own address. Sponsor lines ("use code…", "support@brand") are ignored;
   - otherwise a linked link-in-bio page (Beacons, Carrd, bio.link, …) or a personal site whose domain matches the channel name, fetched normally with `robots.txt` respected (Linktree's robots.txt blocks automated fetches, so Linktree links are skipped);
   - `[at]` / `[dot]` spellings are read, Cloudflare-protected addresses shown on a page are decoded, junk/no-reply/image-name addresses are dropped, and the domain must have a real MX record.
   - **Never**: YouTube's CAPTCHA-gated "View email address" button, scraping youtube.com pages, or any third-party email-finder.
5. **Status:** `Pending` (ready to email) · `Review - no contact` (no public email, never emailed) · `Review - UK channel (PECR)` (UK individuals/sole traders need consent for cold email) · `Review - non-English`. The emailer only ever touches `Pending`.
6. **De-duplication / suppression:** channel IDs and emails already in Business Queue or Outreach Tracker are never re-added; any address in the Replies tab (bounces, opt-outs) is suppressed, and opted-out company domains too.

## YouTube API data retention (30 days)

YouTube's Developer Policies only allow non-authorized API data to be stored for 30 days. So each run re-checks queue rows whose *Last Checked* is 28+ days old through the API (subscriber count, activity, email still published) and updates them, or deletes the row if it no longer qualifies or never had an email. Channel IDs in `state.json` expire after 30 days too.

## Quota and Actions minutes

- ~350 units per run (3 searches + ~50 one-unit calls) × 12 runs ≈ 4,200 of the free 10,000 units/day. `DAILY_QUOTA_BUDGET` (9,000) and `RUN_QUOTA_BUDGET` (700) are hard stops; YouTube's quota day resets at midnight Pacific.
- The repo is **private** (Actions logs and the preview CSV contain creators' emails). A run bills ~1 minute, so every 2 hours ≈ 360–720 of the 2,000 free minutes a month. `timeout-minutes: 8` caps a bad run. Making the repo public removes the minutes limit if you ever need hourly runs, but then the logs are public.

## Setup

1. **YouTube API key** (free, no billing): Google Cloud Console → project `inbox-agent-t` (the ES Agents one) → *APIs & Services → Library* → enable **YouTube Data API v3** → *Credentials → Create credentials → API key* → restrict it to YouTube Data API v3.
2. **Repo secrets** (*Settings → Secrets and variables → Actions*):
   - `YOUTUBE_API_KEY` – the key from step 1
   - `GOOGLE_SERVICE_ACCOUNT_JSON` – the same service-account JSON key the es-lead-finder repo uses (`lead-finder@inbox-agent-t.iam.gserviceaccount.com`)
   - `SHEET_ID` – already set
3. **Share the sheet** `YT_Editing_Hub` with `lead-finder@inbox-agent-t.iam.gserviceaccount.com` (Editor).
4. **Test:** *Actions → Lead finder → Run workflow* with *Dry run* ticked, then open the `lead-preview` artifact. Untick for a real run.

## The other two pieces

- **Emailer:** Claude scheduled task "YT Editing Emailer (Pending Queue)". Prompt kept in `emailer/emailer_prompt.md`. It reads the `Pending (live)` tab (not the whole queue), respects the ramp on the Results tab (15/day in week 1 → 20 → 30 → 40 → 50; edit the table on the Lists tab), sends from the dedicated outreach Gmail through Composio, logs to Outreach Tracker, deletes the queue row. It does nothing until the email template is written.
- **Reply tracking:** `apps_script/ReplyTracker.gs`, installed in the sheet from the outreach Gmail account. Fills Replies and moves Outreach Tracker statuses on (replied / bounced / opt-out) every 30 minutes.

## Tuning

- `config.json`: niches and search terms (Gaming by default; add another niche as another entry in `niches`), subscriber band, activity rules, what to do with UK / non-English channels (`review` or anything else to let them through), whether to record no-contact channels.
- Workflow env: `SEARCHES_PER_RUN`, `MAX_NEW_PER_RUN`, `REFRESH_PER_RUN`, `MAX_SITE_FETCHES`, budgets.
- Run the tests with `python -m pytest tests`.
