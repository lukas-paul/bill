#!/usr/bin/env python3
"""Local web UI for make_bill.py.

    python3 app.py           # then open http://127.0.0.1:5000

The form collects the per-invoice details; sender / bank / currency / language
come from the chosen config file (config.json, config.de.json, ...). The PDF is
built by make_bill.build_pdf, so the CLI and the UI produce identical output.

Every generated PDF gets a sidecar <name>.json in out/ holding the client name,
date and totals. The "Existing invoices" list and the monthly summary / ZIP
export read those sidecars.
"""

import io
import re
import json
import zipfile
import datetime
from pathlib import Path

from flask import (
    Flask, request, render_template_string, send_file, send_from_directory,
    redirect, url_for, abort
)

import make_bill as mb

HERE = Path(__file__).parent
OUT_DIR = HERE / "out"
CLIENTS_PATH = HERE / "clients.json"
app = Flask(__name__)


def load_clients():
    if CLIENTS_PATH.is_file():
        try:
            data = json.loads(CLIENTS_PATH.read_text())
            if isinstance(data, list):
                return sorted(data, key=lambda c: c.get("name", "").lower())
        except json.JSONDecodeError:
            pass
    return []


def save_clients(clients):
    CLIENTS_PATH.write_text(json.dumps(clients, indent=2, ensure_ascii=False) + "\n")


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "client"


def add_client(name, address_lines):
    """Add or update a client (matched case-insensitively by name)."""
    name = name.strip()
    if not name:
        return None
    clients = load_clients()
    for c in clients:
        if c.get("name", "").strip().lower() == name.lower():
            c["address_lines"] = address_lines
            save_clients(clients)
            return c
    existing = {c.get("id") for c in clients}
    base = cid = _slug(name)
    n = 2
    while cid in existing:
        cid, n = f"{base}-{n}", n + 1
    client = {"id": cid, "name": name, "address_lines": address_lines}
    clients.append(client)
    save_clients(clients)
    return client


def delete_client(cid):
    save_clients([c for c in load_clients() if c.get("id") != cid])


def fmt_money(amount, currency="EUR", language="en"):
    return mb.make_money({"currency": currency, "language": language})(amount)


def load_meta(stem):
    p = OUT_DIR / f"{stem}.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
    return None


def used_numbers():
    if not OUT_DIR.exists():
        return set()
    nums = set()
    for p in OUT_DIR.glob("*.json"):
        m = load_meta(p.stem) or {}
        if m.get("number"):
            nums.add(str(m["number"]))
    return nums


def next_free_number(preferred):
    """A number close to `preferred` that isn't used yet."""
    used = used_numbers()
    preferred = (preferred or "").strip()
    if preferred and preferred not in used:
        return preferred
    m = re.search(r"(\d+)(\D*)$", preferred)
    if m:
        width, start = len(m.group(1)), int(m.group(1))
        head, tail = preferred[:m.start(1)], m.group(2)
        n = start + 1
        while f"{head}{n:0{width}d}{tail}" in used:
            n += 1
        return f"{head}{n:0{width}d}{tail}"
    n = 2
    while f"{preferred}-{n}" in used:
        n += 1
    return f"{preferred}-{n}"


def find_date_conflict(client_name, date, ignore_stem=None):
    """An existing invoice for the same client (case-insensitive) and date."""
    key = client_name.strip().lower()
    if not key or not date or not OUT_DIR.exists():
        return None
    matches = []
    for p in OUT_DIR.glob("*.json"):
        if p.stem == ignore_stem:
            continue
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if m.get("client_name", "").strip().lower() == key and m.get("date") == date:
            m["_pdf"] = f"{p.stem}.pdf"
            matches.append((p.stat().st_mtime, m))
    if not matches:
        return None
    matches.sort(reverse=True)  # newest first
    newest = matches[0][1]
    newest["_others"] = len(matches) - 1
    return newest


