"""In-memory stand-in for a gspread Spreadsheet, good enough to run leadfinder/emailer end to end in tests."""
import re


def a1_to_rc(a1):
    m = re.fullmatch(r"([A-Z]+)(\d+)", a1.split("!")[-1])
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


class FakeWorksheet:
    def __init__(self, title, values, sheet_id=0, row_count=1000):
        self.title, self.id, self.row_count = title, sheet_id, row_count
        self.values = [list(r) for r in values]
        self.writes = 0

    def get_all_values(self):
        width = max((len(r) for r in self.values), default=0)
        rows = [list(r) + [""] * (width - len(r)) for r in self.values]
        while rows and not any(str(c).strip() for c in rows[-1]):
            rows.pop()
        return [[str(c) for c in r] for r in rows]

    def _set(self, r, c, v):
        while len(self.values) < r:
            self.values.append([])
        row = self.values[r - 1]
        while len(row) < c:
            row.append("")
        v = v if not isinstance(v, str) or not v.startswith("'") else v[1:]  # USER_ENTERED hides the apostrophe
        row[c - 1] = v

    def batch_update(self, data, **kw):
        self.writes += 1
        for d in data:
            r, c = a1_to_rc(d["range"])
            for dr, row in enumerate(d["values"]):
                for dc, v in enumerate(row):
                    self._set(r + dr, c + dc, v)

    def append_rows(self, rows, **kw):
        assert kw.get("insert_data_option") == "OVERWRITE"
        self.writes += 1
        start = len(self.get_all_values()) + 1
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                self._set(start + i, j + 1, v)

    def add_rows(self, n):
        self.row_count += n

    def update(self, range_name=None, values=None, **kw):
        r, c = a1_to_rc(range_name)
        for dr, row in enumerate(values):
            for dc, v in enumerate(row):
                self._set(r + dr, c + dc, v)

    def update_cell(self, r, c, v):
        self._set(r, c, v)


class FakeSpreadsheet:
    def __init__(self, tabs):
        self.tabs = tabs
        self.deleted = []

    def worksheet(self, name):
        import gspread
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return self.tabs[name]

    def batch_update(self, body):
        for req in sorted(body["requests"], key=lambda r: -r["deleteDimension"]["range"]["startIndex"]):
            rng = req["deleteDimension"]["range"]
            ws = next(t for t in self.tabs.values() if t.id == rng["sheetId"])
            del ws.values[rng["startIndex"]:rng["endIndex"]]
            self.deleted.append((ws.title, rng["startIndex"] + 1))


def hub(queue_rows=(), tracker_rows=(), replies_rows=(), templates=None, settings=None):
    """A YT_Editing_Hub-shaped fake sheet."""
    import leadfinder as lf
    import emailer as em
    lists = [["Niche / Category", "Tracker Status", "Queue Status", "Template", "Hook Type", "Reply Type", "Yes / No",
              "", "", "", "Ramp: from day", "Daily send cap", "", "Setting", "Value"],
             ["Gaming", "", "Pending", "A", "None (generic)", "Reply", "Yes", "", "", "", "1", "15", "", "Subscriber min", "10,000"],
             ["", "", "", "", "", "", "No", "", "", "", "8", "20", "", "Subscriber max", "50,000"],
             ["", "", "", "", "", "", "", "", "", "", "15", "30", "", "Emailer paused", "No"],
             ["", "", "", "", "", "", "", "", "", "", "22", "40", "", "Sender name", "Ethan"],
             ["", "", "", "", "", "", "", "", "", "", "29", "50", "", "Check replies", "Yes"],
             ["", "", "", "", "", "", "", "", "", "", "", "", "", "Daily cap override", ""]]
    for k, v in (settings or {}).items():
        for row in lists:
            if row[13] == k:
                row[14] = v
    tpl = templates if templates is not None else [["A", "No", "[not written yet]", "[not written yet]", "None (generic)"]]
    return FakeSpreadsheet({
        "Business Queue": FakeWorksheet("Business Queue", [lf.DEFAULT_HEADER] + [list(r) for r in queue_rows], 101),
        "Outreach Tracker": FakeWorksheet("Outreach Tracker", [em.TRACKER_HEADER] + [list(r) for r in tracker_rows], 103),
        "Replies": FakeWorksheet("Replies", [em.REPLIES_HEADER] + [list(r) for r in replies_rows], 104),
        "Templates": FakeWorksheet("Templates", [["Template", "Ready?", "Subject", "Body", "Hook Type", "Notes"]] + tpl, 107),
        "Lists": FakeWorksheet("Lists", lists, 106),
    })
