# OpsReportAgent — Operations Runbook

The operational workflow for the weekly operations reporting job: how to schedule it, how to
triage the human review queue, when to escalate, and how to ship a new prompt revision.

Audience: the on-call ops analyst and the district operations manager.

---

## 1. What runs, and when

The only command a scheduler needs is:

```bash
opsreport schedule --weekly --run-id "<id>"
```

It runs four stages in order and prints a timing line for each:

| Stage | Command it wraps | Output |
| --- | --- | --- |
| `ingest` | `opsreport ingest --path $CORPUS_DIR` | new documents / duplicates / skipped |
| `run` | `opsreport run --run-id <id> --prompt-version vN` | extracted count, flagged count |
| `score` | `opsreport score --run-id <id>` | macro + category accuracy, `reports/scorecard_<id>.{csv,md}` |
| `report` | `opsreport report --run-id <id> --weeks 6` | `reports/weekly_<id>.md` + `.html` + 7 charts |

It exits non-zero on a failed stage so cron/monitoring can alert.

### Cron

```cron
# every Monday 06:00, one run id per ISO week
0 6 * * 1 cd /opt/OpsReportAgent && . .venv/bin/activate && \
  opsreport schedule --weekly --run-id "run-$(date +\%Y\%V)" >> /var/log/opsreport.log 2>&1
```

### systemd timer (alternative)

```ini
# /etc/systemd/system/opsreport-weekly.service
[Unit]
Description=OpsReportAgent weekly rollup
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/OpsReportAgent
EnvironmentFile=/opt/OpsReportAgent/.env
ExecStart=/opt/OpsReportAgent/.venv/bin/opsreport schedule --weekly --run-id run-%i
User=ops
```

```ini
# /etc/systemd/system/opsreport-weekly.timer
[Unit]
Description=Run the OpsReportAgent weekly rollup every Monday

[Timer]
OnCalendar=Mon 06:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Environment

Everything is configured through the environment (`.env.example` documents the defaults):

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_PROVIDER` | `stub` | `stub` \| `openai` \| `anthropic` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | empty | required only for the matching provider |
| `MODEL_NAME` | provider default | recorded on every `extractions` and `runs` row |
| `DATABASE_URL` | `sqlite:///./opsreport.db` | use `postgresql+psycopg2://…` in production |
| `CONFIDENCE_THRESHOLD` | `0.75` | below this the extraction is routed to review |
| `PROMPT_VERSION` | `v1` | default extract prompt version |
| `CORPUS_DIR` / `GOLD_DIR` / `REPORTS_DIR` / `TEMPLATES_DIR` | `data/corpus`, `data/gold`, `reports`, `<repo>/templates` | override in containers |

Check the effective configuration (secrets are redacted) with `opsreport info`.

---

## 2. Routine weekly operation

1. **Verify the job ran.** Check the log for `TOTAL` and for the artifact paths.
   The run row lands in `runs` with `doc_count`, `provider`, `model`, `prompt_version`.
2. **Read the scorecard.** `opsreport score --run-id "<id>"` prints per-field accuracy and the
   `needs_review` flag quality. Compare against last week.
3. **Triage the review queue** (§3).
4. **Publish** `reports/weekly_<id>.html` to the district leadership channel.
5. **If the prompt was revised this week**, run `opsreport compare-runs --a <last> --b <this>`
   and attach `reports/prompt_ab_<a>_vs_<b>.md` to the change record.

### Health thresholds (alert if breached)

| Signal | Healthy | Investigate |
| --- | --- | --- |
| `macro accuracy` (vs gold) | ≥ 0.95 | < 0.90 |
| `category accuracy` | ≥ 0.90 | < 0.80 |
| review queue size | ≤ 20% of documents | > 30% for two consecutive weeks |
| `needs_review` precision | ≥ 0.80 | < 0.60 (reviewers waste time on correct rows) |
| `needs_review` recall | ≥ 0.70 | < 0.50 (silent errors are reaching the report) |
| hard failures (`confidence = 0.0`) | 0 | > 0 |

---

## 3. Reviewer SOP

### 3.1 Pull the queue

```bash
opsreport review list --run-id "run-2026-W42"
```

Rows are sorted by **confidence ascending** — the model's own uncertainty, worst first.

### 3.2 Triage each row

Open the source document from the `source` column and check, in order:

1. **Category** — is this the operational *event*, or was the model fooled by a company name or
   a single generic word ("vendor", "truck", "audit", "count")? Category is the highest-impact
   field: it drives the headline metrics in the rollup.
2. **Store ID** — must be the `ST-###` the document refers to, not a store mentioned in passing.
3. **Incident date** — must be the *incident* date, not the email send date.
4. **Vendor** — the third party actually involved. Blank when the document names none.
5. **Dollar impact** — if the document states none, it stays empty. **Never** estimate.
6. **Severity** — must be one of `Low`, `Medium`, `High`, `Critical`. Truncated documents
   usually state no severity: leave it empty rather than guessing.

