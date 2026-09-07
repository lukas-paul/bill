# bill

Small script that turns a few pieces of information into a clean PDF invoice,
in English or German.

## Setup (once)

```
python3 -m pip install --user reportlab flask
```
(`flask` is only needed for the web UI.)

Then create your config from a template and fill in your name, address,
tax ID, bank details, currency, default VAT rate and payment terms:

```
cp config.example.json config.json          # English invoices
cp config.de.example.json config.de.json    # German invoices (optional)
```

The real config files hold your IBAN etc. and are git-ignored; only the
`*.example.json` templates are tracked. You need at least one (`config.json`
or `config.de.json`) for the app to start.

- `config.json`    – `"language": "en"`
- `config.de.json` – `"language": "de"`: "RECHNUNG", German labels,
  `1.234,56 €` number format, `TT.MM.JJJJ` dates, and an optional
  `small_business_note` (§ 19 UStG) shown when the tax rate is 0.

`clients.json`, `invoice.json` and `out/` are also git-ignored and created
automatically as you use the app.

## Web UI

```
python3 app.py
```

Open http://127.0.0.1:5000. Pick a config from the dropdown (sets language,
sender, bank, currency), fill in the client and line items (each row is
`fixed` or `hourly`), watch the live total, hit **Generate PDF**. The PDF
downloads and is also written to `out/`. Same `build_pdf()` as the CLI, so
output is identical.

**Existing invoices** at the bottom lists every PDF in `out/` (newest first)
with client name, number, date and total — click the client to view the PDF,
"download" to save it. Reload after generating to see a new one.

Each row carries a status badge: **Paid**, **Overdue** (unpaid and past its
due date), or **Open**. "Mark paid" / "Mark unpaid" toggles it; the paid date
is stored in the sidecar and survives re-generating the invoice.

**By month**: pick a month to see how many invoices are dated in it, the
net (subtotal) and gross (invoiced) earnings per currency, how much is still
**outstanding** (unpaid), and a button to download all of that month's PDFs
as one ZIP.

### Duplicate check

Two guards before an invoice is written:

- **Same client + date**: if one already exists (and you typed a *different*
  number), the UI asks — **Overwrite** it, or **create a separate invoice**.
- **Number already in use**: invoice numbers must be unique. If the number you
  typed belongs to another invoice, the UI blocks and offers the next free
  number (e.g. `2026-001` → `2026-002`) in one click.

Re-generating the *same* invoice — same number, client and date — just
overwrites it silently (that's how you fix a typo). The CLI asks
`Overwrite? [y/N]` if the file already exists.

### Clients

`clients.json` stores repeat customers (name + address). In the web UI:

- **Client** section has a "Use existing" dropdown that autofills name and
  address, and a "Save / update this client" checkbox to store whatever you
  typed when you generate the invoice.
- **Clients** section lists saved clients, lets you add one directly, or
  delete one.

It's a plain JSON file — you can also edit it by hand.

### Sidecar metadata

Each generated PDF gets a `out/<name>.json` next to it with the client name,
date, due date, totals and (once marked) `paid_on`. The list and the monthly
summary read these. PDFs created before this feature (or added by hand) still
show up, but without a client name or total and they're not counted in the
monthly earnings.

## CLI

Interactive – answer the prompts:

```
python3 make_bill.py                        # uses config.json (English)
python3 make_bill.py --config config.de.json   # German
```

From a file (good for repeat clients / keeping a record):

```
python3 make_bill.py --sample                       # writes invoice.json
python3 make_bill.py invoice.json                   # English
python3 make_bill.py --config config.de.json invoice.json   # German
```

PDFs land in `out/invoice-<number>.pdf`.

## invoice.json shape

```json
{
  "number": "2026-001",
  "date": "2026-09-07",
  "client": { "name": "Client GmbH", "address_lines": ["Musterstr. 5", "10115 Berlin"] },
  "items": [
    { "description": "Consulting, June 2026", "hours": 12, "hourly_rate": 95.0 },
    { "description": "Design work",            "quantity": 1, "unit_price": 450.0 }
  ],
  "tax_rate": 19,
  "notes": "Payment within 14 days."
}
```

Each line item is either:

- **time-based** – `"hours"` + `"hourly_rate"` (the Qty column shows `12 h` / `12 Std.`)
- **fixed** – `"quantity"` + `"unit_price"`

`tax_rate` and `notes` are optional and fall back to the config file.
In interactive mode you pick `[f]ixed` or `[h]ourly` per item.
