#!/usr/bin/env python
"""Generate a reproducible synthetic retail-ops corpus + gold labels.

Produces EXACTLY 60 documents into ``data/corpus``:

* 20 incident write-ups as PDF (reportlab)
* 20 vendor notices as DOCX (python-docx)
* 20 store-ops emails as EML (stdlib ``email``)

and hand-labelled ground truth for all 60 into ``data/gold/gold_labels.csv``.

The corpus is generated from a template whose ground-truth fields are known at
generation time, so the gold CSV is *derived from the generator's own truth*,
not from a human reading the rendered text. That is documented honestly in the
README. The RNG is seeded so the corpus is byte-for-byte reproducible.

~10 documents (indices where ``i % 6 == 3``) are deliberately vague/truncated:
they omit the store, date, vendor, dollar impact, and severity fields, which
drives low-confidence extractions and populates the human review queue.

10 documents (indices where ``i % 6 == 4``) are non-vague "trap" docs whose
category is expressed with a synonym that prompt ``extract.v1`` does not
recognise; prompt v2 adds those synonyms (see ``extract.v2.yaml``).
"""

from __future__ import annotations

import argparse
import csv
import random
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import docx
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

SEED = 20260831
BASE_DATE = date(2026, 8, 31)  # start of the 6-week reporting window
WEEKS = 6

CATEGORIES = [
    "Equipment Failure",
    "Food Safety",
    "Vendor Delivery",
    "Safety Incident",
    "POS/IT Outage",
    "Facilities",
    "Inventory Shrink",
]

STORES = [f"ST-{n:03d}" for n in range(1, 21)]

VENDORS = {
    "Equipment Failure": ["Northwind Cold Chain", "Polar Mechanical", "Vertex Mechanical"],
    "Food Safety": ["GreenLeaf Produce", "Harbor Dairy Co", "Sterling Food Group"],
    "Vendor Delivery": [
        "Cascade Beverage",
        "Ridgeline Distribution",
        "Summit Wholesale",
        "BluePeak Logistics",
    ],
    "Safety Incident": ["Aegis Compliance Supply", "Sentinel Compliance Group"],
    "POS/IT Outage": ["Nexus Retail Systems", "Gridpoint Networks", "Teller Support Services"],
    "Facilities": ["Keystone Building Services", "Ironwood Facilities", "Anchor Maintenance"],
    "Inventory Shrink": ["Watchtower Loss Prevention", "Sentinel Audit Group"],
}

SUBCATEGORIES = {
    "Equipment Failure": ["Refrigeration", "HVAC", "Cooking Equipment", "Bakery Equipment"],
    "Food Safety": ["Inspection", "Temperature", "Expired Product", "Cross-Contamination", "Hygiene"],
    "Vendor Delivery": ["Short Shipment", "Late Delivery", "Damaged Goods", "Backorder"],
    "Safety Incident": ["Slip/Fall", "Cut/Laceration", "Struck By", "Overexertion"],
    "POS/IT Outage": ["Register Outage", "Network", "Payment", "Software"],
    "Facilities": ["Roofing", "Plumbing", "Lighting", "Flooring", "Doors"],
    "Inventory Shrink": ["Theft", "Cycle Count", "Receiving", "Unknown Loss"],
}

# Phrases that prompt v1's keyword list recognises.
V1_PHRASES = {
    "Equipment Failure": [
        "the walk-in freezer compressor failed overnight and product is thawing",
        "the refrigeration unit in the meat room is running warm",
        "the HVAC rooftop unit stopped cooling the sales floor",
        "the fryer bank lost power during the lunch rush",
    ],
    "Food Safety": [
        "a food safety inspection found contamination on the prep table",
        "expired product was found in the dairy case during the morning check",
        "temperature abuse was recorded on the hot bar",
        "spoiled produce was pulled from the display",
    ],
    "Vendor Delivery": [
        "the delivery arrived two pallets short of the order",
        "the shipment was missing twelve cases of water",
        "a vendor backorder delayed the beverage order by a day",
        "the delivery truck left a pallet blocking the dock",
    ],
    "Safety Incident": [
        "an employee injury was reported after a slip and fall in aisle 6",
        "a safety incident occurred when a case fell from the top stock",
        "first aid was given after a team member cut their hand",
        "an employee injury was logged in the backroom",
    ],
    "POS/IT Outage": [
        "the POS system was down for two hours during peak traffic",
        "a network outage took the registers offline",
        "the checkout lanes lost the point of sale connection",
        "a register reboot loop slowed the front end",
    ],
    "Facilities": [
        "a roof leak developed over the dairy aisle",
        "the restroom plumbing backed up during the afternoon",
        "the parking lot lighting is out on the east side",
        "a floor tile came loose near the entrance",
    ],
    "Inventory Shrink": [
        "shrink on high-value electronics was flagged this week",
        "a theft was reported in the liquor aisle",
        "the cycle count variance on energy drinks was large",
        "missing inventory was found in the receiving audit",
    ],
}