def list_bills():
    if not OUT_DIR.exists():
        return []
    today = datetime.date.today().isoformat()
    bills = []
    for p in OUT_DIR.glob("*.pdf"):
        st = p.stat()
        meta = load_meta(p.stem) or {}
        currency = meta.get("currency", "EUR")
        language = meta.get("language", "en")
        total = meta.get("total")
        due = meta.get("due")
        paid_on = meta.get("paid_on")
        if paid_on:
            status, status_label = "paid", "Paid"
        elif due and due < today:
            status, status_label = "overdue", "Overdue"
        else:
            status, status_label = "open", "Open"
        bills.append({
            "name": p.name,
            "stem": p.stem,
            "number": meta.get("number") or p.stem.replace("invoice-", ""),
            "client": meta.get("client_name") or "—",
            "date": meta.get("date") or datetime.date.fromtimestamp(st.st_mtime).isoformat(),
            "due": due,
            "paid_on": paid_on,
            "status": status,
            "status_label": status_label,
            "currency": currency,
            "language": language,
            "subtotal": meta.get("subtotal") or 0.0,
            "total": total,
            "total_fmt": fmt_money(total, currency, language) if total is not None else "—",
            "size_kb": max(1, round(st.st_size / 1024)),
            "has_meta": bool(meta),
            "mtime": st.st_mtime,
        })
    return sorted(bills, key=lambda b: (b["date"], b["mtime"]), reverse=True)


def month_summary(bills, month):
    """month is 'YYYY-MM'. Returns dict for the template, or None."""
    if not month:
        return None
    matched = [b for b in bills if b["date"].startswith(month)]
    buckets = {}
    unknown = 0
    for b in matched:
        if b["total"] is None:
            unknown += 1
            continue
        s = buckets.setdefault(b["currency"],
                               {"net": 0.0, "gross": 0.0, "outstanding": 0.0, "count": 0, "lang": "en"})
        s["net"] += b["subtotal"]
        s["gross"] += b["total"]
        s["count"] += 1
        s["lang"] = b.get("language", "en")
        if not b["paid_on"]:
            s["outstanding"] += b["total"]
    lines = [{
        "currency": cur,
        "count": s["count"],
        "net_fmt": fmt_money(s["net"], cur, s["lang"]),
        "gross_fmt": fmt_money(s["gross"], cur, s["lang"]),
        "outstanding_fmt": fmt_money(s["outstanding"], cur, s["lang"]) if s["outstanding"] > 0.005 else "",
    } for cur, s in sorted(buckets.items())]
    return {"month": month, "matched": len(matched), "unknown": unknown, "lines": lines}


def list_configs():
    out = []
    # config.json first, then the rest alphabetically; skip *.example.json templates
    paths = sorted(
        (p for p in HERE.glob("config*.json") if not p.name.endswith(".example.json")),
        key=lambda p: (p.name != "config.json", p.name),
    )
    for p in paths:
        try:
            cfg = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        out.append((p.name, cfg))
    return out


