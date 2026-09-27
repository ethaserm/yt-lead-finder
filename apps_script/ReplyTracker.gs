/**
 * Reply tracker - YouTube editing outreach.
 *
 * Fills the "Replies" tab from the OUTREACH Gmail inbox and moves the matching "Outreach Tracker" row on
 * (Replied - Needs Review / Bounced / Opt-out / Do Not Contact / Auto-reply + Response Date), so the Results tab
 * stays live with nothing typed by hand. Same job as ES Agents' ReplyTracker.gs, for this sheet.
 *
 * Install once (about 3 minutes):
 *  1. Share this Google Sheet (Editor) with the outreach Gmail account.
 *  2. Signed in as the OUTREACH account, open the Sheet > Extensions > Apps Script.
 *  3. Paste this whole file over Code.gs, Save, pick `setup` in the function menu, Run, approve the permissions.
 *     From then on it checks the inbox every 30 minutes by itself.
 * It has to be installed from the outreach account: Apps Script reads the inbox of whoever created the trigger.
 */
const CFG = {
  trackerTab: 'Outreach Tracker',
  repliesTab: 'Replies',
  lookbackDays: 21,
  maxThreads: 150,
};

// Tracker statuses the script may move on automatically. Anything Ethan set by hand is left alone
// (except an opt-out, which always wins unless they already bought).
const AUTO_STATUSES = ['', 'Sent - Awaiting Reply', 'Follow-up 1 Sent', 'Follow-up 2 Sent', 'Auto-reply'];

function setup() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'checkReplies')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('checkReplies').timeBased().everyMinutes(30).create();
  checkReplies();
}

function checkReplies() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const tracker = ss.getSheetByName(CFG.trackerTab);
  const replies = ss.getSheetByName(CFG.repliesTab);
  if (!tracker || !replies) throw new Error('Missing "' + CFG.trackerTab + '" or "' + CFG.repliesTab + '" tab');
  const me = Session.getEffectiveUser().getEmail().toLowerCase();

  // Outreach Tracker index: contact email -> row info
  const tv = tracker.getDataRange().getValues();
  const th = tv[0].map(h => String(h).trim().toLowerCase());
  const tc = name => th.indexOf(name);
  const cEmail = tc('contact email'), cName = tc('channel name'), cStatus = tc('status'), cResp = tc('response date');
  if (cEmail < 0 || cStatus < 0) throw new Error('Outreach Tracker header is missing Contact Email / Status');
  const byEmail = {};
  for (let r = 1; r < tv.length; r++) {
    const e = String(tv[r][cEmail] || '').trim().toLowerCase();
    if (e) byEmail[e] = { row: r + 1, name: cName >= 0 ? tv[r][cName] : '', status: String(tv[r][cStatus] || '') };
  }

  // Message IDs already logged
  const rv = replies.getDataRange().getValues();
  const rh = rv[0].map(h => String(h).trim().toLowerCase());
  const cMsg = rh.indexOf('message id');
  const seen = new Set(rv.slice(1).map(r => String(r[cMsg] || '')));

  const threads = GmailApp.search('in:inbox newer_than:' + CFG.lookbackDays + 'd', 0, CFG.maxThreads);
  const newRows = [];
  const updates = [];
  threads.forEach(thread => {
    const msgs = thread.getMessages();
    const ours = msgs.filter(m => addr(m.getFrom()) === me);
    const sentTo = [];
    ours.forEach(m => addrs(m.getTo()).forEach(a => sentTo.push(a)));
    msgs.forEach(m => {
      const id = m.getId();
      if (seen.has(id)) return;
      const from = addr(m.getFrom());
      if (from === me) return;
      const body = m.getPlainBody() || '';
      const type = classify(m, from, body);
      if (type === 'Delay') return; // Gmail is still retrying; a real failure (or nothing) follows
      let related = from;
      if (type === 'Bounce') related = failedAddress(m, body) || sentTo[0] || '';
      let match = byEmail[related];
      if (!match) {
        const alt = sentTo.find(a => byEmail[a]);
        if (alt) { match = byEmail[alt]; related = alt; }
      }
      if (!match && !ours.length) return; // not about our outreach
      seen.add(id);
      newRows.push([
        m.getDate(), safe(from), safe(match ? match.name : ''), safe(m.getSubject()), safe(snippet(body)), type,
        'No', type === 'Bounce' && related ? 'Failed address: ' + related : (match && related !== from ? 'Contacted address: ' + related : ''),
        id,
      ]);
      if (match) updates.push({ match: match, type: type, date: m.getDate() });
    });
  });

  if (newRows.length) {
    replies.getRange(replies.getLastRow() + 1, 1, newRows.length, newRows[0].length).setValues(newRows);
  }
  updates.forEach(u => {
    const next = nextStatus(u.match.status, u.type);
    if (!next) return;
    tracker.getRange(u.match.row, cStatus + 1).setValue(next);
    u.match.status = next;
    if (cResp >= 0 && (u.type === 'Reply' || u.type === 'Opt-out') && !tracker.getRange(u.match.row, cResp + 1).getValue()) {
      tracker.getRange(u.match.row, cResp + 1).setValue(u.date);
    }
  });
  console.log('Replies logged: ' + newRows.length + ', tracker rows updated: ' + updates.length);
}

