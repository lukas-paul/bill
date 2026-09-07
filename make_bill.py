#!/usr/bin/env python3
"""Create a PDF invoice/bill from a few pieces of information.

Usage:
    python3 make_bill.py                        # interactive prompts
    python3 make_bill.py invoice.json           # read everything from a JSON file
    python3 make_bill.py --config config.de.json invoice.json
    python3 make_bill.py --sample               # write a sample invoice.json and exit

Sender details, bank details, currency, tax rate, payment terms and the
output language are read from a config file (config.json by default, or one
passed with --config). Everything invoice-specific is asked for or read from
the JSON file.

Line items can be either fixed-price or time-based:
    {"description": "Design work",  "quantity": 1, "unit_price": 450.0}
    {"description": "Consulting",    "hours": 12,   "hourly_rate": 95.0}
"""

import json
import sys
import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"

CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}

LABELS = {
    "en": {
        "invoice": "INVOICE", "bill_to": "BILL TO", "details": "DETAILS",
        "invoice_no": "Invoice no.", "date": "Date", "due": "Due",
        "description": "Description", "qty": "Qty", "unit_price": "Unit price",
        "amount": "Amount", "subtotal": "Subtotal", "vat": "VAT",
        "total": "Total", "payment": "PAYMENT", "bank": "Bank",
        "iban": "IBAN", "bic": "BIC", "reference": "Reference",
        "hours_unit": "h",
    },
    "de": {
        "invoice": "RECHNUNG", "bill_to": "RECHNUNG AN", "details": "ANGABEN",
        "invoice_no": "Rechnungsnr.", "date": "Rechnungsdatum", "due": "Fällig am",
        "description": "Beschreibung", "qty": "Menge", "unit_price": "Einzelpreis",
        "amount": "Betrag", "subtotal": "Zwischensumme", "vat": "USt.",
        "total": "Gesamtbetrag", "payment": "ZAHLUNG", "bank": "Bank",
        "iban": "IBAN", "bic": "BIC", "reference": "Verwendungszweck",
        "hours_unit": "Std.",
    },
}


# --------------------------------------------------------------------------- #
# Config / input
# --------------------------------------------------------------------------- #
def load_config(path):
    if not path.exists():
        sys.exit(f"Missing {path}. Copy a template and fill it in.")
    cfg = json.loads(path.read_text())
    cfg.setdefault("language", "en")
    if cfg["language"] not in LABELS:
        sys.exit(f"Unknown language '{cfg['language']}' (use 'en' or 'de').")
    return cfg


def ask(prompt, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default if default is not None else "")


def _num(s):
    return float(s.replace(".", "").replace(",", ".")) if s.count(",") == 1 else float(s.replace(",", ""))


def gather_interactive(config):
    print("\n--- New invoice ---\n")
    client_name = ask("Client name")
    print("Client address (one line at a time, empty line to finish):")
    client_address = []
    while True:
        line = input("  ").strip()
        if not line:
            break
        client_address.append(line)

    number = ask("Invoice number", default=_suggest_number())
    date = ask("Invoice date (YYYY-MM-DD)", default=datetime.date.today().isoformat())

    print("\nLine items (empty description to finish):")
    items = []
    while True:
        desc = input("  Description: ").strip()
        if not desc:
            break
        kind = ask("  Billing: [f]ixed price or [h]ourly", default="f").lower()
        if kind.startswith("h"):
            hours = _num(ask("  Hours worked"))
            rate = _num(ask("  Hourly rate"))
            items.append({"description": desc, "hours": hours, "hourly_rate": rate})
        else:
            qty = _num(ask("  Quantity", default="1"))
            price = _num(ask("  Unit price"))
            items.append({"description": desc, "quantity": qty, "unit_price": price})
        print()

    tax_rate = _num(ask("Tax rate %", default=str(config.get("tax_rate", 0))))
    notes = ask("Notes", default=config.get("notes", ""))

    return {
        "number": number,
        "date": date,
        "client": {"name": client_name, "address_lines": client_address},
        "items": items,
        "tax_rate": tax_rate,
        "notes": notes,
    }