BASE_CSS = """
  :root {
    color-scheme: light dark;
    --bg: #f1eee5; --surface: #fffdf8; --field: #fffdf8;
    --ink: #23201a; --muted: #7d7669; --line: #e5dfd0;
    --accent: #1f6b4f; --accent-ink: #ffffff; --danger: #a63c2d;
    --shadow: 0 1px 2px rgba(35,28,15,.05), 0 16px 32px -20px rgba(35,28,15,.22);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15140f; --surface: #201e16; --field: #262318;
      --ink: #eae5d8; --muted: #9b9484; --line: #332f23;
      --accent: #59bd90; --accent-ink: #0a1811; --danger: #e5806f;
      --shadow: 0 1px 2px rgba(0,0,0,.35), 0 16px 32px -20px rgba(0,0,0,.7);
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; }
  a { color: var(--accent); }

  header { position: sticky; top: 0; z-index: 5; background: var(--surface);
    border-top: 3px solid var(--accent); border-bottom: 1px solid var(--line);
    padding: 16px 20px; }
  header .bar { max-width: 720px; margin: 0 auto; display: flex; align-items: baseline; gap: 14px; }
  .mark { display: inline-flex; align-items: center; gap: 9px; white-space: nowrap;
    font: 700 16px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .02em; }
  .mark::before { content: ""; width: 9px; height: 9px; border-radius: 2px; background: var(--accent); }
  .tag { font-size: 12px; color: var(--muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @media (max-width: 560px) { .tag { display: none; } }

  .wrap { max-width: 720px; margin: 0 auto; padding: 32px 20px 100px;
    display: flex; flex-direction: column; gap: 26px; }
  form { margin: 0; }

  fieldset { margin: 0; margin-bottom: 20px; border: 1px solid var(--line); border-radius: 14px;
    padding: 14px 24px 26px; background: var(--surface); box-shadow: var(--shadow); }
  legend { display: flex; align-items: center; gap: 9px; padding: 0 8px; margin: 0 0 0 -4px;
    font: 700 15px/1.2 -apple-system, system-ui, sans-serif; letter-spacing: -.01em;
    color: var(--ink); }
  legend::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }

  label { display: block; margin: 20px 0 6px; font-size: 12px; font-weight: 600;
    letter-spacing: .01em; color: var(--ink); }
  fieldset > label:first-of-type, fieldset > .row:first-of-type { margin-top: 14px; }
  .row { display: flex; gap: 18px; }
  .row > div { flex: 1; }
  .row label { margin-top: 0; }

  input, select, textarea { width: 100%; padding: 9px 11px; font: inherit; color: var(--ink);
    background: var(--field); border: 1px solid var(--line); border-radius: 9px;
    transition: border-color .15s, box-shadow .15s; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
  textarea { min-height: 70px; resize: vertical; }
  ::placeholder { color: color-mix(in srgb, var(--muted) 70%, transparent); }

  .btn { display: inline-flex; align-items: center; gap: 6px; font: inherit; font-weight: 600;
    padding: 10px 17px; border: 1px solid var(--line); border-radius: 10px;
    background: var(--surface); color: var(--ink); cursor: pointer; text-decoration: none;
    transition: border-color .15s, color .15s, filter .15s; }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
  .btn.primary:hover { filter: brightness(1.07); color: var(--accent-ink); }
  .btn.danger { color: var(--danger); }
  .btn.danger:hover { border-color: var(--danger); color: var(--danger); }

  .actions { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-top: 12px; }
  .actions .hint { margin: 0; }
  .hint { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
  code { font: 12px/1 ui-monospace, Menlo, monospace; background: var(--bg);
    border: 1px solid var(--line); border-radius: 5px; padding: 1px 5px; }

  .item { position: relative; display: grid; gap: 12px 14px;
    grid-template-columns: 1fr 90px 118px 118px 30px; align-items: end;
    padding: 16px; margin-bottom: 14px; background: var(--bg);
    border: 1px solid var(--line); border-radius: 11px; }
  .item label { margin: 0 0 4px; font-size: 10px; letter-spacing: .07em;
    text-transform: uppercase; color: var(--muted); }
  .item .del { align-self: end; width: 28px; height: 28px; padding: 0; font-size: 15px;
    line-height: 1; border: 1px solid var(--line); border-radius: 8px;
    background: var(--surface); color: var(--danger); cursor: pointer; }
  .item .del:hover { border-color: var(--danger); }
  .item-amount { grid-column: 1 / -1; margin-top: 3px; padding-top: 10px;
    border-top: 1px solid var(--line);
    text-align: right; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  @media (max-width: 600px) {
    .item { grid-template-columns: 1fr 1fr; }
    .item .del { grid-column: 2; justify-self: end; }
  }

  .panel { margin-top: 18px; padding: 15px 17px; background: var(--bg);
    border: 1px solid var(--line); border-radius: 11px; line-height: 1.8;
    font-variant-numeric: tabular-nums; }
  .totals { text-align: right; }
  .totals .grand { margin-top: 6px; font-size: 17px; font-weight: 700; color: var(--accent); }
  .summary a.btn { margin-top: 14px; }
  input[type="month"] { max-width: 220px; }

  .check { display: flex; align-items: center; gap: 10px; margin-top: 20px; padding: 12px 14px;
    font-size: 13px; font-weight: 500; background: var(--bg); border: 1px solid var(--line);
    border-radius: 9px; }
  .check input { width: auto; }

  .bills { list-style: none; margin: 12px 0 0; padding: 0; }
  .bills li { display: flex; align-items: baseline; gap: 12px; padding: 13px 6px;
    border-bottom: 1px solid var(--line); }
  .bills li:last-child { border-bottom: 0; }
  .bills li:hover { background: var(--bg); }
  .bills .who { max-width: 52%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-weight: 600; color: var(--ink); text-decoration: none; }
  a.who:hover { color: var(--accent); }
  .bills .amt { color: var(--ink); font-variant-numeric: tabular-nums; }
  .bills .hint { margin: 0; }
  .bills .dl { margin-left: auto; white-space: nowrap; }
  .linkbtn { padding: 0; border: none; background: none; font: inherit; color: var(--danger); cursor: pointer; }
  .linkbtn:hover { text-decoration: underline; }
  .linkbtn.toggle { color: var(--accent); }

  .invoices { list-style: none; margin: 12px 0 0; padding: 0; }
  .invoices li { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 5px 14px;
    padding: 14px 6px; border-bottom: 1px solid var(--line); }
  .invoices li:last-child { border-bottom: 0; }
  .invoices li:hover { background: var(--bg); }
  .invoices li.overdue { box-shadow: inset 3px 0 0 var(--danger); padding-left: 12px; }
  .invoices li.paid .who { color: var(--muted); }
  .invoices .r1 { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .invoices .who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-weight: 600; color: var(--ink); text-decoration: none; }
  .invoices .amt { text-align: right; font-weight: 600; font-variant-numeric: tabular-nums;
    white-space: nowrap; }
  .invoices .meta { font-size: 12px; color: var(--muted); }
  .invoices .acts { display: flex; gap: 12px; align-items: baseline; justify-content: flex-end;
    white-space: nowrap; }
  .invoices .acts form { display: inline; }

  .badge { flex: none; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent;
    font-size: 10px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; }
  .badge.open { color: var(--muted); background: var(--surface); border-color: var(--line); }
  .badge.paid { color: var(--accent);
    background: color-mix(in srgb, var(--accent) 13%, transparent);
    border-color: color-mix(in srgb, var(--accent) 32%, transparent); }
  .badge.overdue { color: var(--danger);
    background: color-mix(in srgb, var(--danger) 13%, transparent);
    border-color: color-mix(in srgb, var(--danger) 32%, transparent); }

  .cardwrap { max-width: 520px; margin: 72px auto; padding: 0 20px; }
  .card { background: var(--surface); border: 1px solid var(--line);
    border-top: 3px solid var(--accent); border-radius: 14px; padding: 30px 32px;
    box-shadow: var(--shadow); }
  .card h1 { margin: 0 0 18px; font-size: 20px; font-weight: 700; letter-spacing: -.01em;
    line-height: 1.3; }
  .card .row { display: block; margin: 16px 0; line-height: 1.6; }
  .card .btns { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 26px; }
"""


