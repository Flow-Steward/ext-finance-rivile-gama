# Rivile GAMA integration for Flow Steward

<img src="icon.png" width="96" alt="Rivile GAMA integration for Flow Steward">

[![CI](https://img.shields.io/github/actions/workflow/status/Flow-Steward/ext-finance-rivile-gama/ci.yml?branch=main&label=CI)](https://github.com/Flow-Steward/ext-finance-rivile-gama/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Flow-Steward/ext-finance-rivile-gama)](https://github.com/Flow-Steward/ext-finance-rivile-gama/releases/latest)

Automate Rivile GAMA with Flow Steward workflows and AI agents: create invoices, clients and products, register payments and read stock — without writing code.

This Flow Steward extension is an accounting integration for companies in Lithuania that work
with Rivile GAMA. It connects Rivile GAMA to workflow automation through the Rivile API (REST
API v2 and Manorivile-Gateway), so invoices, clients, items and stock come from any system
without retyping.

## What is this

Rivile GAMA is an accounting and business management system by Rivile, Lithuania, installed at
the company and reached through Rivile's API service and Manorivile-Gateway. This extension is a
connector: it adds ready workflow steps to Flow Steward, and the same steps are available as tools
for AI agents. Learn more about Flow Steward at
[docs.flowsteward.org](https://docs.flowsteward.org).

## What you can automate

- Create sales invoices in Rivile GAMA from orders in your shop — the customer is found or created, no retyping.
- Enter supplier invoices as purchase invoices and attach the original PDF to the document.
- Register payments against invoices from your bank or payment provider data.
- Send stock levels and product prices from Rivile GAMA to your online shop.
- Pass clients, products, prices and stock changed since the last run to another system.
- Check a client's debt before a new order is shipped.
- Export any Rivile list to a file for reporting or an exchange with another system.

## Why teams use it

- **No duplicate records.** Clients and products are looked up before they are created, an invoice number Rivile already has for the client is not created again, and a payment with the same reference is not registered twice.
- **Exact amounts.** Amounts and quantities are handled as exact decimals, never rounded through floating point.
- **A change is never sent twice.** When the answer to a write is lost, the step reports the outcome as unknown and does not retry it.
- **A test run changes nothing.** Every write is skipped in test runs, so you can try a workflow safely.
- **The whole API.** Each of the 209 methods of the Rivile GAMA API v2 has its own step.
- **The key stays secret.** It is stored encrypted, never echoed and removed from Rivile's error texts.

## What's inside

Everyday steps:

- **Test the connection** — checks the API key and the gateway with one read.
- **Find or create customer / supplier** — by company code, VAT code or client code.
- **Create sales invoice**, **Create purchase invoice** — with lines in one request, optionally posted; a failed line rolls the whole document back.
- **Attach file to document**, **Download document file** — invoice PDFs and other files as workflow files.
- **Register payment** — against a sales or purchase document, checked for repeats.
- **Find or create product** — by code or barcode.
- **Read stock levels**, **Read product prices** — per product and warehouse, with every price list and discount.
- **Read records changed since a date** — clients, products, prices, stock or documents.
- **Read client debt** — debit, credit and balance.

All 209 Rivile GAMA API v2 methods are available as steps (223 steps in total, with **Call any Rivile method** and **Export a Rivile list to a JSONL file**): purchases and sales, general ledger, debts and payments, products and services, inventory and stocktaking, production, fixed assets, prices, payroll, personnel, point of sale, loyalty and gift vouchers, contracts and document management. The [workflow action guide](docs/workflow-actions.md) shows examples.

## Get started

1. In Flow Steward, open **Extensions → Catalog** and install **Rivile GAMA**.
2. In the extension's **Connection** page, add a connection named after the company and paste the Rivile API key (`username.secret`) that came with the Rivile API service.
3. Press **Test connection**.
4. Build your first workflow, for example **Create sales invoice** from a new shop order.

## Requirements

- The Rivile API service ordered from Rivile, with a valid version access agreement, and Rivile GAMA 119 or later.
- Manorivile-Gateway installed on a Windows computer next to the Rivile GAMA database, with outbound access to `gateway.manorivile.lt`, port 5671.
- An API key (`username.secret`). The steps work with that user's Rivile rights; the `RGI_*` parameters control editing others' documents, payroll and personnel access.
- To read changes since a date for clients, products, prices or stock, run **Enable change tracking** (`INIT_TRIGGER`) once first.
- Run find-or-create steps for the same client or product one at a time.
- Flow Steward 0.9.16 or later.

## FAQ

**Can I create invoices in Rivile GAMA from an online shop?**
Yes. **Create sales invoice** finds or creates the customer and creates the invoice with its lines in one request; it can post the document too.

**Do I need Manorivile-Gateway for the Rivile API?**
Yes. The Rivile API reaches the Rivile GAMA database through Manorivile-Gateway, installed next to the database.

**How do I sync stock and prices from Rivile GAMA to my e-shop?**
Use **Read stock levels** and **Read product prices**, or **Read records changed since a date** to send only what changed since the last run.

**What if I need a Rivile method that has no everyday step?**
Every Rivile GAMA API v2 method has its own step, and **Call any Rivile method** sends any method with its parameters and data.

**Will running a workflow again create the same invoice twice?**
No. When Rivile GAMA already has an invoice with that number for the client, the step does not create a second one.

## In your language

Rivile GAMA integracija su Flow Steward per Rivile API automatizuoja apskaitą Lietuvoje: pardavimo ir pirkimo sąskaitos, klientai, prekės, mokėjimai ir likučiai keliauja iš bet kurios sistemos be rankinio įvedimo. Plėtinys apima visus 209 Rivile GAMA API v2 metodus.

## Links

- [Flow Steward documentation](https://docs.flowsteward.org)
- [Flow Steward extension catalog](https://github.com/Flow-Steward/extensions)
- [Issues](https://github.com/Flow-Steward/ext-finance-rivile-gama/issues)
- [Rivile API guide](https://gidas.rivile.lt/internetiniai_moduliai/api_rest)

## For developers

The contracts are generated from the Rivile reference; do not edit them by hand.

```bash
python3 tests/reference/build_contracts.py   # regenerate from the Rivile reference
```

From a Flow Steward checkout:

```bash
./flow-steward extensions validate flowsteward.rivile-gama --root <parent> --health --marketplace
./flow-steward extensions test flowsteward.rivile-gama --root <parent> --non-interactive
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root <abs path>
```

Rivile, Rivile GAMA and their marks belong to their owner; see [BRANDING.md](BRANDING.md).
