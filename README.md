# Rivilė GAMA for Flow Steward

`flowsteward.rivile-gama` connects Flow Steward workflows to **Rivilė GAMA REST API v2**
(`https://api.manorivile.lt/client/v2`). Every method documented in the Rivilė guide
(<https://gidas.rivile.lt/internetiniai_moduliai/api_rest>) is a typed workflow operation:
**209 methods** — 91 reads and 118 writes — plus:

| Operation | What it does |
|---|---|
| `test_connection` | Checks the API key and the Manorivile-Gateway with `GET_N45_LIST`. |
| `call_method` | Sends any Rivilė method with raw params and data (for methods Rivilė adds later). Treated as a write. |
| `export_list` | Reads any list method page by page and saves every record to a JSONL workflow file. |

## Requirements on the Rivilė side

- The Rivilė API service ordered, a valid version access agreement, Rivilė GAMA 119+.
- Manorivile-Gateway installed next to the GAMA database and running.
- An API key (`username.secret`). The username's Rivilė rights apply; the `RGI_*`
  parameters in Rivilė GAMA control editing others' documents, payroll and personnel access.

## How operations map to Rivilė

- Operation id = Rivilė method in lower case: `EDIT_N08` → `edit_n08`, `GET_I06_LIST` → `get_i06_list`.
- Every documented param is a typed input (`oper`, `fil`, `list`, `error_action`, `page_number`, …);
  `data` is Rivilė's data block with Rivilė's field names; `extra_params` passes undocumented params.
- Outputs: `records`, `record`, `record_type`, `other_tables`, `count`, `data_errors`, `partial_success`,
  `http_status`, `duration_ms` (+ `page_number`, `next_page`, `pages_fetched`, `has_more` for lists).
- Lists: `page_number`, or `fetch_all_pages` + `max_pages`. Reads stop at the last page that fits one
  step output (the host's secret-scan budget and the 4.5 MiB output cap) and report `next_page`.
- Files: `edit_i06_doc` / `edit_i06_docs` take a workflow file in `document_file` (sent with
  `show_doc` 0); `get_i06_doc` saves the attachment as a workflow file (`rivile-document.bin`,
  `application/octet-stream`; the Rivilė name/type are in `original_filename` / `original_content_type`).
- `export_list` stops reading after 360 s, saves what it has, and returns `complete: false` with
  `resume_page` (pass it as `start_page`).
- Page param: the guide spells it `pagenumber` on most pages and `pageNumber` on a few (`GET_N07_LIST`,
  `GET_N09_LIST`); each operation sends the spelling its own page documents. Not verified against a live key.

## Writes and failures

All writes (`EDIT_*`, `INIT_TRIGGER`, `GET_USER_PROC`, `call_method`) are declared external effects:
recorded in the host ledger, suppressed in test runs. HTTP 400 fails with Rivilė's `dataError` details
and no effect; 207 succeeds with `partial_success: true`; 429 and 423 (record locked) are retried with
1/2/4/8 s backoff; on reads 502/503 are retryable; on writes a timeout or 500/502/503/504 is
`timeout_unknown` and is not retried. Pacing is per process (per step).

Responses are parsed as XML with `defusedxml` (no DTDs or entities). The API key is never echoed, and is
redacted from any Rivilė error text. Trailing padding is trimmed; leading spaces (significant in Rivilė
codes) are kept.

## Development

The contracts are generated; do not edit them by hand.

```bash
python3 tests/reference/build_contracts.py          # regenerate from the Rivilė reference
```

`tests/reference/` holds the reference pipeline: `html_to_text.py` (guide pages → text),
`parse_docs.py` (text → `rivile_api_v2_spec.json`), `titles.py` (English titles and groups) and
`build_contracts.py`. From the Flow Steward application checkout:

```bash
./flow-steward extensions validate flowsteward.rivile-gama --root <parent> --health --marketplace
./flow-steward extensions test flowsteward.rivile-gama --root <parent> --non-interactive
./flow-steward extensions lock-deps flowsteward.rivile-gama --root <parent> --non-interactive --confirm
./flow-steward extensions bundle flowsteward.rivile-gama --root <parent> --with-wheels --non-interactive --confirm
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root <abs path>
```