def _head(title):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>{BASE_CSS}</style></head>'
    )


PAGE = _head("bill") + """
<body>
<header>
  <div class="bar">
    <span class="mark">bill</span>
    <span class="tag">fill in the fields, get a PDF invoice &nbsp;&middot;&nbsp; sender &amp; bank come from the config file</span>
  </div>
</header>
<div class="wrap">

<form method="post" action="/generate" id="f">
  <fieldset>
    <legend>Template</legend>
    <label for="config">Config file</label>
    <select name="config" id="config" onchange="location.search='?config='+encodeURIComponent(this.value)">
      {% for name, c in configs %}
        <option value="{{ name }}" {{ 'selected' if name == selected }}>
          {{ name }} — {{ c.get('language','en')|upper }} · {{ c.sender.name }}
        </option>
      {% endfor %}
    </select>
    <p class="hint">{{ cfg.sender.name }}, {{ cfg.sender.address_lines|join(', ') }} ·
       {{ cfg.currency }} · default VAT {{ cfg.get('tax_rate', 0) }}%</p>
  </fieldset>

  <fieldset>
    <legend>Invoice</legend>
    <div class="row">
      <div><label for="number">Number</label>
        <input name="number" id="number" value="{{ suggest_number }}" required></div>
      <div><label for="date">Date</label>
        <input name="date" id="date" type="date" value="{{ today }}" required></div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Client</legend>
    {% if clients %}
      <label for="client_picker">Use existing</label>
      <select id="client_picker" onchange="fillClient(this.value)">
        <option value="">— new client —</option>
        {% for c in clients %}<option value="{{ c.id }}">{{ c.name }}</option>{% endfor %}
      </select>
    {% endif %}
    <label for="client_name">Name</label>
    <input name="client_name" id="client_name" required>
    <label for="client_address">Address (one line per row)</label>
    <textarea name="client_address" id="client_address"></textarea>
    <label class="check"><input type="checkbox" name="save_client" value="1">
      Save / update this client for next time</label>
  </fieldset>

  <fieldset>
    <legend>Line items</legend>
    <div id="items"></div>
    <button type="button" class="btn" onclick="addItem()">+ Add item</button>
    <div class="panel totals" id="totals" hidden></div>
  </fieldset>

  <fieldset>
    <legend>Tax &amp; notes</legend>
    <div class="row">
      <div><label for="tax_rate">VAT rate %</label>
        <input name="tax_rate" id="tax_rate" type="number" step="0.1" min="0"
               value="{{ cfg.get('tax_rate', 0) }}"></div>
    </div>
    <label for="notes">Notes (blank = use config default: “{{ cfg.get('notes','') }}”)</label>
    <textarea name="notes" id="notes"></textarea>
  </fieldset>

  <div class="actions">
    <button type="submit" class="btn primary">Generate PDF</button>
    <span class="hint">Also saved to <code>out/</code></span>
  </div>
</form>

<form method="post" action="/clients">
  <input type="hidden" name="config" value="{{ selected }}">
  <fieldset>
    <legend>Clients ({{ clients|length }})</legend>
    {% if clients %}
      <ul class="bills">
        {% for c in clients %}
          <li>
            <span class="who">{{ c.name }}</span>
            <span class="hint">{{ c.address_lines|join(' · ') }}</span>
            <button class="linkbtn dl" type="submit" formnovalidate
                    formaction="/clients/{{ c.id }}/delete">delete</button>
          </li>
        {% endfor %}
      </ul>
    {% endif %}
    <label for="new_client_name">Add a client</label>
    <input name="client_name" id="new_client_name" placeholder="Client name" required>
    <textarea name="client_address" placeholder="Address, one line per row"></textarea>
    <button class="btn" type="submit" style="margin-top:10px">Add client</button>
  </fieldset>
</form>

<form method="get" action="/">
  <input type="hidden" name="config" value="{{ selected }}">
  <fieldset>
    <legend>By month</legend>
    <label for="month">Month</label>
    <input type="month" name="month" id="month" value="{{ month }}" onchange="this.form.submit()">
    {% if summary %}
      <div class="panel summary">
        <strong>{{ summary.matched }}</strong> invoice(s) dated {{ summary.month }}{% if summary.unknown %} —
          {{ summary.unknown }} without metadata, not counted in totals{% endif %}.
        {% for l in summary.lines %}
          <br>{{ l.currency }}: net <strong>{{ l.net_fmt }}</strong> ·
              gross {{ l.gross_fmt }}
              {% if l.outstanding_fmt %} · <span style="color:var(--danger)">{{ l.outstanding_fmt }} outstanding</span>{% endif %}
              <span class="hint">({{ l.count }} invoice(s))</span>
        {% endfor %}
        {% if not summary.lines %}<br><span class="hint">No earnings data for this month.</span>{% endif %}
        {% if summary.matched %}
          <br><a class="btn" style="display:inline-block;margin-top:10px"
                 href="/month.zip?month={{ summary.month }}">Download {{ summary.matched }} PDF(s) as ZIP</a>
        {% endif %}
      </div>
    {% endif %}
  </fieldset>
</form>

<div>
  <fieldset>
    <legend>Existing invoices ({{ bills|length }})</legend>
    {% if not bills %}
      <p class="hint">Nothing in <code>out/</code> yet.</p>
    {% else %}
      <ul class="invoices">
        {% for b in bills %}
          <li class="{{ b.status }}">
            <div class="r1">
              <a class="who" href="/pdf/{{ b.name }}" target="_blank">{{ b.client }}</a>
              <span class="badge {{ b.status }}">{{ b.status_label }}</span>
            </div>
            <div class="amt">{{ b.total_fmt }}</div>
            <div class="meta">{{ b.number }} · {{ b.date }}{% if b.due %} · due {{ b.due }}{% endif %}{% if b.paid_on %} · paid {{ b.paid_on }}{% endif %}</div>
            <div class="acts">
              <a class="hint" href="/pdf/{{ b.name }}?download=1">download</a>
              <form method="post" action="/invoice/{{ b.stem }}/toggle-paid">
                <input type="hidden" name="config" value="{{ selected }}">
                <input type="hidden" name="month" value="{{ month }}">
                <button class="linkbtn toggle" type="submit">{{ 'Mark unpaid' if b.paid_on else 'Mark paid' }}</button>
              </form>
            </div>
          </li>
        {% endfor %}
      </ul>
    {% endif %}
  </fieldset>
</div>

</div>

<template id="item-tpl">
  <div class="item">
    <div><label>Description</label><input name="item_desc" required></div>
    <div><label>Type</label>
      <select name="item_type" onchange="recalc()">
        <option value="fixed">fixed</option>
        <option value="hourly">hourly</option>
      </select></div>
    <div><label class="qty-label">Quantity</label>
      <input name="item_qty" type="number" step="0.01" min="0" value="1" oninput="recalc()"></div>
    <div><label class="price-label">Unit price</label>
      <input name="item_price" type="number" step="0.01" min="0" value="0" oninput="recalc()"></div>
    <button type="button" class="del" title="Remove" onclick="this.closest('.item').remove(); recalc()">×</button>
    <div class="item-amount" style="grid-column: 1 / -1"></div>
  </div>
</template>

<script>
const currency = {{ cfg.currency|tojson }};
const german = {{ (cfg.get('language','en') == 'de')|tojson }};
const clients = {{ clients|tojson }};

function fillClient(id) {
  const c = clients.find(x => x.id === id);
  if (!c) return;
  document.getElementById('client_name').value = c.name;
  document.getElementById('client_address').value = (c.address_lines || []).join('\\n');
}

function fmt(n) {
  let s = n.toLocaleString(german ? 'de-DE' : 'en-US',
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return s + ' ' + (currency === 'EUR' ? '€' : currency);
}

function addItem() {
  const node = document.getElementById('item-tpl').content.cloneNode(true);
  document.getElementById('items').appendChild(node);
  recalc();
}

function recalc() {
  let subtotal = 0;
  document.querySelectorAll('.item').forEach(row => {
    const type = row.querySelector('[name=item_type]').value;
    const qty = parseFloat(row.querySelector('[name=item_qty]').value) || 0;
    const price = parseFloat(row.querySelector('[name=item_price]').value) || 0;
    row.querySelector('.qty-label').textContent = type === 'hourly' ? 'Hours' : 'Quantity';
    row.querySelector('.price-label').textContent = type === 'hourly' ? 'Hourly rate' : 'Unit price';
    const amount = qty * price;
    subtotal += amount;
    row.querySelector('.item-amount').textContent = fmt(amount);
  });
  const rate = parseFloat(document.getElementById('tax_rate').value) || 0;
  const tax = subtotal * rate / 100;
  const t = document.getElementById('totals');
  t.hidden = document.querySelectorAll('.item').length === 0;
  t.innerHTML = 'Subtotal ' + fmt(subtotal) +
    (rate ? '<br>VAT ' + rate + '% ' + fmt(tax) : '') +
    '<div class="grand">Total ' + fmt(subtotal + tax) + '</div>';
}
document.getElementById('tax_rate').addEventListener('input', recalc);
addItem();
</script>
</body>
</html>
"""


