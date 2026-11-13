# Rivile GAMA workflow actions

Choose an everyday action for common invoices, clients, products and payments.
Choose **Advanced ·** actions when you need a particular provider method or record shape.
Action IDs in parentheses remain compatible with existing workflows.

## Connect and check access

Enable the Rivile API service, run Manorivile-Gateway beside the company database and add the username.secret API key to a project connection. Confirm the Rivile user permissions and company-specific RGI settings. Run **Test the connection** (`test_connection`). For incremental readers, run **Advanced · Enable change tracking (INIT_TRIGGER)** once where required before relying on change results.

## Create an invoice from an approved order

1. Add **Create sales invoice** (`create_sales_invoice`) and select the project connection.
2. Set **Client code** to an existing client, or give the company/VAT code and name for a missing client. Set **Number** to the source document number, **Date** to `2026-01-15` and the company department/warehouse as needed.
3. Set **Lines** to a JSON array, binding values from earlier steps as needed:

   ```json
   [{"product_code":"PRODUCT01","quantity":2,"unit":"VNT","unit_price":12.5,"department_code":"MAIN"}]
   ```

Replace PRODUCT01, VNT and MAIN with existing product/service, unit and department codes. Each line requires a department here or on the document. Check the connection defaults for account relations and tax tables. Leave **Post the document** disabled for an unposted document. A failed detail line rolls back the document creation; later posting failures require inspection before repeating. **Register payment** uses the Rivile document code returned by creation, not the printed invoice number.

Use the purchase invoice/vendor bill action for supplier documents; check its
partner, product and tax settings before execution. File inputs accept workflow
files from earlier steps. Download actions return workflow files for later email,
archive or document steps, rather than local filesystem paths.

## Read stock, prices, balances and changes

- **Read stock levels** (`stock_levels`): select a product and the warehouse/location filters needed for the company.
- **Read product prices** (`product_prices`): read product pricing; client/pricelist options depend on the provider.
- **Read client debt** (`client_debt`): read existing debt/balance information before a follow-up workflow.
- **Read records changed since a date** (`changes_since`): choose **What** and set **Since** to `2026-01-01T00:00:00`.

Start pages at 1 and pass `next_page` while `has_more` is true. Keep the same entity/date and review the documented change-tracking requirement for that entity.

**Advanced · Export a Rivile list to a JSONL file** (`export_list`) reads a paged list method into a workflow file. Each JSON line carries its table in `_table`; use `resume_page` when an export is incomplete.

## Work with advanced records

**Advanced ·** actions include the exact Rivile method code. `GET_…` methods generally read tables; `EDIT_…` methods take **Record operation (oper)** and Rivile **Data** blocks such as N08 or I06. Use the selected method’s table fields and supported operation values, not everyday-action input names. **Record filter (T-SQL)** is a WHERE condition without WHERE; for **Advanced · Clients: list (GET_N08_LIST)**, a client-code filter can be `n08_kodas_ks='CLIENT01'`. Use the correct table for other methods. The pinned Rivile guide retains original field descriptions for accounting reference.

Read actions still contact the provider during a workflow test. Write actions are
suppressed in test mode. A lost response after a write can leave an unknown
outcome; inspect provider records and reported effects before running it again.
