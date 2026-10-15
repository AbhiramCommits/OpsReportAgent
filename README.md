# OpsReportAgent

**A document-to-reporting pipeline for retail store operations.**

Store operations teams receive a constant stream of unstructured paperwork: incident
write-ups as PDFs, vendor notices as Word documents, and store-ops emails. None of it is
structured, none of it lands in a database, and the weekly rollup is assembled by hand from
whoever happens to be on shift.

OpsReportAgent turns that stream into an auditable pipeline:

- **Batch-ingests mixed-format documents** (PDF, DOCX, EML, TXT) with whitespace
  normalisation and SHA-256 de-duplication, so re-ingesting the same folder is a no-op.
- **Summarises, classifies and extracts fields** with an LLM driven by a *versioned prompt
  library*, modelling each extraction as a LangGraph state machine.
- **Writes structured rows** (category, store, date, vendor, dollar impact, severity, summary,
  calibrated confidence) into SQL (PostgreSQL or SQLite).
- **Routes low-confidence extractions to a human review queue**, logs every reviewer
  correction as an audit row, and re-scores the run immediately.
- **Scores accuracy against a hand-labelled gold set** per field, with a category confusion
  matrix and precision/recall for the review flag itself.
- **Publishes a weekly rollup** (Markdown + HTML with charts and an LLM-written executive
  summary) and can be run end-to-end from cron.

Everything runs **offline and deterministically** with a built-in `stub` LLM provider, so the
test suite and the full demo work with no API keys.

---

## Table of contents