CONFIRM_PAGE = _head("Duplicate invoice?") + """
<body>
<div class="cardwrap"><div class="card">
  <h1>There's already an invoice for this client and date</h1>
  <div class="row">
    <strong>{{ conflict.client_name }}</strong> &middot; {{ conflict.date }}<br>
    Existing: <strong>{{ conflict.number }}</strong>{% if conflict_total %} &middot; {{ conflict_total }}{% endif %}
    &nbsp;<a href="/pdf/{{ conflict._pdf }}" target="_blank">view</a>
    {% if conflict._others %}<br><span style="color:#888">(+{{ conflict._others }} more invoice(s)
      for this client and date; “Overwrite” replaces the most recent one, {{ conflict.number }}.)</span>{% endif %}
  </div>
  <div class="row">You were about to create invoice <strong>{{ entered_number }}</strong>.
    What would you like to do?</div>

  <form method="post" action="/generate">
    {% for key, values in form.lists() %}
      {% if key != 'resolve' %}
        {% for v in values %}<input type="hidden" name="{{ key }}" value="{{ v }}">{% endfor %}
      {% endif %}
    {% endfor %}
    <div class="btns">
      <button type="submit" name="resolve" value="overwrite" class="btn danger">
        Overwrite {{ conflict.number }}</button>
      <button type="submit" name="resolve" value="new" class="btn">
        Create {{ entered_number }} as a separate invoice</button>
      <a class="btn" href="/">Cancel</a>
    </div>
  </form>
</div></div>
</body>
</html>
"""


