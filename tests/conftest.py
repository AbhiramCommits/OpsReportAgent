"""Shared pytest fixtures.

Everything here is offline: documents are generated in a temp directory with
reportlab / python-docx / the stdlib email package and LLM_PROVIDER is ``stub``.
"""

from __future__ import annotations

import csv
import email.message
import email.policy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import docx
import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from opsreportagent.config import PROJECT_ROOT, Settings
from opsreportagent.db import Database, reset_default_database

TEMPLATES_DIR = PROJECT_ROOT / "templates"
REAL_CORPUS = PROJECT_ROOT / "data" / "corpus"
REAL_GOLD = PROJECT_ROOT / "data" / "gold" / "gold_labels.csv"


# --------------------------------------------------------------------------
# Document writers
# --------------------------------------------------------------------------


def write_pdf(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    _, height = LETTER
    y = height - 60
    for line in lines:
        c.drawString(60, y, line)
        y -= 15
    c.showPage()
    c.save()
    return path


def write_docx(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(str(path))
    return path


def write_eml(path: Path, subject: str, body: str, when: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "tester@store.retailco.example"
    msg["To"] = "store-ops@retailco.example"
    when = when or datetime(2026, 9, 5, 9, 30, tzinfo=UTC)
    msg["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body)
    path.write_bytes(msg.as_bytes())
    return path


# --------------------------------------------------------------------------
# Fixture corpus (3 documents)
# --------------------------------------------------------------------------

PDF_DOC = {
    "filename": "incident_fixture.pdf",
    "lines": [
        "RETAILCO STORE OPERATIONS - INCIDENT WRITE-UP",
        "Reference: INC-9001",
        "Store: ST-002",
        "Date: August 10, 2026",
        "Vendor: Polar Mechanical",
        "Reported by: Test Reporter",
        "Severity: High",
        "Narrative:",
        "The walk-in freezer compressor failed overnight and product is thawing.",
        "Estimated impact: $1,234.56",
        "Status: Open",
    ],
    "gold": {
        "category": "Equipment Failure",
        "store_id": "ST-002",
        "incident_date": "2026-08-10",
        "vendor": "Polar Mechanical",
        "dollar_impact": "1234.56",
        "severity": "High",
    },
}

DOCX_DOC = {
    "filename": "vendor_fixture.docx",
    "lines": [
        "Vendor Notice",
        "Reference: VN-9002",
        "Store: ST-003",
        "Date: September 2, 2026",
        "Vendor: Cascade Beverage",
        "Prepared by: Test Reporter",
        "Severity: Medium",
        "The delivery arrived two pallets short of the order.",
        "Estimated impact: $2,500.00",
        "Status: Open",
    ],
    "gold": {
        "category": "Vendor Delivery",
        "store_id": "ST-003",
        "incident_date": "2026-09-02",
        "vendor": "Cascade Beverage",
        "dollar_impact": "2500.00",
        "severity": "Medium",
    },
}

EML_DOC = {
    "filename": "email_fixture.eml",
    "subject": "[Store Ops] EM-9003 - ST-004",
    "body": (
        "Store: ST-004\n"
        "Date: 9/5/26\n"
        "Vendor: Aegis Compliance Supply\n"
        "\n"
        "Not sure what happened. Let me know what you need from me.\n"
        "\n"
        "Thanks,\nTest Reporter\n"
    ),
    # Gold keeps the truth; the vague text hides it, so this row is expected to
    # be flagged for review by the pipeline.
    "gold": {
        "category": "Safety Incident",
        "store_id": "ST-004",
        "incident_date": "2026-09-05",
        "vendor": "Aegis Compliance Supply",
        "dollar_impact": "",
        "severity": "",
    },
}


@dataclass
class CorpusFixture:
    root: Path
    corpus_dir: Path
    gold_path: Path
    files: list[Path]

    @property
    def gold_rows(self) -> list[dict]:
        with self.gold_path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


@pytest.fixture
def corpus(tmp_path: Path) -> CorpusFixture:
    """A deterministic 3-document corpus plus its gold labels."""
    root = tmp_path
    corpus_dir = root / "data" / "corpus"
    gold_path = root / "data" / "gold" / "gold_labels.csv"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    gold_path.parent.mkdir(parents=True, exist_ok=True)

    files = [
        write_pdf(corpus_dir / PDF_DOC["filename"], PDF_DOC["lines"]),
        write_docx(corpus_dir / DOCX_DOC["filename"], DOCX_DOC["lines"]),
        write_eml(corpus_dir / EML_DOC["filename"], EML_DOC["subject"], EML_DOC["body"]),
    ]

    with gold_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["source_file", "category", "store_id", "incident_date", "vendor", "dollar_impact", "severity"]
        )
        for spec in (PDF_DOC, DOCX_DOC, EML_DOC):
            gold = spec["gold"]
            writer.writerow(
                [
                    spec["filename"],
                    gold["category"],
                    gold["store_id"],
                    gold["incident_date"],
                    gold["vendor"],
                    gold["dollar_impact"],
                    gold["severity"],
                ]
            )

    return CorpusFixture(root=root, corpus_dir=corpus_dir, gold_path=gold_path, files=files)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    reset_default_database()
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def database(db_url: str) -> Database:
    db = Database(db_url)
    db.create_all()
    return db


@pytest.fixture
def settings(database: Database, corpus: CorpusFixture) -> Settings:
    return Settings(
        llm_provider="stub",
        database_url=database.url,
        confidence_threshold=0.75,
        prompt_version="v1",
        corpus_dir=str(corpus.corpus_dir),
        gold_dir=str(corpus.gold_path.parent),
        reports_dir=str(corpus.root / "reports"),
        templates_dir=str(TEMPLATES_DIR),
    )
