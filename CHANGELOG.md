# Changelog

## 1.0.0

- All 209 methods of the Rivilė GAMA API v2 guide as typed workflow operations
  (91 read, 118 write), generated from the guide.
- `test_connection`, `call_method` (any method) and `export_list` (JSONL export).
- API-key connection type; fixed endpoint `api.manorivile.lt`.
- Writes declared as external effects, suppressed in test runs.
- Rivilė `dataError` details in failures; HTTP 207 as partial success.
- Paged reads with Rivilė rate-limit pacing.
- Document attach from workflow files (`EDIT_I06_DOC`/`EDIT_I06_DOCS`) and download to a workflow file (`GET_I06_DOC`).