NUMBER_PAGE = _head("Invoice number in use") + """
<body>
<div class="cardwrap"><div class="card">
  <h1>Invoice number {{ entered }} is already in use</h1>
  <div class="row">
    It belongs to <strong>{{ existing.client_name }}</strong> &middot; {{ existing.date }}
    &nbsp;<a href="/pdf/{{ existing._pdf }}" target="_blank">view</a>
  </div>
  <div class="row">Every invoice needs its own number.
    The next free one is <strong>{{ suggestion }}</strong>.</div>

  <form method="post" action="/generate">
    {% for key, values in form.lists() %}
      {% if key not in ('number', 'resolve') %}
        {% for v in values %}<input type="hidden" name="{{ key }}" value="{{ v }}">{% endfor %}
      {% endif %}
    {% endfor %}
    <input type="hidden" name="number" value="{{ suggestion }}">
    <input type="hidden" name="resolve" value="new">
    <div class="btns">
      <button type="submit" class="btn primary">Create as {{ suggestion }}</button>
      <a class="btn" href="/">Cancel</a>
    </div>
  </form>
</div></div>
</body>
</html>
"""


def current_config_name():
    names = [n for n, _ in list_configs()]
    if not names:
        abort(500, "No config file. Copy config.example.json to config.json and edit it.")
    want = request.args.get("config") or request.form.get("config")
    return want if want in names else names[0]


