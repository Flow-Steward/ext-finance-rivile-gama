# Rivilė GAMA for Flow Steward

`flowsteward.rivile-gama` connects Flow Steward workflows to **Rivilė GAMA REST API v2**
(`https://api.manorivile.lt/client/v2`). Every method documented in the Rivilė guide
(<https://gidas.rivile.lt/internetiniai_moduliai/api_rest>) is a typed workflow operation:
**209 methods** — 91 reads and 118 writes — plus eleven everyday operations (below) and:

| Operation | What it does |
|---|---|
| `test_connection` | Checks the API key and the Manorivile-Gateway with `GET_N45_LIST`. |
| `call_method` | Sends any Rivilė method with raw params and data (for methods Rivilė adds later). Treated as a write. |
| `export_list` | Reads any list method page by page and saves every record to a JSONL workflow file. |

## Everyday operations (start here)

| Operation | Rivilė calls |
|---|---|
| `find_or_create_client` | `GET_N08_LIST` by company code → VAT code → client code; `EDIT_N08` only if none matches |
| `create_sales_invoice` / `create_purchase_invoice` | optional client find-or-create, `EDIT_I06_FULL` (`errorAction` ROLLBACK), optional `EDIT_I06` oper P |
| `attach_file_to_document` | `EDIT_I06_DOC` with a workflow file (`show_doc` 0) |
| `download_document_file` | `GET_I06_DOC` → workflow file |
| `register_payment` | `EDIT_I13` |
| `find_or_create_product` | `GET_N17_LIST` / `GET_N37_LIST` / `GET_N40_LIST`; `EDIT_N17_FULL` with an `N40` barcode only if none matches |
| `stock_levels` | `GET_I17_LIST` grouped by department, decimal quantities |
| `product_prices` | `GET_PS_KAIN`, flattened |
| `changes_since` | `GET_CLIENTS_TIME`, `GET_PROD_BY_BARCODE_TIME`, `GET_PRICES_TIME`, `GET_I17_LIST_TIME` (need `INIT_TRIGGER`), documents via `GET_I06_LIST` on `I06_R_DATE` |
| `client_debt` | `GET_T03_LIST`: debit, credit, balance |

A composite step that fails after an earlier write (for example the client was created, then the
invoice refused) fails with "may have an effect", lists what was already created, and is not retried.
A failed posting after a created invoice returns success with `posted: false` and `post_error`.

### Mapping assumptions — unverified without a live key

Taken from the guide's field tables and examples, not yet confirmed against a real Rivilė database:

1. `I06_OP_TIP` 51 = sales invoice (Pardavimai: Važtaraštis), 1 = purchase invoice (Pirkimai: Važtaraštis).
2. Posting = `EDIT_I06` with `oper` P and `I06_KODAS_PO` ("perkėlimas").
3. Line quantity in decimals via `KIEKIS_U` (as in the `EDIT_I06_FULL` example) rather than `I07_KIEKIS`/`I07_FRAKCIJA`.
4. Net price `I07_KAINA_BE`; gross `I07_KAINA_SU` with `I06_PVM_TIP` 1; with a currency, `I07_VAL_KAINA`.
5. VAT: `vat_rate` → `I07_MOKESTIS` 1 + `I07_MOKESTIS_P`; `vat_code` → `I07_KODAS_KL` (Rivilė 109+); neither → Rivilė's
   default from the product/client tax table (`I06_KODAS_XS` "fills from the client card").
6. Due date → one `I08` term: 100 % after N days from the document date.
7. Defaults from the guide's examples, company-specific in practice: client account relation `PT001`
   (`N08_KODAS_DS`), product account relation `PR001` (`N17_KODAS_DS`), tax table `PVM` (`N08_KODAS_XS_P/T`).
8. `N08_RUSIS` customer 1 / supplier 2 / both 3; `N08_TIPAS` legal entity 1 / person 2. Rivilė has no
   country field; `location_code` maps to `N08_KODAS_VS`.
9. New product barcode as an `N40` alternative barcode (the `EDIT_N17_FULL` example), not `N37_BAR_KODAS`.
10. Payments via `EDIT_I13` on the document; `I13_KODAS_SS` is the company's money account code.
11. Stock quantity = `des_likutis_us` when Rivilė sends it, else `I17_KIEKIS / I17_FRAKCIJA`.
12. `lastDate` accepts `YYYY-MM-DD HH:MM[:SS]` as well as the documented `YYYY-MM-DD`; documents changed since a
    date are read by `i06_r_date >= …` (there is no `*_TIME` method for documents).
13. Debt `balance` = `T03_SUMA_DB − T03_SUMA_CR`; which side means "the client owes us" is not stated.

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