# Phrases that prompt v1 does NOT recognise but prompt v2 does (the revision).
TRAP_PHRASES = {
    "Equipment Failure": [
        "the walk-in cooler is not holding temperature",
        "the dough mixer stopped mid-batch",
        "the icemaker on the deli line quit",
    ],
    "Food Safety": [
        "product was found past its use-by date on the shelf",
        "raw chicken was stored above ready-to-eat items",
        "a handwashing violation was noted at the deli sink",
    ],
    "Vendor Delivery": [
        "the truck arrived four cases short of the order",
        "only part of the order showed up on the morning run",
        "the produce order came in three crates light",
    ],
    "Safety Incident": [
        "a team member slipped near the entrance and needed assistance",
        "a customer fell by the wet floor sign",
        "someone was struck by a rolling cart in the stockroom",
    ],
    "POS/IT Outage": [
        "the payment terminals stopped accepting cards",
        "the card readers at the front end all showed an error",
        "the scanners at the front end would not scan",
    ],
    "Facilities": [
        "broken tiles in aisle 4 created a trip hazard",
        "the ceiling above the bakery started dripping",
        "the exterior doors would not latch overnight",
    ],
    "Inventory Shrink": [
        "fourteen units were unaccounted for in the cycle count",
        "the overnight audit found a gap in the tobacco count",
        "several cases vanished between receiving and the floor",
    ],
}

# Weak category hints used in the truncated/vague documents (half of them).
VAGUE_HINTS = {
    "Equipment Failure": "the cooler seems off again",
    "Food Safety": "something on the hot bar looked wrong",
    "Vendor Delivery": "the load was not right",
    "Safety Incident": "someone got hurt",
    "POS/IT Outage": "the registers are acting up",
    "Facilities": "there is a problem with the building",
    "Inventory Shrink": "the counts do not match",
}

DOLLAR_RANGE = {
    "Equipment Failure": (450, 14000),
    "Food Safety": (150, 4200),
    "Vendor Delivery": (200, 8500),
    "Safety Incident": (300, 15000),
    "POS/IT Outage": (500, 9000),
    "Facilities": (250, 22000),
    "Inventory Shrink": (400, 11000),
}

SEVERITY_WEIGHTS = {
    "Equipment Failure": ["Medium", "High", "High", "Critical"],
    "Food Safety": ["Medium", "High", "Critical"],
    "Vendor Delivery": ["Low", "Medium", "Medium", "High"],
    "Safety Incident": ["Medium", "High", "High", "Critical"],
    "POS/IT Outage": ["Low", "Medium", "High"],
    "Facilities": ["Low", "Medium", "High"],
    "Inventory Shrink": ["Medium", "High"],
}

REPORTERS = [
    "Dana Whitfield",
    "Marcus Lee",
    "Priya Raman",
    "Owen Barrett",
    "Sofia Alvarez",
    "Jamal Carter",
    "Elena Petrova",
    "Tobias Nguyen",
]
RECIPIENTS = ["store-ops@retailco.example", "district-managers@retailco.example"]


@dataclass
class DocSpec:
    index: int
    kind: str  # pdf | docx | eml
    filename: str
    category: str
    subcategory: str
    store_id: str
    incident_date: date
    vendor: str | None
    dollar_impact: float | None
    severity: str | None
    vague: bool
    trap: bool
    phrase: str
    record_id: str
    reporter: str
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Spec construction
# --------------------------------------------------------------------------