def _suggest_number():
    return f"{datetime.date.today():%Y}-001"


def _sample_invoice():
    return {
        "number": _suggest_number(),
        "date": datetime.date.today().isoformat(),
        "client": {
            "name": "Client GmbH",
            "address_lines": ["Musterstr. 5", "10115 Berlin", "Germany"],
        },
        "items": [
            {"description": "Consulting, June 2026", "hours": 12, "hourly_rate": 95.0},
            {"description": "Design work", "quantity": 1, "unit_price": 450.0},
        ],
    }


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def make_money(config):
    symbol = CURRENCY_SYMBOLS.get(config.get("currency", "EUR"), config.get("currency", "EUR"))
    german = config["language"] == "de"

    def money(amount):
        s = f"{amount:,.2f}"  # 1,234.56
        if german:
            s = s.translate(str.maketrans({",": "\0", ".": ","})).replace("\0", ".")
        return f"{s} {symbol}"  # symbol trails; NBSP keeps it attached

    return money


def fmt_qty(qty):
    return f"{qty:g}".replace(".", ",") if False else f"{qty:g}"


def fmt_date(iso, config):
    try:
        d = datetime.date.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    return d.strftime("%d.%m.%Y") if config["language"] == "de" else d.isoformat()


def normalize_item(it, labels):
    """Return (description, qty, unit_price, qty_suffix)."""
    if "hours" in it or "hourly_rate" in it:
        return (it["description"], float(it.get("hours", 0)),
                float(it.get("hourly_rate", 0)), " " + labels["hours_unit"])
    return (it["description"], float(it.get("quantity", 1)),
            float(it.get("unit_price", 0)), "")


def invoice_totals(data):
    """(subtotal, tax, total) for an invoice dict; tax_rate is a percentage."""
    subtotal = 0.0
    for it in data.get("items", []):
        if "hours" in it or "hourly_rate" in it:
            subtotal += float(it.get("hours", 0)) * float(it.get("hourly_rate", 0))
        else:
            subtotal += float(it.get("quantity", 1)) * float(it.get("unit_price", 0))
    tax = subtotal * float(data.get("tax_rate", 0)) / 100.0
    return subtotal, tax, subtotal + tax


