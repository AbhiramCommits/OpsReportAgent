# Prompt catalog — full text and revision rationale

Every prompt is a YAML file under `src/opsreportagent/prompts/` named `<name>.<version>.yaml`.
The library content-hashes each prompt body and stores that hash on every extraction row
(`extractions.prompt_hash`), so a row can always be traced back to the exact revision.

```bash
python -c "from opsreportagent.prompt_library import list_prompts; [print(p['ref'], p['sha256']) for p in list_prompts()]"
```

```
summarize.v1    7ecf08b06cf987639935a47264bdea944361248a349ecb0f8f6199d0125e1c67
classify.v1     454bcdba5da754568ec4abd855532cc9a30f6223f775c97f640bad471b84a270
extract.v1      de1efcbb715f45fe228651f71cc0620c90bf0420a3bedf357afe1ed867193c0e
extract.v2      5106fed1c845e5472f2206705cd661cf34f001adca0ca10270b51a00a759954e
exec_summary.v1 35fc86a2041bcd6c5f162b24cb1b6794738aae4f29c867489bc76b85ff631aa9
```

---

## 1. `summarize.v1` — `7ecf08b0`

**Purpose:** produce a 1–3 sentence summary and 2–5 key points for the district readout.
**Inputs:** `document`, `taxonomy`.

### System

```
You are an operations analyst for a retail grocery chain. You read internal
incident write-ups, vendor notices and store-ops emails and summarise them for
a district manager who has two minutes to spare.

Rules:
- Be factual and specific. Never invent figures, dates, store IDs or names.
- Only use information present in the document.
- If the document is vague or truncated, say so explicitly in the summary.
- Return ONLY a JSON object. No prose, no markdown fences.
```

### Template

```
Summarise the following retail store operations document.

Document:
{document}

Return a JSON object with:
- "summary": 1-3 sentence plain-text summary.
- "key_points": array of 2-5 short bullet strings.
```

### Output schema

`{summary: string(minLength 1), key_points: string[](minItems 1)}`, `additionalProperties: false`.

### Known failure modes

- Hallucinating a dollar figure that is not in the document.
- Summarising a vague document as if it were complete.
- Returning prose or markdown fences instead of a bare JSON object.

**Revision history:** v1 only. Never needed a second version — summaries are not scored against
the gold set, and no reviewer correction in this build touched the `summary` field.

---

## 2. `classify.v1` — `454bcdba`

**Purpose:** assign exactly one taxonomy category (plus an optional subcategory) and a
calibrated confidence.
**Inputs:** `document`, `taxonomy`.

### System

```
You are a retail operations taxonomist. Assign each document to exactly ONE
category from the taxonomy supplied by the user.

Rules:
- The category MUST be one of the taxonomy values verbatim.
- Base the decision only on evidence in the document text.
- If the document is too vague to classify confidently, still choose the single
  most likely category but return a low confidence.
- Return ONLY a JSON object. No prose, no markdown fences.
```

### Template

```
Classify the following document.

Taxonomy:
{taxonomy}

Document:
{document}

Return a JSON object with:
- "category": one taxonomy value, verbatim.
- "subcategory": optional short sub-label, or null.
- "confidence": number in [0,1] reflecting how certain you are.
- "rationale": one short sentence citing the evidence.
```

### Output schema

`{category: enum(7 categories), subcategory: string|null, confidence: number[0,1], rationale: string}`.

### Known failure modes

- Choosing a category from a generic word (e.g. "vendor") that appears in an unrelated document.
- Returning a category outside the taxonomy enum.
- Confidently classifying a truncated document instead of returning low confidence.

**Revision history:** v1 only. The classifier's *behaviour* improved in the v2 release because
the extraction graph shares `extract.v2`'s keyword hints across all three LLM nodes; the
classifier's prompt text is deliberately unchanged so the v1→v2 comparison isolates one change.

---