@app.route("/")
def index():
    configs = list_configs()
    selected = current_config_name()
    cfg = dict(configs)[selected]
    bills = list_bills()
    month = request.args.get("month", "")
    return render_template_string(
        PAGE,
        configs=configs,
        selected=selected,
        cfg=cfg,
        today=datetime.date.today().isoformat(),
        suggest_number=mb._suggest_number(),
        bills=bills,
        month=month,
        summary=month_summary(bills, month),
        clients=load_clients(),
    )


@app.route("/clients", methods=["POST"])
def clients_create():
    add_client(
        request.form.get("client_name", ""),
        [ln.strip() for ln in request.form.get("client_address", "").splitlines() if ln.strip()],
    )
    return redirect(url_for("index", config=request.form.get("config", "")))


@app.route("/clients/<cid>/delete", methods=["POST"])
def clients_delete(cid):
    delete_client(cid)
    return redirect(url_for("index", config=request.form.get("config", "")))


@app.route("/pdf/<name>")
def pdf(name):
    if not name.endswith(".pdf") or "/" in name or "\\" in name:
        abort(404)
    if not (OUT_DIR / name).is_file():
        abort(404)
    return send_from_directory(
        OUT_DIR, name, mimetype="application/pdf",
        as_attachment=bool(request.args.get("download")),
    )


@app.route("/invoice/<stem>/toggle-paid", methods=["POST"])
def toggle_paid(stem):
    if "/" in stem or "\\" in stem or ".." in stem:
        abort(404)
    p = OUT_DIR / f"{stem}.json"
    if not p.is_file():
        abort(404)
    meta = load_meta(stem) or {}
    if meta.get("paid_on"):
        meta.pop("paid_on", None)
    else:
        meta["paid_on"] = datetime.date.today().isoformat()
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return redirect(url_for(
        "index",
        config=request.form.get("config", ""),
        month=request.form.get("month", ""),
    ))