1. [Architecture](#architecture)
2. [LangGraph node flow](#langgraph-node-flow)
3. [Data model](#data-model)
4. [How to run](#how-to-run)
5. [Prompt engineering: the versioned library](#prompt-engineering-the-versioned-library)
6. [Reviewer workflow](#reviewer-workflow)
7. [RESULTS (measured in this build)](#results-measured-in-this-build)
8. [Repository layout](#repository-layout)

---

## Architecture

```mermaid
flowchart LR
  A["Mixed corpus: PDF / DOCX / EML / TXT"] --> B["Ingestion: normalise + sha256 dedupe"]
  B --> C[("documents")]
  C --> D["LangGraph StateGraph (per document)"]
  D --> L1["load"] --> L2["summarize"] --> L3["classify"] --> L4["extract"] --> L5["validate"] --> L6["persist"]
  L6 --> E[("extractions")]
  P[("prompt library: YAML + sha256")] -.-> L2
  P -.-> L3
  P -.-> L4
  E --> F{"confidence below threshold?"}
  F -->|yes| G["Human review queue"]
  F -->|no| R
  G --> H["review fix -> reviews audit log"]
  H --> I["re-score run"]
  E --> J["Scoring vs gold set"]
  I --> J
  J --> K[("scores")]
  E --> R["Weekly rollup report"]
  K --> R
  R --> S["reports/weekly_*.md + .html + figs/"]
```

A generated data-flow figure annotated with real run counts is produced for every report
([`reports/figs/data_flow.png`](reports/figs/data_flow.png)).

### Tech stack

| Concern | Choice |
| --- | --- |
| Language / layout | Python 3.11, `src/` layout, `uv` or `pip + venv`, `typer` CLI |
| Prompting + models | LangChain (`ChatPromptTemplate`, `ChatOpenAI`, `ChatAnthropic`) + a deterministic `StubLLM` |
| Orchestration | LangGraph `StateGraph` |
| Storage | SQLAlchemy 2.x over PostgreSQL, SQLite fallback (`DATABASE_URL`) |
| Aggregation / charts | pandas, matplotlib (`Agg` backend) |
| Templating | Jinja2 (Markdown + HTML) |
| Parsing | pdfplumber, python-docx, stdlib `email` |
| Quality | pytest + ruff, GitHub Actions, Docker + docker-compose with Postgres 16 |

## LangGraph node flow

The graph is compiled from a single `PipelineContext` and invoked **once per document**:

```
START -> load -> summarize -> classify -> extract -> validate -> persist -> END
```

| Node | What it does | Prompt |
| --- | --- | --- |
| `load` | Reads `documents.raw_text` for the document id in state | — |
| `summarize` | Plain-language summary + key points | `summarize.v1` |
| `classify` | One taxonomy category + subcategory + confidence | `classify.v1` |
| `extract` | All structured fields + overall calibrated confidence | `extract.v1` / `extract.v2` |
| `validate` | Schema-validates each payload, cross-checks classifier vs extractor, applies the confidence threshold to set `needs_review` | — |
| `persist` | Writes an `extractions` row (prompt version + prompt hash recorded) | — |

Every LLM call is validated against the prompt's JSON Schema. On failure the call is retried
**once** with a repair instruction; if the repair also fails the extraction is recorded with
`confidence = 0.0` and `needs_review = true` rather than being dropped.

## Data model

| Table | Column | Type | Notes |
| --- | --- | --- | --- |
| `documents` | `id`, `source_path`, `doc_type`, `ingested_at`, `raw_text`, `char_count`, `sha256` | int, str, str, ts, text, int, str | `sha256` is **unique** → idempotent re-ingest |
| `extractions` | `id`, `document_id`→`documents.id`, `run_id`, `category`, `subcategory`, `store_id`, `incident_date`, `vendor`, `dollar_impact`, `severity`, `summary`, `confidence`, `needs_review`, `prompt_version`, `prompt_hash`, `model`, `created_at` | int, int, str, str…, float, bool, str, str, str, ts | one row per document per run |
| `reviews` | `id`, `extraction_id`→`extractions.id`, `field_name`, `old_value`, `new_value`, `reviewer`, `note`, `created_at` | int, int, str, text, text, str, text, ts | append-only correction audit log |
| `runs` | `id`, `run_id` (unique), `started_at`, `finished_at`, `doc_count`, `provider`, `model`, `prompt_version`, `notes` | int, str, ts, ts, int, str, str, str, text | one row per extraction run |
| `scores` | `id`, `run_id`, `field_name`, `n`, `exact_match`, `accuracy`, `created_at` | int, str, str, int, int, float, ts | per-field rows plus a `__macro__` row |

---

## How to run

### Local quickstart

```bash
uv venv --python 3.11 .venv          # or: python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                 # defaults to LLM_PROVIDER=stub, SQLite

python scripts/make_corpus.py        # 60 synthetic documents + gold labels
opsreport init-db
opsreport ingest --path data/corpus
opsreport run --run-id run-001
opsreport score --run-id run-001
opsreport report --run-id run-001
```

One-command end-to-end (what the scheduler runs):

```bash
opsreport schedule --weekly --run-id run-002
```

### Docker + PostgreSQL

```bash
docker compose up --build           # postgres:16 + the app, wired via DATABASE_URL
docker compose run --rm app init-db
docker compose run --rm app schedule --weekly --run-id run-docker
```

The image is `python:3.11-slim` with a non-root `ops` user and `ENTRYPOINT ["opsreport"]`.
Verified in this build (see [RESULTS](#results-measured-in-this-build)).

### Cron scheduling

```cron
# every Monday 06:00 — ingest, extract, score and publish the weekly rollup
0 6 * * 1 cd /opt/OpsReportAgent && . .venv/bin/activate && \
  opsreport schedule --weekly --run-id "run-$(date +\%Y\%V)" >> /var/log/opsreport.log 2>&1
```

`opsreport schedule --weekly` is intentionally the only entrypoint a scheduler needs: it runs
`ingest -> run -> score -> report`, prints per-stage timings, and exits non-zero on failure.

### Switching providers

`LLM_PROVIDER` selects the backend; nothing else changes.

```bash
# Deterministic, offline, no key required (tests + demo)
LLM_PROVIDER=stub

# OpenAI
LLM_PROVIDER=openai  OPENAI_API_KEY=sk-...  MODEL_NAME=gpt-4o-mini

# Anthropic
LLM_PROVIDER=anthropic  ANTHROPIC_API_KEY=sk-ant-...  MODEL_NAME=claude-3-5-sonnet-latest
```

Real providers render the same versioned prompts through `ChatPromptTemplate` and are held to
the same JSON Schema. `StubLLM` is a `BaseChatModel` subclass, so the graph is identical for all
three providers — only the answers differ.

---

## Prompt engineering: the versioned library

Prompts are YAML files under `src/opsreportagent/prompts/`. Each file declares its `name`,
`version`, `description`, `inputs`, `system` message, `template`, a JSON Schema
(`output_schema`), a list of `known_failure_modes` and few-shot `examples`. The library
content-hashes each prompt body, and **every extraction row stores the prompt version and
hash**, so any extraction can be traced back to the exact prompt revision that produced it.

The prompt catalog — with hashes, schemas, failure modes and the full v1→v2 diff — lives in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) and the full prompt text with revision rationale lives in
[`docs/PROMPTS.md`](docs/PROMPTS.md).

**v1 → v2 in one line:** v2 adds the category synonyms, short-date formats and
word-form currency phrasing that the human corrections proved v1 was missing, and tightens
the rule that a generic noun or a company name is not evidence of a category.

---

## Reviewer workflow

```bash
opsreport review list --run-id run-001         # queue, lowest confidence first
opsreport review fix \
  --extraction-id 10 --field category --value "Vendor Delivery" \
  --reviewer "Maria Delgado" --note "Doc describes a short delivery; not equipment"
opsreport review export --out reports/corrections.csv   # feed into the next prompt revision
```

`review fix` coerces and normalises the value (`$1,234.50` → `1234.50`, `9/5/26` → `2026-09-05`),
appends a `reviews` audit row with the old value, and **re-scores the run immediately**,
printing the macro and category accuracy before → after. The full SOP is in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## RESULTS (measured in this build)

Everything below was produced by running the code in this repository. Commands are noted so
each figure can be reproduced. Machine: macOS (Apple Silicon), Python 3.11.14,
`LLM_PROVIDER=stub`, SQLite for the local numbers and `postgres:16` for the database check.
No number here is estimated, and nothing is a placeholder.

### 1. Corpus

`python scripts/make_corpus.py` (RNG seed `20260831`, reproducible byte-for-byte):

| Property | Value |
| --- | ---: |
| Documents | **60** |
| PDF incident write-ups | 20 |
| DOCX vendor notices | 20 |
| EML store-ops emails | 20 |
| Categories in taxonomy | 7 |
| Deliberately vague / truncated docs | 10 |
| "Trap" docs using category synonyms prompt v1 does not know | 10 |
| Date span | 2026-08-31 → 2026-10-11 (6 weeks) |

**Honest note on the gold set:** `data/gold/gold_labels.csv` is generated by the same script
that renders the documents, from the generator's own ground-truth record — it is *not* an
independently human-labelled corpus. It is a controlled stand-in that lets the scoring,
review and prompt-revision machinery be exercised end-to-end and offline. The 10 vague
documents keep their true labels in the gold file even though the rendered text hides those
fields, which is exactly what drives the low-confidence review queue.

### 2. Throughput and wall-clock runtime

Cold run (empty database) on the 60-document corpus, `opsreport schedule --weekly --run-id run-timing`:

| Stage | Time | Result |
| --- | ---: | --- |
| `ingest` | 1.23 s | 60 ingested / 0 duplicates / 0 skipped |
| `run` (extract) | 0.47 s | 60 extracted, 10 flagged |
| `score` | 0.02 s | macro 0.911, category 0.717 |
| `report` | 0.76 s | 7 charts + Markdown + HTML |
| **TOTAL** | **2.48 s** | |

A warm re-run (documents already ingested) is faster: ingest 0.34 s, run 0.20 s, score 0.01 s,
report 0.43 s. The full offline test suite runs in **12.50 s**.

### 3. Extraction accuracy — prompt v1 vs prompt v2

Prompt v1 = `run-002` (`opsreport schedule --weekly --run-id run-002`, 60 documents).
Prompt v2 = `run-003` (`opsreport run --run-id run-003 --prompt-version v2`, same 60 documents,
same stub provider). Neither run has human corrections applied, so the delta is purely the
prompt revision.

| field | v1 (run-002) | v2 (run-003) | delta |
| --- | ---: | ---: | ---: |
| category | 0.717 | 0.933 | **+0.217** |
| store_id | 1.000 | 1.000 | +0.000 |
| incident_date | 0.833 | 1.000 | **+0.167** |
| vendor | 1.000 | 1.000 | +0.000 |
| dollar_impact | 0.917 | 1.000 | **+0.083** |
| severity | 1.000 | 1.000 | +0.000 |
| **macro** | **0.911** | **0.989** | **+0.078** |

Source: [`reports/prompt_ab_run-002_vs_run-003.md`](reports/prompt_ab_run-002_vs_run-003.md).
`opsreport compare-runs --a run-001 --b run-003` is also generated
([`reports/prompt_ab_run-001_vs_run-003.md`](reports/prompt_ab_run-001_vs_run-003.md)); run-001
had human corrections applied (see §5) so it is a *post-review* baseline, not a clean v1
baseline. The clean v1 baseline is run-002.

### 4. Category classification and confusion highlights

Category accuracy: **0.717 (43/60) for v1 → 0.933 (56/60) for v2**.

v1's errors all collapse to the fallback category, because when no v1 keyword fires the model
still has to emit a value from the enum:

| gold category | v1 correct | v1 predicted as `Equipment Failure` |
| --- | ---: | ---: |
| Equipment Failure | 9 / 9 | 0 |
| Facilities | 6 / 8 | 2 |
| Food Safety | 6 / 9 | 3 |
| Inventory Shrink | 6 / 8 | 2 |
| POS/IT Outage | 6 / 8 | 2 |
| Safety Incident | 5 / 9 | 4 |
| Vendor Delivery | 5 / 9 | 4 |

v2 fixes the synonym misses; the only categories still imperfect are the 4 vague documents
that carry no category signal at all:

| gold category | v2 correct | v2 predicted as `Equipment Failure` |
| --- | ---: | ---: |
| Equipment Failure | 9 / 9 | 0 |
| Facilities | 7 / 8 | 1 |
| Food Safety | 8 / 9 | 1 |
| Inventory Shrink | 8 / 8 | 0 |
| POS/IT Outage | 8 / 8 | 0 |
| Safety Incident | 8 / 9 | 1 |
| Vendor Delivery | 8 / 9 | 1 |

Full matrices: [`reports/scorecard_run-002.md`](reports/scorecard_run-002.md),
[`reports/scorecard_run-003.md`](reports/scorecard_run-003.md).

### 5. Human corrections: accuracy before vs after

`opsreport review list --run-id run-001` surfaced **10 extractions**.
**23 corrections** were applied through `opsreport review fix` by reviewer *Maria Delgado*
across 10 extractions ([`reports/corrections.csv`](reports/corrections.csv)).

| metric | before review | after 23 corrections | delta |
| --- | ---: | ---: | ---: |
| macro accuracy | 0.911 | 0.975 | +0.064 |
| category accuracy | 0.717 | 0.850 | +0.133 |
| incident_date | 0.833 | 1.000 | +0.167 |
| dollar_impact | 0.917 | 1.000 | +0.083 |

Source: [`reports/scorecard_run-001_before_review.md`](reports/scorecard_run-001_before_review.md)
and [`reports/scorecard_run-001.md`](reports/scorecard_run-001.md).

### 6. Review queue size and the precision/recall of the `needs_review` flag

A document counts as a *true error* if any scored field is wrong. `needs_review` is the
prediction.

| | v1 (run-002) | v2 (run-003) |
| --- | ---: | ---: |
| queue size (flagged) | 10 | 5 |
| true positive | 10 | 4 |
| false positive | 0 | 1 |
| false negative | 9 | 0 |
| precision | 1.000 | 0.800 |
| recall | 0.526 | 1.000 |

**Interpretation:** under v1 the flag never fired on a document whose fields were all correct
(precision 1.000) but it caught only 10 of the 19 documents that contained at least one error
(recall 0.526) — the 9 misses were the silent category errors from unrecognised synonyms, which
is precisely why the prompt was revised. Under v2 the queue halves (10 → 5), every remaining
error is flagged (recall 1.000), and one fully-correct vague document is over-flagged
(precision 0.800).

### 7. Executive summary is generated from the real numbers

`exec_summary.v1` receives the aggregated statistics as JSON; the stub provider renders it
without inventing figures. This is the actual output in
[`reports/weekly_run-002.md`](reports/weekly_run-002.md):

> OpsReportAgent processed 60 operations documents across 6 weeks (2026-08-31 to 2026-10-11),
> with a total estimated dollar impact of 359,494.79. The highest-volume category was Equipment
> Failure with 26 incidents and 134,236.24 of estimated impact. Store ST-010 carried the largest
> single-store impact at 40,889.73. Severity mix: High 19, Medium 19, (not stated) 10, Critical 7,
> Low 5. 10 extraction(s) fell below the confidence threshold and were routed to the human review
> queue.

Note that the 359,494.79 total and the "26 incidents" under Equipment Failure are the *v1
model's* numbers (they include its misclassifications) — that is the honest state of the data
at the time the report was rendered, and it is recomputed from the database on every run.

### 8. Tests and CI

```console
$ ruff check .
All checks passed!

$ pytest -q --cov=src/opsreportagent
95 passed in 12.50s
TOTAL   1614 statements   110 missed   93%
```

**95 tests, 93% statement coverage**, all offline with `LLM_PROVIDER=stub`. The suite covers
ingestion per format and SHA-256 de-duplication, prompt loading/hashing, output-schema
validation including the repair-retry and hard-fail paths, the LangGraph pipeline on a
3-document fixture, confidence thresholding, scoring math on a handcrafted gold set, reviewer
fixes plus the audit log, report generation (files + charts), the scheduled job, and CLI smoke
tests for every command. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs
`ruff check .`, the test suite with `--cov-fail-under=85`, and then a full
`opsreport schedule --weekly` on the synthetic corpus plus a v1-vs-v2 comparison.

### 9. PostgreSQL / Docker verification (recorded)

`docker compose config -q` → *compose config OK*.

Against a real `postgres:16` container:

```console
$ opsreport init-db
Database: postgresql+psycopg2://ops:ops@localhost:55432/opsreport
Created/verified 5 tables:
  - documents
  - extractions
  - reviews
  - runs
  - scores

$ opsreport schedule --weekly --run-id run-pg
  [  1.23s] ingest   60 ingested / 0 duplicates / 0 skipped
  [  0.47s] run      60 extracted, 10 flagged
  ...
  macro accuracy=0.911 category accuracy=0.717 flagged=10
```

Row counts read back with `psql`:

```console
      t      | count
-------------+-------
 documents   |    60
 extractions |    60
 runs        |     2
 scores      |     7
```

The Docker image was also built and run against the same PostgreSQL instance:

```console
$ docker build -t opsreportagent:local .
$ docker run --rm opsreportagent:local --version
opsreportagent 0.1.0
$ docker run --rm -e DATABASE_URL=postgresql+psycopg2://ops:ops@host.docker.internal:55432/opsreport \
    opsreportagent:local schedule --weekly --run-id run-docker
  ... artifact: /app/reports/weekly_run-docker.html
```

Both `run-pg` and `run-docker` completed with `doc_count = 60` against PostgreSQL.
Artifacts: [`reports/weekly_run-pg.html`](reports/weekly_run-pg.html),
[`reports/scorecard_run-pg.md`](reports/scorecard_run-pg.md).

### Generated artifacts committed in this repo

| Artifact | What it is |
| --- | --- |
| [`reports/weekly_run-002.md`](reports/weekly_run-002.md) / [`.html`](reports/weekly_run-002.html) | v1 weekly rollup, 7 charts, scorecard section |
| [`reports/weekly_run-001.md`](reports/weekly_run-001.md) / [`.html`](reports/weekly_run-001.html) | rollup after the 23 human corrections |
| [`reports/weekly_run-pg.md`](reports/weekly_run-pg.md) / [`.html`](reports/weekly_run-pg.html) | rollup produced against PostgreSQL |
| [`reports/scorecard_run-001_before_review.md`](reports/scorecard_run-001_before_review.md) | scorecard before corrections |
| [`reports/scorecard_run-001.md`](reports/scorecard_run-001.md) | scorecard after 23 corrections |
| [`reports/scorecard_run-002.md`](reports/scorecard_run-002.md) | v1 scorecard |
| [`reports/scorecard_run-003.md`](reports/scorecard_run-003.md) | v2 scorecard |
| [`reports/prompt_ab_run-002_vs_run-003.md`](reports/prompt_ab_run-002_vs_run-003.md) | clean v1→v2 per-field delta |
| [`reports/prompt_ab_run-001_vs_run-003.md`](reports/prompt_ab_run-001_vs_run-003.md) | post-review baseline vs v2 |
| [`reports/corrections.csv`](reports/corrections.csv) | the 23 logged reviewer corrections |
| [`reports/figs/`](reports/figs) | 7 matplotlib charts, including the data-flow figure |

---

## Repository layout

```
OpsReportAgent/
├── pyproject.toml              # pinned deps, ruff + pytest + coverage config
├── Dockerfile                  # slim, non-root, ENTRYPOINT ["opsreport"]
├── docker-compose.yml          # postgres:16 + app
├── .github/workflows/ci.yml
├── src/opsreportagent/
│   ├── config.py               # Settings dataclass from env
│   ├── db.py                   # SQLAlchemy 2.x engine/session + 5 models
│   ├── ingest.py               # load_pdf / load_docx / load_eml / load_txt / ingest_dir
│   ├── prompt_library.py       # versioned YAML prompts + content hashing
│   ├── llm.py                  # StubLLM + LangChain providers + schema validation/repair
│   ├── pipeline.py             # LangGraph StateGraph
│   ├── scoring.py              # gold-set scoring + confusion matrix + A/B compare
│   ├── review.py               # reviewer queue, corrections, audit log, export
│   ├── report.py               # pandas aggregation, matplotlib charts, Jinja2 render
│   ├── schedule.py             # ingest -> run -> score -> report, timed
│   ├── taxonomy.py
│   └── prompts/*.yaml
├── templates/                  # report.md.j2, report.html.j2
├── scripts/make_corpus.py      # 60-doc synthetic corpus + gold labels
├── tests/                      # 95 offline tests
├── docs/RUNBOOK.md, docs/PROMPTS.md
├── data/corpus/  data/gold/    # committed so results are inspectable
└── reports/                    # committed generated artifacts
```