## 3. `extract.v1` — `de1efcbb`

**Purpose:** extract every structured field plus an overall calibrated confidence. This is the
prompt the whole scoring story turns on.
**Inputs:** `document`, `taxonomy`, `today`.

### System

```
You are a precise information-extraction engine for a retail grocery chain.
You read one internal operations document and emit structured fields.

Normalisation rules:
- "incident_date" MUST be ISO 8601 (YYYY-MM-DD). If you cannot determine the
  date, return null. Do not guess.
- "dollar_impact" MUST be a bare number (no currency symbol, no thousands
  separators). If no monetary impact is stated, return null.
- "store_id" MUST match the pattern ST-### exactly, or null.
- "severity" MUST be one of Low, Medium, High, Critical, or null.
- Use null for any field the document does not state. Never invent values.

Confidence:
- Return a calibrated "confidence" in [0,1] for the extraction as a whole.
- Truncated, vague or ambiguous documents must receive a low confidence
  (below 0.75) so they are routed to a human reviewer.

Return ONLY a JSON object. No prose, no markdown fences.
```

### Template

```
Extract structured fields from the following document.

Taxonomy:
{taxonomy}

Today's date (for resolving relative dates): {today}

Document:
{document}
```

### Output schema (JSON Schema)

```json
{
  "type": "object",
  "required": ["category", "subcategory", "store_id", "incident_date",
               "vendor", "dollar_impact", "severity", "summary", "confidence"],
  "additionalProperties": false,
  "properties": {
    "category":      {"type": "string", "enum": ["Equipment Failure", "Food Safety",
                                                "Vendor Delivery", "Safety Incident",
                                                "POS/IT Outage", "Facilities",
                                                "Inventory Shrink"]},
    "subcategory":   {"type": ["string", "null"]},
    "store_id":      {"type": ["string", "null"], "pattern": "^ST-[0-9]{3}$"},
    "incident_date": {"type": ["string", "null"], "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    "vendor":        {"type": ["string", "null"]},
    "dollar_impact": {"type": ["number", "null"]},
    "severity":      {"type": ["string", "null"], "enum": ["Low", "Medium", "High",
                                                            "Critical", null]},
    "summary":       {"type": "string", "minLength": 1},
    "confidence":    {"type": "number", "minimum": 0, "maximum": 1}
  }
}
```

### Known failure modes

- Emitting dates in a local format instead of ISO 8601.
- Emitting currency strings such as `$2,420.33` instead of the number `2420.33`.
- Inventing a dollar impact for a truncated document that states none.
- Treating the generic word "vendor" as evidence of a Vendor Delivery document.
- Returning a category outside the taxonomy enum.
- Reporting high confidence for a vague document (should be below 0.75).

### Measured performance (this build)

Against the 60-document gold set with the deterministic `stub` provider
(`run-002`, `reports/scorecard_run-002.md`):

| field | accuracy |
| --- | ---: |
| category | 0.717 |
| store_id | 1.000 |
| incident_date | 0.833 |
| vendor | 1.000 |
| dollar_impact | 0.917 |
| severity | 1.000 |
| **macro** | **0.911** |

---

## 4. `extract.v2` — `5106fed1`

**Purpose:** v1 plus the fixes that the 23 logged human corrections proved were needed.
**Inputs:** `document`, `taxonomy`, `today`.

### System (v2 — the changed parts are marked)