Do **not** "fix" a field to something the document does not support. The gold set scores exact
match, and an invented value is worse than a null.

### 3.3 Log the correction

```bash
opsreport review fix \
  --extraction-id 10 \
  --field category \
  --value "Vendor Delivery" \
  --reviewer "Maria Delgado" \
  --note "Doc describes a short delivery; not equipment"
```

- Allowed `--field` values: `category`, `subcategory`, `store_id`, `incident_date`, `vendor`,
  `dollar_impact`, `severity`, `summary`.
- Values are normalised automatically: `$1,234.50` → `1234.50`, `9/5/26` → `2026-09-05`.
- Clearing a field: pass an empty string (`--value ""`) to set it to null.
- Every fix appends a `reviews` row (old value, new value, reviewer, note, timestamp) and
  **re-scores the run**, printing macro and category accuracy before → after.

### 3.4 Keep the raw documents

`documents.source_path` points at the original file. Never edit the document text: the
extraction is anchored to `documents.sha256`, and changing the file would silently create a
second document on the next ingest.

### 3.5 End of the review session

```bash
opsreport review export --out reports/corrections.csv
```

This CSV is the input to the next prompt revision (§5). Attach it to the weekly review ticket.

---

## 4. Escalation rules

| Trigger | Action |
| --- | --- |
| Any document with `confidence = 0.0` (hard failure) | Page the on-call analyst. The LLM failed schema validation twice; inspect the provider status and the document. |
| `needs_review` recall < 0.50 on the weekly scorecard | Silent errors are reaching the report. Freeze publication of the inaccurate sections and open a prompt-revision ticket within 2 business days. |
| `needs_review` precision < 0.60 | The threshold is too low or the prompt is over-cautious. Re-tune `CONFIDENCE_THRESHOLD` (never above 0.90) and re-run. |
| Category accuracy drops > 0.05 week over week | New phrasing, a new vendor, or a new document type. Export corrections and revise the prompt (§5). |
| A new document format arrives (e.g. scanned PDFs) | Template change, not a prompt change: extend `ingest.load_*` / `LOADERS`, add a fixture test. |
| `dollar_impact` total swings > 25% with no corresponding document volume change | Re-check the extraction rows for a mis-parsed currency value before publishing. |
| Provider outage (`LLM_PROVIDER=openai`/`anthropic` unreachable) | Switch `LLM_PROVIDER=stub` to keep ingestion and scoring running, or hold the run. Document the gap in `runs.notes`. |
| Gold set no longer matches the corpus | `python scripts/make_corpus.py` regenerates both (seeded). Never regenerate one without the other. |

---

## 5. Shipping a new prompt revision

1. Export the week's corrections: `opsreport review export --out reports/corrections.csv`.
2. Cluster the misses (category synonyms, date formats, currency phrasing, missing fields).
3. Copy `extract.vN.yaml` → `extract.v(N+1).yaml`, bump `version`, and add:
   - `known_failure_modes` entries naming the concrete documents that failed,
   - few-shot `examples` drawn from those documents,
   - tightened normalisation rules.
4. Never edit a shipped prompt file in place — the content hash is the audit key recorded on
   every extraction row.
5. Validate on a new run id and compare:

```bash
opsreport run --run-id run-NEXT --prompt-version v2
opsreport score --run-id run-NEXT
opsreport compare-runs --a run-PREV --b run-NEXT
```

6. Ship only if the macro delta is positive **and** no individual field regresses.
7. Record the change in `docs/PROMPTS.md`.

---

## 6. Prompt catalog

Hashes are the SHA-256 of the canonical prompt body; the same value is stored on every
`extractions.prompt_hash`, so any row can be traced back to the exact revision.

| Prompt | Version | SHA-256 (short) | Purpose | Inputs | Output schema (summary) |
| --- | --- | --- | --- | --- | --- |
| `summarize` | v1 | `7ecf08b0` | 1–3 sentence plain-language summary + key points for the district readout | `document`, `taxonomy` | `{summary: string, key_points: string[]}` |
| `classify` | v1 | `454bcdba` | One taxonomy category + subcategory with a calibrated confidence | `document`, `taxonomy` | `{category: enum(7), subcategory: string\|null, confidence: number, rationale: string}` |
| `extract` | v1 | `de1efcbb` | All structured fields plus overall calibrated confidence | `document`, `taxonomy`, `today` | `{category, subcategory, store_id, incident_date, vendor, dollar_impact, severity, summary, confidence}`, `additionalProperties: false` |
| `extract` | v2 | `5106fed1` | v1 with the synonym/date/currency fixes from the human corrections | `document`, `taxonomy`, `today` | same as v1 |
| `exec_summary` | v1 | `35fc86a2` | Executive summary written **only** from supplied aggregate statistics | `stats` (JSON) | `{summary: string(≥20), highlights: string[](≥3)}` |

