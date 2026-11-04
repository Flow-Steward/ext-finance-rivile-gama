# Changelog

## 0.1.0 — unreleased

First release, not yet verified against a live Rivilė GAMA key (1.0.0 follows that check).

### Everyday operations

- Eleven everyday operations, listed first in the step picker: Find or create customer / supplier,
  Create sales invoice, Create purchase invoice, Attach file to document, Download document file,
  Register payment, Find or create product, Stock levels, Product prices, Changes since, Client debt.
- The 209 raw methods and Call any Rivile method are named "Advanced · …" (the step picker lists
  operations flat, by name; it has no grouping field).
- Composite writes report exactly what was created when a later call fails, and are not retried.
- Own connector icon; listing categories finance, inventory, hr.
- Pages: a one-minute Setup Guide (one block for whoever sets up Rivilė and the gateway, one for
  whoever connects in Flow Steward); Connection with "How to get the API key"; "Rivilė API —
  operations" (each everyday step: Rivilė methods, inputs, results, and how steps behave);
  "Rivilė API — advanced methods" (every method by area, with coverage); Changelog. No tables.

- Review round 3: Rivilė fields read case-insensitively (client debt was always 0 because T03 answers
  in lower case); explicit `foreign_currency` (a base-currency code keeps net/gross price fields);
  several matches or conflicting identifiers refused before any write; `KIEKIS_U` sent with all three
  unit codes; codes allowlisted before they enter a filter; invoices with a number looked up first
  (`created` output); running out of time after a write reported as such; a created client without
  an echoed code read back; no posting without a document code; test-mode results carry every
  declared output; Decimal money and quantities with Rivilė precision and bounds; stock with
  `alt_unit`; prices changes for every product; documented datetime form; `Advanced ·` export.

### Complete Rivilė GAMA API v2 coverage and hardening

Review fixes before the first release:

- Round 2: record_locked only on HTTP 423 or a 400 whose errorMessage starts "423:", never over
  an ambiguous 5xx write; one step deadline (600 s budget) for every request and backoff, with
  fetch_all/export stopping in time and reporting next_page/resume_page; export saves what it has
  at 200 MB; the API key is redacted from raw bodies before parsing; whole results metered,
  oversized write echoes shortened instead of failed; linear error recovery.

- `get_i06_doc` / `export_list` write files under the exact granted content type; the Rivilė
  file name/type are reported as `original_filename` / `original_content_type`.
- Writes answered 502/503 are `timeout_unknown` and not retried; reads stay retryable.
- The main table is no longer duplicated (`tables` → `other_tables`); multi-page reads stop at the
  last page that fits the host's secret scan and output cap and report `next_page`.
- `EDIT_PAP` sends its fields in `data.PAP`; `GET_USER_PROC` is a write (external effect).
- 429 and 423 (record locked) back off 1/2/4/8 s; DNS failure and IncompleteRead are connection
  failures; failures before sending are `definitely_no_external_effect`.
- `export_list` stops before the step time limit, saves what it read, returns `resume_page`;
  `start_page` resumes it. No second in-memory copy of the file.
- `show_doc` defaults to 0 for EDIT_I06_DOC(S)/GET_I06_DOCS and write results never carry file
  content; metadata-only updates (oper U) need no file; unknown upload types need `ext`.
- 400 bodies up to 4 MB are parsed, and dataErrors are recovered from cut bodies.
- Required params enforced (GET_VIEWS_LIST, GET_I06_DOC(S), EDIT_I06_DOC(S), EDIT_BUS with its
  module list, GET_USER_PROC, GET_I06_DEBT); `list` values per method (GET_N17_LIST has F);
  `oper` on the FULL methods that document it; `top` sets the page size.
- Data schemas: field lists and descriptions from the guide's field tables, one record per request.
- Trailing padding trimmed, leading spaces kept; Rivilė error text redacted of the API key.
- Category `finance` (+ inventory, hr); own connector icon (sheet with a stylised R), not the Rivilė mark.

Initial scope:

- All 209 methods of the Rivilė GAMA API v2 guide as typed workflow operations
  (91 read, 118 write), generated from the guide.
- `test_connection`, `call_method` (any method) and `export_list` (JSONL export).
- API-key connection type; fixed endpoint `api.manorivile.lt`.
- Writes declared as external effects, suppressed in test runs.
- Rivilė `dataError` details in failures; HTTP 207 as partial success.
- Paged reads with Rivilė rate-limit pacing.
- Document attach from workflow files (`EDIT_I06_DOC`/`EDIT_I06_DOCS`) and download to a workflow file (`GET_I06_DOC`).