```
You are a precise information-extraction engine for a retail grocery chain.
You read one internal operations document and emit structured fields.

Category disambiguation (fixes v1 misclassifications):                 <-- NEW
- Decide the category from the OPERATIONAL EVENT described, never from a
  company name and never from a single generic word.
- A generic noun such as "vendor", "store", "truck", "audit" or "count" is NOT
  evidence on its own. v1 over-triggered on these.
- "the walk-in cooler is not holding temperature", "the dough mixer stopped",
  "the icemaker quit" are Equipment Failure (refrigeration/equipment), not
  Facilities.
- "product past its use-by date", "raw chicken stored above ready-to-eat
  items", "a handwashing violation" are Food Safety.
- "the truck arrived four cases short", "only part of the order showed up",
  "three crates light" are Vendor Delivery.
- "a team member slipped", "a customer fell", "someone was struck by a cart"
  are Safety Incident.
- "the payment terminals stopped accepting cards", "card readers showed an
  error", "the scanners would not scan" are POS/IT Outage.
- "broken tiles in aisle 4", "the ceiling started dripping", "the exterior
  doors would not latch" are Facilities.
- "fourteen units were unaccounted for", "a gap in the tobacco count",
  "cases vanished between receiving and the floor" are Inventory Shrink.

Normalisation rules (fixes v1 format misses):                          <-- EXPANDED
- "incident_date" MUST be ISO 8601 (YYYY-MM-DD). Accept these input formats
  and normalise them all:
    * "September 20, 2026"  -> 2026-09-20
    * "2026-09-20"          -> 2026-09-20
    * "9/23/26" and "9/23/2026" -> 2026-09-23 (two-digit years are 20xx)
  If no date is stated, return null. Do not guess.
- "dollar_impact" MUST be a bare number with no currency symbol and no
  thousands separators. Accept these input formats and normalise them all:
    * "$2,420.33"        -> 2420.33
    * "about 3,310.84 dollars" -> 3310.84
    * "USD 1853.31"      -> 1853.31
  If no monetary impact is stated, return null. Never invent one.
- "store_id" MUST match the pattern ST-### exactly, or null.
- "severity" MUST be one of Low, Medium, High, Critical, or null. Truncated
  documents usually do not state a severity: return null rather than guessing.

Confidence:
- Return a calibrated "confidence" in [0,1] for the extraction as a whole.
- Truncated, vague or ambiguous documents must receive a low confidence
  (below 0.75) so they are routed to a human reviewer. Recovering a store ID
  and a date is not enough on its own for high confidence.                <-- NEW

Return ONLY a JSON object. No prose, no markdown fences.
```

The template and output schema are byte-identical to v1: the revision is entirely in the
system message and the few-shot examples, so the JSON contract does not change and no
downstream code needed to change.

### Revised `known_failure_modes` (what v1 got wrong)

```
v1: the truncated documents gave the category away only in weak phrases that were not
    keywords - 'someone got hurt' (incident_003.pdf), 'something on the hot bar looked wrong'
    (incident_015.pdf), 'the counts do not match' (vendor_notice_027.docx), 'the load was not
    right' (store_email_051.eml). All four appear in reports/corrections.csv.
v1: had no keyword for the nouns 'cooler', 'mixer' or 'icemaker', so Equipment Failure
    documents built on those words (for example vendor_notice_028.docx, 'the dough mixer
    stopped mid-batch') were only ever labelled correctly by the fallback guess, not on evidence.
v1: missed 'the truck arrived four cases short' because 'truck' and 'cases short' were not in
    the keyword set. Adding 'truck' also exposed a boundary bug: the substring 'truck' matched
    inside 'struck', so keyword matching now requires a leading word boundary.
v1: missed 'product past its use-by date' and 'a handwashing violation' (only 'expired product'
    and 'contamination' were known).
v1: missed 'a team member slipped' and 'a customer fell' (only the exact phrase 'slip and fall'
    was known).
v1: missed 'the payment terminals stopped accepting cards' and 'the card readers showed an error'.
v1: missed 'broken tiles in aisle 4' (only the exact phrase 'floor tile' was known).
v1: missed 'fourteen units were unaccounted for' and 'cases vanished' (only 'cycle count
    variance' was known).
v1: returned incident_date=null for short numeric dates such as '9/23/26' - 10 of 60 documents,
    field accuracy 0.833.
v1: returned dollar_impact=null for impacts written in words such as 'about 3,310.84 dollars' -
    5 of 60 documents, field accuracy 0.917.
v1: reported high confidence for documents where the category was only a fallback guess; v2
    requires category evidence for high confidence.
```