def build_specs(seed: int = SEED) -> list[DocSpec]:
    rng = random.Random(seed)
    specs: list[DocSpec] = []
    for i in range(60):
        if i < 20:
            kind, prefix = "pdf", "incident"
        elif i < 40:
            kind, prefix = "docx", "vendor_notice"
        else:
            kind, prefix = "eml", "store_email"

        category = CATEGORIES[i % len(CATEGORIES)]
        subcategory = rng.choice(SUBCATEGORIES[category])
        vague = i % 6 == 3
        trap = i % 6 == 4

        store_id = STORES[(i * 7 + rng.randrange(0, 20)) % 20]
        week = i % WEEKS
        day = (i * 3) % 7
        incident_date = BASE_DATE + timedelta(weeks=week, days=day)

        vendor = rng.choice(VENDORS[category])
        lo, hi = DOLLAR_RANGE[category]
        dollar = round(rng.uniform(lo, hi), 2)
        severity = rng.choice(SEVERITY_WEIGHTS[category])

        if trap:
            phrase = rng.choice(TRAP_PHRASES[category])
        else:
            phrase = rng.choice(V1_PHRASES[category])

        if vague:
            # Truncated doc: drop fields and use weak hints. ``vb`` varies the
            # amount of recoverable signal so prompt v2 has something to fix.
            vb = (i // 6) % 2
            phrase = VAGUE_HINTS[category] if vb == 0 else "not sure what happened"
            store_id = store_id if vb == 0 else ""
            vendor = vendor if i % 3 == 0 else None
            dollar = dollar if vb == 0 else None
            severity = None
            subcategory = ""

        record_id = f"{'INC' if kind == 'pdf' else 'VN' if kind == 'docx' else 'EM'}-{i:04d}"
        specs.append(
            DocSpec(
                index=i,
                kind=kind,
                filename=f"{prefix}_{i:03d}.{kind}",
                category=category,
                subcategory=subcategory,
                store_id=store_id,
                incident_date=incident_date,
                vendor=vendor,
                dollar_impact=dollar,
                severity=severity,
                vague=vague,
                trap=trap,
                phrase=phrase,
                record_id=record_id,
                reporter=rng.choice(REPORTERS),
            )
        )
    return specs


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _long_date_safe(d: date) -> str:
    # Build manually: %-d is glibc/macOS specific and not portable.
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _short_date(d: date) -> str:
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _impact_lines(spec: DocSpec) -> list[str]:
    """Render the dollar-impact block.

    Vague documents deliberately phrase the impact in words ("about 3,200.47
    dollars") which prompt v1 cannot parse but prompt v2's currency
    normalisation rules can.
    """
    if spec.dollar_impact is None:
        return []
    if spec.vague:
        return [f"Estimated impact: about {spec.dollar_impact:,.2f} dollars"]
    return [f"Estimated impact: {_money(spec.dollar_impact)}", "Status: Open"]


def render_pdf(spec: DocSpec, path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    y = height - 60
    lines: list[str] = []
    lines.append("RETAILCO STORE OPERATIONS - INCIDENT WRITE-UP")
    lines.append("=" * 72)
    lines.append(f"Reference: {spec.record_id}")
    if spec.store_id:
        lines.append(f"Store: {spec.store_id}")
    if spec.vague:
        lines.append(f"Date: {_short_date(spec.incident_date)}")
    else:
        lines.append(f"Date: {_long_date_safe(spec.incident_date)}")
    if spec.vendor:
        lines.append(f"Vendor: {spec.vendor}")
    lines.append(f"Reported by: {spec.reporter}")
    if not spec.vague:
        lines.append(f"Severity: {spec.severity}")
    lines.append("")
    lines.append("Narrative:")
    narrative = spec.phrase.capitalize() + "."
    if not spec.vague:
        narrative += (
            " The team followed standard procedures and documented the event."
            " A follow-up action was assigned to the store manager."
        )
    else:
        narrative += " Please advise on next steps as soon as possible."
    lines.extend(_wrap(narrative, 88))
    impact = _impact_lines(spec)
    if impact:
        lines.append("")
        lines.extend(impact)
    for line in lines:
        if y < 70:
            c.showPage()
            y = height - 60
        c.drawString(60, y, line)
        y -= 15
    c.showPage()
    c.save()


def render_docx(spec: DocSpec, path: Path) -> None:
    document = docx.Document()
    document.add_heading("Vendor Notice", level=1)
    document.add_paragraph(f"Reference: {spec.record_id}")
    if spec.store_id:
        document.add_paragraph(f"Store: {spec.store_id}")
    if spec.vague:
        document.add_paragraph(f"Date: {_short_date(spec.incident_date)}")
    else:
        document.add_paragraph(f"Date: {_long_date_safe(spec.incident_date)}")
    if spec.vendor:
        document.add_paragraph(f"Vendor: {spec.vendor}")
    document.add_paragraph(f"Prepared by: {spec.reporter}")
    if not spec.vague:
        document.add_paragraph(f"Severity: {spec.severity}")
    document.add_paragraph("")
    body = spec.phrase.capitalize() + "."
    if not spec.vague:
        body += (
            " Please review the attached details and confirm the credit or the"
            " replacement plan by end of week."
        )
    else:
        body += " We will follow up once we have more information."
    document.add_paragraph(body)
    for line in _impact_lines(spec):
        document.add_paragraph(line)
    document.save(str(path))


def render_eml(spec: DocSpec, path: Path) -> None:
    msg = EmailMessage()
    subject = f"[Store Ops] {spec.record_id}"
    if spec.store_id:
        subject += f" - {spec.store_id}"
    msg["Subject"] = subject
    msg["From"] = f"{spec.reporter.lower().replace(' ', '.')}@store.retailco.example"
    msg["To"] = RECIPIENTS[spec.index % len(RECIPIENTS)]
    msg["Date"] = format_datetime(
        _dt(spec.incident_date), usegmt=True
    )
    lines = []
    if spec.store_id:
        lines.append(f"Store: {spec.store_id}")
    if spec.vague:
        lines.append(f"Date: {_short_date(spec.incident_date)}")
    else:
        lines.append(f"Date: {_long_date_safe(spec.incident_date)}")
    if spec.vendor:
        lines.append(f"Vendor: {spec.vendor}")
    if not spec.vague:
        lines.append(f"Severity: {spec.severity}")
    lines.append("")
    body = spec.phrase.capitalize() + "."
    if not spec.vague:
        body += (
            " Can someone from the district team take a look and confirm the"
            " impact figure below is correct?"
        )
    else:
        body += " Let me know what you need from me."
    lines.append(body)
    impact = _impact_lines(spec)
    if impact:
        lines.append("")
        lines.extend(impact)
    lines.append("")
    lines.append(f"Thanks,\n{spec.reporter}")
    msg.set_content("\n".join(lines))
    path.write_bytes(msg.as_bytes())


def _dt(d: date):
    from datetime import datetime

    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=UTC)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


RENDERERS = {"pdf": render_pdf, "docx": render_docx, "eml": render_eml}


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def write_gold(specs: list[DocSpec], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["source_file", "category", "store_id", "incident_date", "vendor", "dollar_impact", "severity"]
        )
        for spec in sorted(specs, key=lambda s: s.filename):
            writer.writerow(
                [
                    spec.filename,
                    spec.category,
                    spec.store_id,
                    spec.incident_date.isoformat(),
                    spec.vendor or "",
                    f"{spec.dollar_impact:.2f}" if spec.dollar_impact is not None else "",
                    spec.severity or "",
                ]
            )


def generate(
    corpus_dir: str | Path = "data/corpus",
    gold_path: str | Path = "data/gold/gold_labels.csv",
    seed: int = SEED,
) -> list[DocSpec]:
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    specs = build_specs(seed)
    for spec in specs:
        RENDERERS[spec.kind](spec, corpus_dir / spec.filename)
    write_gold(specs, Path(gold_path))
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic ops corpus.")
    parser.add_argument("--out", default="data/corpus", help="Corpus output directory.")
    parser.add_argument("--gold", default="data/gold/gold_labels.csv", help="Gold CSV path.")
    parser.add_argument("--seed", type=int, default=SEED, help="RNG seed (reproducibility).")
    args = parser.parse_args(argv)

    specs = generate(args.out, args.gold, args.seed)
    kinds: dict[str, int] = {}
    for spec in specs:
        kinds[spec.kind] = kinds.get(spec.kind, 0) + 1
    vague = sum(1 for s in specs if s.vague)
    trap = sum(1 for s in specs if s.trap)
    print(f"Wrote {len(specs)} documents to {args.out}/")
    print(f"  by format: {kinds}")
    print(f"  vague/truncated: {vague}   trap (v1-unrecognised synonyms): {trap}")
    print(f"Gold labels: {args.gold} ({len(specs)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