# --------------------------------------------------------------------------- #
# PDF building
# --------------------------------------------------------------------------- #
def build_pdf(data, config, out_path):
    L = LABELS[config["language"]]
    money = make_money(config)
    sender = config["sender"]
    payment = config.get("payment", {})

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "Helvetica"
    normal.fontSize = 9.5
    normal.leading = 13
    small = ParagraphStyle("small", parent=normal, fontSize=8, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=normal, fontSize=22, leading=26, fontName="Helvetica-Bold")
    label = ParagraphStyle("label", parent=small, fontName="Helvetica-Bold", textColor=colors.HexColor("#888888"))

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=f"{L['invoice'].title()} {data['number']}",
    )
    story = []

    # Header
    story.append(Paragraph(L["invoice"], h1))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(sender["name"], normal))
    for line in sender.get("address_lines", []):
        story.append(Paragraph(line, small))
    contact = " · ".join(
        v for v in [sender.get("email"), sender.get("phone"), sender.get("tax_id")] if v
    )
    if contact:
        story.append(Paragraph(contact, small))
    story.append(Spacer(1, 8 * mm))

    # Bill-to + meta, side by side
    client = data["client"]
    bill_to = [Paragraph(L["bill_to"], label), Paragraph(client["name"], normal)]
    for line in client.get("address_lines", []):
        bill_to.append(Paragraph(line, small))

    issue_date = data["date"]
    try:
        due = (datetime.date.fromisoformat(issue_date)
               + datetime.timedelta(days=int(config.get("payment_terms_days", 14))))
        due = fmt_date(due.isoformat(), config)
    except (ValueError, TypeError):
        due = "-"
    meta = [
        Paragraph(L["details"], label),
        Paragraph(f"{L['invoice_no']}&nbsp; {data['number']}", normal),
        Paragraph(f"{L['date']}&nbsp; {fmt_date(issue_date, config)}", normal),
        Paragraph(f"{L['due']}&nbsp; {due}", normal),
    ]
    head = Table([[bill_to, meta]], colWidths=[95 * mm, 75 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(head)
    story.append(Spacer(1, 10 * mm))

    # Line items
    rows = [[L["description"], L["qty"], L["unit_price"], L["amount"]]]
    subtotal = 0.0
    for it in data["items"]:
        desc, qty, price, suffix = normalize_item(it, L)
        amount = qty * price
        subtotal += amount
        rows.append([
            Paragraph(desc, normal),
            f"{fmt_qty(qty)}{suffix}",
            money(price),
            money(amount),
        ])

    tax_rate = float(data.get("tax_rate", 0))
    tax = subtotal * tax_rate / 100.0
    total = subtotal + tax

    vat_label = f"{L['vat']} {tax_rate:g} %" if config["language"] == "de" else f"{L['vat']} {tax_rate:g}%"
    rows.append(["", "", L["subtotal"], money(subtotal)])
    if tax_rate:
        rows.append(["", "", vat_label, money(tax)])
    rows.append(["", "", L["total"], money(total)])

    n_items = len(data["items"])
    table = Table(rows, colWidths=[92 * mm, 18 * mm, 30 * mm, 30 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#888888")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#333333")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, n_items), 0.25, colors.HexColor("#dddddd")),
        ("LINEABOVE", (2, n_items + 1), (-1, n_items + 1), 0.5, colors.HexColor("#333333")),
        ("FONT", (2, -1), (-1, -1), "Helvetica-Bold", 10),
        ("TOPPADDING", (2, -1), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 14 * mm))

    # Payment details
    if payment:
        story.append(Paragraph(L["payment"], label))
        pay_bits = []
        if payment.get("bank"):
            pay_bits.append(f"{L['bank']}: {payment['bank']}")
        if payment.get("iban"):
            pay_bits.append(f"{L['iban']}: {payment['iban']}")
        if payment.get("bic"):
            pay_bits.append(f"{L['bic']}: {payment['bic']}")
        pay_bits.append(f"{L['reference']}: {data['number']}")
        for b in pay_bits:
            story.append(Paragraph(b, normal))
        story.append(Spacer(1, 6 * mm))

    # Small-business / no-VAT note
    if not tax_rate and config.get("small_business_note"):
        story.append(Paragraph(config["small_business_note"], small))
        story.append(Spacer(1, 3 * mm))

    if data.get("notes"):
        story.append(Paragraph(data["notes"], small))

    doc.build(story)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    args = sys.argv[1:]

    config_path = CONFIG_PATH
    if "--config" in args:
        i = args.index("--config")
        try:
            config_path = Path(args[i + 1])
        except IndexError:
            sys.exit("--config needs a path")
        del args[i:i + 2]

    if args and args[0] == "--sample":
        path = HERE / "invoice.json"
        path.write_text(json.dumps(_sample_invoice(), indent=2))
        print(f"Wrote {path}")
        return

    config = load_config(config_path)

    if args:
        data = json.loads(Path(args[0]).read_text())
        data.setdefault("tax_rate", config.get("tax_rate", 0))
        data.setdefault("notes", config.get("notes", ""))
    else:
        data = gather_interactive(config)

    if not data.get("items"):
        sys.exit("No line items - nothing to bill.")

    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    safe_number = str(data["number"]).replace("/", "-").replace(" ", "")
    out_path = out_dir / f"invoice-{safe_number}.pdf"
    if out_path.exists():
        answer = input(f"{out_path.name} already exists. Overwrite? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit("Cancelled - pick a different invoice number.")
    build_pdf(data, config, out_path)
    print(f"\nCreated {out_path}")


if __name__ == "__main__":
    main()