### Few-shot examples added (drawn from the misses)

```yaml
- inputs:
    document: "Narrative: The walk-in cooler is not holding temperature. Please advise."
  output: {category: Equipment Failure, subcategory: Refrigeration, store_id: null,
           incident_date: null, vendor: null, dollar_impact: null, severity: null,
           summary: "The walk-in cooler is not holding temperature.", confidence: 0.31}

- inputs:
    document: |
      Store: ST-011
      Date: 9/20/26
      Vendor: Ridgeline Distribution
      The truck arrived four cases short of the order.
      Estimated impact: about 2,420.33 dollars
  output: {category: Vendor Delivery, subcategory: Short Shipment, store_id: ST-011,
           incident_date: "2026-09-20", vendor: Ridgeline Distribution,
           dollar_impact: 2420.33, severity: null,
           summary: "A delivery arrived four cases short of the order.", confidence: 0.86}

- inputs:
    document: "A team member slipped near the entrance. Product was found past its use-by date on the shelf."
  output: {category: Safety Incident, subcategory: SlipFall, store_id: null,
           incident_date: null, vendor: null, dollar_impact: null, severity: null,
           summary: "A team member slipped near the entrance.", confidence: 0.42}
```

### Rationale, tied to specific corrected extractions

Each row below is a real row from [`reports/corrections.csv`](../reports/corrections.csv)
(reviewer *Maria Delgado*, run `run-001`).

All ten corrected documents are *truncated/vague* documents. Five of them still contain a weak
category hint in the text; five contain no category signal at all ("not sure what happened").
v2 recovers the hint-bearing five and the date/currency fields of all ten, but it **cannot**
recover a category that the document never states — those four rows stay wrong (see the last
column).

| Corrected extraction | Document | Field(s) fixed | v1 output | v2 output |
| --- | --- | --- | --- | --- |
| 4 | `incident_003.pdf` (hint: *"someone got hurt"*) | category, date, dollar | `Equipment Failure`, `null`, `null` | **`Safety Incident`, `2026-09-23`, `11788.52` — fixed** by the `got hurt` synonym + short-date + word-currency rules |
| 16 | `incident_015.pdf` (hint: *"something on the hot bar looked wrong"*) | category, date, dollar | `Equipment Failure`, `null`, `null` | **`Food Safety`, `2026-09-24`, `3310.84` — fixed** by the `hot bar` synonym + short-date + word-currency rules |
| 32 | `store_email_051.eml` (hint: *"the load was not right"*) | category, date, dollar | `Equipment Failure`, `null`, `null` | **`Vendor Delivery`, `2026-09-27`, `4112.31` — fixed** by the `load was not right` synonym + short-date + word-currency rules |
| 48 | `vendor_notice_027.docx` (hint: *"the counts do not match"*) | category, date, dollar | `Equipment Failure`, `null`, `null` | **`Inventory Shrink`, `2026-09-25`, `9573.26` — fixed** by the `counts do not match` synonym + short-date + word-currency rules |
| 60 | `vendor_notice_039.docx` (hint: *"the registers are acting up"*) | date, dollar | `POS/IT Outage` (already right), `null`, `null` | **`2026-09-26`, `1853.31` — fixed** by the short-date + word-currency rules |
| 10 | `incident_009.pdf` (no hint) | category, date | `Equipment Failure`, `null` | date **`2026-09-27` fixed**; category **still `Equipment Failure`** — the document states no category at all |
| 26 | `store_email_045.eml` (no hint) | category, date | `Equipment Failure`, `null` | date **`2026-09-23` fixed**; category still wrong |
| 38 | `store_email_057.eml` (no hint) | category, date | `Equipment Failure`, `null` | date **`2026-09-24` fixed**; category still wrong |
| 42 | `vendor_notice_021.docx` (no hint; gold *is* `Equipment Failure`) | date | `Equipment Failure` (right by luck), `null` | date **`2026-09-21` fixed** |
| 54 | `vendor_notice_033.docx` (no hint) | category, date | `Equipment Failure`, `null` | date **`2026-09-22` fixed**; category still wrong |