@app.route("/month.zip")
def month_zip():
    month = request.args.get("month", "")
    bills = [b for b in list_bills() if month and b["date"].startswith(month)]
    if not bills:
        abort(404, "No invoices for that month.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for b in bills:
            z.write(OUT_DIR / b["name"], b["name"])
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"invoices-{month}.zip")


@app.route("/generate", methods=["POST"])
def generate():
    f = request.form
    config_path = HERE / current_config_name()
    config = mb.load_config(config_path)

    items = []
    for desc, typ, qty, price in zip(
        f.getlist("item_desc"), f.getlist("item_type"),
        f.getlist("item_qty"), f.getlist("item_price"),
    ):
        desc = desc.strip()
        if not desc:
            continue
        qty = float(qty or 0)
        price = float(price or 0)
        if typ == "hourly":
            items.append({"description": desc, "hours": qty, "hourly_rate": price})
        else:
            items.append({"description": desc, "quantity": qty, "unit_price": price})

    if not items:
        abort(400, "Add at least one line item.")

    client_name = f["client_name"].strip()
    client_address = [ln.strip() for ln in f["client_address"].splitlines() if ln.strip()]
    entered_number = f["number"].strip()
    date = f["date"].strip()
    resolve = f.get("resolve", "")

    # Warn about an existing invoice for the same client + date, unless the
    # user already chose how to handle it, or this is plainly the same invoice
    # being re-generated (same number too).
    if not resolve:
        conflict = find_date_conflict(client_name, date)
        if conflict and str(conflict.get("number")) != entered_number:
            return render_template_string(
                CONFIRM_PAGE,
                conflict=conflict,
                conflict_total=fmt_money(
                    conflict["total"], conflict.get("currency", "EUR"),
                    conflict.get("language", "en"),
                ) if conflict.get("total") is not None else "",
                entered_number=entered_number,
                form=f,
            )

    number = entered_number
    if resolve == "overwrite":
        conflict = find_date_conflict(client_name, date)
        if conflict:
            number = conflict["number"]

    if not number:
        abort(400, "An invoice number is required.")

    # Enforce unique invoice numbers. Overwriting is only allowed when it is
    # genuinely the same invoice (same number + client + date), or when the
    # user explicitly chose "overwrite" on the duplicate-date screen.
    safe = number.replace("/", "-").replace(" ", "")
    target_stem = f"invoice-{safe}"
    existing = load_meta(target_stem)
    if existing is not None:
        same_invoice = (
            existing.get("client_name", "").strip().lower() == client_name.lower()
            and existing.get("date") == date
        )
        allowed = same_invoice and resolve != "new"
        if not allowed:
            existing["_pdf"] = f"{target_stem}.pdf"
            return render_template_string(
                NUMBER_PAGE,
                existing=existing,
                entered=number,
                suggestion=next_free_number(number),
                form=f,
            )

    if f.get("save_client"):
        add_client(client_name, client_address)

    data = {
        "number": number,
        "date": date,
        "client": {
            "name": client_name,
            "address_lines": client_address,
        },
        "items": items,
        "tax_rate": float(f.get("tax_rate") or 0),
        "notes": f.get("notes", "").strip() or config.get("notes", ""),
    }

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"invoice-{safe}.pdf"
    mb.build_pdf(data, config, out_path)

    try:
        due = (datetime.date.fromisoformat(data["date"])
               + datetime.timedelta(days=int(config.get("payment_terms_days", 14)))).isoformat()
    except ValueError:
        due = None

    subtotal, tax, total = mb.invoice_totals(data)
    meta_path = out_path.with_suffix(".json")
    prev = load_meta(out_path.stem) or {}
    meta = {
        "number": data["number"],
        "date": data["date"],
        "due": due,
        "client_name": data["client"]["name"],
        "currency": config.get("currency", "EUR"),
        "language": config.get("language", "en"),
        "subtotal": round(subtotal, 2),
        "tax_rate": data["tax_rate"],
        "tax": round(tax, 2),
        "total": round(total, 2),
    }
    if prev.get("paid_on"):  # keep paid status when re-generating the same invoice
        meta["paid_on"] = prev["paid_on"]
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    return send_file(
        io.BytesIO(out_path.read_bytes()),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=out_path.name,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