function nextStatus(current, type) {
  const auto = AUTO_STATUSES.indexOf(current) >= 0;
  if (type === 'Opt-out') return current === 'Bought' || current === 'Opt-out / Do Not Contact' ? '' : 'Opt-out / Do Not Contact';
  if (!auto) return '';
  if (type === 'Reply') return 'Replied - Needs Review';
  if (type === 'Bounce') return 'Bounced';
  if (type === 'Auto-reply') return current === 'Auto-reply' ? '' : 'Auto-reply';
  return '';
}

function classify(m, from, body) {
  const subject = m.getSubject() || '';
  if (/mailer-daemon|postmaster/i.test(from) ||
      /delivery status notification|undeliverable|undelivered|mail delivery (failed|subsystem)|returned mail|failure notice/i.test(subject)) {
    return /\(delay\)|delivery incomplete|will retry|temporar/i.test(subject + ' ' + body.slice(0, 400)) ? 'Delay' : 'Bounce';
  }
  const autoSubmitted = String(m.getHeader('Auto-Submitted') || '').toLowerCase();
  const precedence = String(m.getHeader('Precedence') || '').toLowerCase();
  if ((autoSubmitted && autoSubmitted !== 'no') || m.getHeader('X-Autoreply') || precedence === 'auto_reply' ||
      /^(automatic reply|auto[- ]?reply|autoreply|out of (the )?office)/i.test(subject)) {
    return 'Auto-reply';
  }
  const fresh = stripQuoted(body).slice(0, 800);
  if (/\b(unsubscribe|remove me|take me off|stop (emailing|contacting|messaging)|do not (email|contact)|don'?t (email|contact)|opt[- ]?out)\b/i.test(fresh)) {
    return 'Opt-out';
  }
  return 'Reply';
}

function failedAddress(m, body) {
  const header = String(m.getHeader('X-Failed-Recipients') || '').trim();
  if (header) return header.split(',')[0].trim().toLowerCase();
  const hit = body.match(/(?:delivered to|message to|address(?: not found)?:?|recipient)\s*<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?/i);
  return hit ? hit[1].toLowerCase() : '';
}

function stripQuoted(body) {
  const cut = body.search(/\n(On .{5,200}wrote:|-{2,} ?Original Message|From: .+\n(Sent|Date): )/i);
  const text = cut > 0 ? body.slice(0, cut) : body;
  return text.split('\n').filter(l => !/^\s*>/.test(l)).join('\n');
}

function snippet(body) {
  return stripQuoted(body).replace(/\s+/g, ' ').trim().slice(0, 300);
}

function addr(raw) {
  const m = String(raw || '').match(/[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/);
  return m ? m[0].toLowerCase() : '';
}

function addrs(raw) {
  return (String(raw || '').match(/[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/g) || []).map(a => a.toLowerCase());
}

function safe(v) {
  const s = String(v || '');
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}