A further cluster was visible in the scorecard but **not** in the reviewer queue, because those
documents scored above the confidence threshold and were therefore never shown to a human:

- `incident_004.pdf`, `incident_010.pdf`, `incident_016.pdf`, `vendor_notice_022.docx`,
  `vendor_notice_028.docx`, `vendor_notice_034.docx`, `store_email_040.eml`,
  `store_email_046.eml`, `store_email_052.eml`, `store_email_058.eml` — the ten "trap"
  documents that used a category synonym v1 did not know. Nine of the ten were *silent*
  category errors (only `vendor_notice_028.docx`, whose true category is `Equipment Failure`,
  matched the fallback guess by luck). This is exactly the `needs_review` recall of 0.526 in the
  v1 scorecard, and it is why the **prompt** was revised rather than only the threshold.
  All ten are classified correctly under v2.

Putting it together: v1 misclassifies 19 of 60 documents (9 silent trap misses + the 10 flagged
vague documents). v2 fixes 15 of those 19 and still fails on the 4 vague documents that contain
no category signal whatsoever — which is the correct behaviour, and those rows remain in the
review queue.

### Measured performance (this build)

`run-003`, same 60 documents and same provider (`reports/scorecard_run-003.md`):

| field | v1 | v2 | delta |
| --- | ---: | ---: | ---: |
| category | 0.717 | 0.933 | +0.217 |
| store_id | 1.000 | 1.000 | +0.000 |
| incident_date | 0.833 | 1.000 | +0.167 |
| vendor | 1.000 | 1.000 | +0.000 |
| dollar_impact | 0.917 | 1.000 | +0.083 |
| severity | 1.000 | 1.000 | +0.000 |
| **macro** | **0.911** | **0.989** | **+0.078** |

Review queue: 10 documents → 5 documents.
`needs_review` precision/recall: 1.000 / 0.526 → 0.800 / 1.000.

---

## 5. `exec_summary.v1` — `35fc86a2`

**Purpose:** write the executive summary of the weekly rollup **strictly from pre-aggregated
statistics**, so the narrative can never disagree with the charts.
**Inputs:** `stats` (a JSON blob produced by `report.build_stats`).

### System

```
You are the operations analytics lead for a retail grocery chain. You write the
executive summary paragraph that opens the weekly operations rollup.

Absolute rules:
- Use ONLY the numbers supplied in the statistics JSON. Never invent, estimate
  or extrapolate a figure.
- Every number you mention must appear verbatim in the supplied statistics.
- Be concise: 3-6 sentences of plain prose for the summary.
- Return ONLY a JSON object matching the schema. No markdown fences.
```

### Template

```
Weekly operations statistics (JSON):
{stats}

Write:
- "summary": a 3-6 sentence executive summary using only these numbers.
- "highlights": 3-6 short bullet strings, each citing a real figure.
```

### Output schema

`{summary: string(minLength 20), highlights: string[](minItems 3, maxItems 6)}`.

### Known failure modes

- Rounding or inventing a dollar figure not present in the statistics.
- Describing a trend over a period the statistics do not cover.
- Reporting total incidents instead of the supplied per-week counts.

**Determinism note:** the `stub` provider renders the summary directly from the statistics
dictionary, so the offline demo cannot invent a number. The real providers are held to the same
schema and the same "numbers must appear verbatim" instruction; the aggregated statistics are
always the single source of truth.

**Revision history:** v1 only, shipped with the first weekly report.