Full hashes:

```
summarize.v1    7ecf08b06cf987639935a47264bdea944361248a349ecb0f8f6199d0125e1c67
classify.v1     454bcdba5da754568ec4abd855532cc9a30f6223f775c97f640bad471b84a270
extract.v1      de1efcbb715f45fe228651f71cc0620c90bf0420a3bedf357afe1ed867193c0e
extract.v2      5106fed1c845e5472f2206705cd661cf34f001adca0ca10270b51a00a759954e
exec_summary.v1 35fc86a2041bcd6c5f162b24cb1b6794738aae4f29c867489bc76b85ff631aa9
```

Print the live catalog with:

```bash
python -c "from opsreportagent.prompt_library import list_prompts; [print(p) for p in list_prompts()]"
```

### Known failure modes per prompt

| Prompt | Known failure modes |
| --- | --- |
| `summarize.v1` | Inventing a dollar figure; summarising a truncated document as if complete; returning prose instead of JSON. |
| `classify.v1` | Triggering on a generic word ("vendor"); emitting a category outside the enum; confidently classifying a truncated document. |
| `extract.v1` | Non-ISO dates; currency strings instead of numbers; inventing an impact; treating "vendor" as evidence of a Vendor Delivery doc; high confidence on vague documents. |
| `extract.v2` | Inherits all of v1's *structural* risks but documents and fixes the specific v1 misses: `cooler`/`mixer`/`icemaker` (Equipment), `use-by`/`stored above`/`handwashing` (Food Safety), `truck`/`cases short`/`crates light` (Vendor Delivery), `slipped`/`fell`/`struck by` (Safety), `payment terminal`/`card reader`/`scanner` (POS/IT), `tiles`/`ceiling`/`doors` (Facilities), `unaccounted`/`vanished`/`gap in the` (Shrink); short numeric dates `9/23/26`; word-form currency `about 3,310.84 dollars`. Also adds an explicit word-boundary rule so `truck` no longer fires inside `struck`. |
| `exec_summary.v1` | Rounding or inventing a figure; describing a trend the statistics do not cover. |

### What changed between v1 and v2

| Change | Why (evidence) |
| --- | --- |
| +3–5 category synonyms per taxonomy branch | 9 of the 60 documents used a synonym v1 did not know and fell through to the fallback category (see `reports/corrections.csv`). |
| Accept `M/D/YY` and `M/D/YYYY` dates | 10 of 60 documents wrote short numeric dates; v1 returned `incident_date = null` for all of them (field accuracy 0.833). |
| Accept `N dollars` / `USD N` currency | 5 of 60 documents phrased the impact in words; v1 returned `dollar_impact = null` (field accuracy 0.917). |
| Leading word-boundary keyword matching | v1's unfiltered substring match let `truck` fire inside `struck`, mislabelling Safety Incidents as Vendor Delivery. |
| Explicit "a company name or a generic noun is not a category" rule | v1's keyword list had to drop the bare word `vendor` after it over-triggered on unrelated documents. |
| Confidence requires category evidence | v1 could report 0.97 confidence when it had a store and a date but had only *guessed* the category. |

Measured effect: category accuracy **0.717 → 0.933**, incident_date **0.833 → 1.000**,
dollar_impact **0.917 → 1.000**, macro **0.911 → 0.989**, review queue **10 → 5** documents
([`reports/prompt_ab_run-002_vs_run-003.md`](../reports/prompt_ab_run-002_vs_run-003.md)).

---

## 7. Database operations

### SQLite (demo / single node)

Nothing to do. `opsreport init-db` creates `opsreport.db` in the working directory.

### PostgreSQL (production)

```bash
docker compose up -d postgres
export DATABASE_URL="postgresql+psycopg2://ops:ops@localhost:5432/opsreport"
opsreport init-db
```

- `init-db` is idempotent (`CREATE TABLE IF NOT EXISTS` semantics via SQLAlchemy metadata).
- The schema is created by SQLAlchemy metadata; for a managed environment, generate a migration
  per release and apply it before the scheduled run.
- Back up before every prompt rollout so an A/B can be rolled back by restoring the
  `extractions` rows for the previous run id.

### Useful queries

```sql
-- review queue for one run, worst first
select id, confidence, category, store_id, incident_date
from extractions where run_id = 'run-2026-W42' and needs_review
order by confidence asc;

-- did the review flag help? (field tells you what gets corrected)
select field_name, count(*) from reviews group by 1 order by 2 desc;

-- per-field accuracy history
select run_id, field_name, accuracy from scores where field_name <> '__macro__' order by run_id;
```
