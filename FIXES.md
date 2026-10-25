# Billing fixes — before, after, and verification

Scope: existing FreshThreads Flask application. No redesign, new payment provider, subscription expiry rules, automatic bank verification, database schema change, or historical data rewrite. The existing local port change to 5001 is preserved.

## Confirmed business rules

The user confirmed that Sales means the full bill value after discount and express charge, before wallet/advance, and that cancelling a spent subscription credit must be blocked until the wallet is settled.

## 1. Bill totals and input validation

**Before:** the server accepted the browser's submitted line total, while invoices independently multiplied quantity by rate. The built-in customer payment page ignored wallet and advance. The staff invoice also did not show advance or remaining amount due. Discount edits ignored express charges and wallet deductions.

**After:** new line totals are calculated from quantity × rate on the server with decimal rounding to paise. All HTML invoice variants, PDF export, and the customer API use one calculation. Existing line totals are retained as historical values. Invoices show the final bill, advance, and Amount Due separately. Paid/cancelled bills have zero Amount Due. Discount edits retain express charges, wallet use, and advances. An edit cannot hide wallet money already used; the existing cancel/recreate workflow restores that money. Fixed the edit dropdown so a rupee discount remains a rupee discount.

Invalid/negative/non-finite amounts, mismatched service arrays, fractional garment counts, empty service/customer fields, and percentages over 100 are rejected before saving. Explicit request failures roll back customer, bill, item, and wallet writes together. Staff can still set rates, use decimal weights, add discounts/express charges, and mark bills paid manually.

**Example:** full bill INR 500, wallet INR 200, advance INR 100 → Final Bill INR 300, Amount Due INR 200. Before, the built-in customer payment page requested INR 500. After, it requests INR 200.

**Checks:** tampered line total, decimal rounding, fixed/percentage adjustments, wallet/advance combination, historical line-total preservation, discount edits, invalid input, and a simulated database failure.

## 2. Subscription credits and wallet concurrency

**Before:** a top-up created already Paid did not credit the wallet. A later “Mark Paid” did. The frontend identified top-ups by a phrase in their name, while the server used the subscription-products table. Concurrent requests could repeat a credit or refund.

**After:** both paid-at-creation and later payment call the same once-per-bill credit function. The frontend uses the server's subscription-product mapping. Bills containing top-ups still never spend the wallet. Payment, billing-wallet deduction, and cancellation use serialized database transactions so simultaneous actions cannot double credit/refund or overspend.

**Checks:** paid and pending top-ups, repeated/concurrent Mark Paid, named top-up without the old phrase, concurrent normal bills with limited wallet credit.

## 3. Cancellation

**Before:** cancellation restored wallet deductions but did not remove credits earned from a subscription purchase. Cancelled bills could later be marked paid/delivered. The built-in customer page did not suppress payment for cancelled bills.

**After:** a normal bill refunds its wallet deduction once. A credited top-up removes that credit once. If available funds cannot cover the reversal, cancellation returns a clear conflict and changes nothing. The existing `ADJUST` ledger type records removed subscription credit with an explicit description; no schema change is needed. Cancelled bills cannot be marked paid/delivered or edited, and their customer invoice suppresses payment. Repeated cancellation is harmless. Missing bills return 404 instead of false success.

**Checks:** normal refund, repeated/concurrent refund, unspent credit reversal, spent-credit rejection with unchanged records, missing/cancelled bill actions, cancelled customer invoice.

## 4. Staff access and existing report password

**Before:** many staff read/write APIs had no login protection. The outstanding-report password was checked only by the page, so callers could bypass it. Login passwords and the signing key were hardcoded in source. Cross-origin access covered every API.

**After:** business APIs and exports require a staff session. The existing outstanding-report password now grants a five-minute server-checked report session. Customer token links remain public. Password hashes and a persistent session key live in ignored private configuration or server environment variables. The installer preserves the existing local username/passwords, stores hashes, and generates a fresh session key. Existing weak passwords have not been silently replaced; rotate them before public deployment.

CSRF tokens protect staff forms and JSON/jQuery changes. Cross-origin changes are rejected. Service deletion now uses a protected POST; old delete links cannot mutate records. Customers have no delete control in the active template, and the old hidden GET-delete path is disabled. Generated financial exports under static paths require login; new exports are sent from memory. Only configured customer origins can read token-scoped bill information. Sensitive responses are not cached. User-entered text is escaped in generated report markup.

**Checks:** every staff API rejects unauthenticated requests, public customer links still work, report password enforcement, login with configured hashes, CSRF/origin rejection, POST-only service deletion path, and private export paths.

## 5. Sales and outstanding reporting

**Before:** Sales summed the balance after wallet/advance, understating bill value. A paid but undelivered order could still display its original balance as outstanding money. Some daily exports included cancelled bills. Voided visits were counted in customer visit totals. “New this month” counted everyone who visited that month.

**After:** Sales uses the full bill value you confirmed, consistently across daily/range/date-grouped totals, excluding cancelled bills. Paid but undelivered orders remain in the work queue with zero money due. Daily exports exclude cancelled bills. Customer visit counts exclude voided bills, and the monthly new-customer tile counts first non-void visits in that month. VIP thresholds and lapsed-customer thresholds are unchanged.

**Checks:** sale with discount/express/wallet/advance, cancellation exclusion, paid delivery queue, daily/range Excel contents.

## 6. PDF and Excel export

**Before:** PDF rendering omitted required template values, referenced an output variable before assignment, and used a Windows executable path. Excel export depended on local output folders and one export lacked column names. Some dependencies were absent from requirements.

**After:** ReportLab creates a real, paginated PDF directly from the shared invoice values, including wallet, advance, Amount Due, and paid/cancelled status. It needs no external program. PDF layout is simple A4; the existing HTML print layout is preserved. Currency is labelled INR. Excel downloads are generated in memory with explicit headings and formula-like customer text kept as text. Requirements include the PDF generator, Flask-Cors, and Gunicorn. The original Flask/pandas dependency pins are retained and verified with Python 3.11.

**Checks:** actual PDF parsing and amount extraction, multipage PDF with long service names, special characters, real Excel workbook reading, empty/cancelled exports, and a clean dependency installation.

## 7. Placeholder bill-generation endpoint and database handling

**Before:** `/api/generate-bill` returned a fake invoice ID without saving a bill. The active billing form already posts to `/billing`; its legacy JavaScript handler targets an absent form ID. Some database connections remained open after requests. The database path depended on the shell's current folder.

**After:** the unused placeholder explicitly says to use the real billing form instead of claiming a saved bill. The active billing form and invoice redirect remain unchanged. Request teardown closes connections/rolls back unfinished transactions, and the database path resolves relative to the application (or an explicit configured path). Missing private settings produce a clear startup error. No business database initialization/migration runs automatically.

**Checks:** placeholder cannot claim success, rollback after failed write, connection lifecycle through the route checks.

## Validation results

37 automated regression tests pass on a clean Python 3.11 environment using the exact requirements. The earlier 31-test set also passed with the existing Python 3.13 environment plus temporary missing dependencies. Tests use disposable databases containing synthetic records.

Before/after reproduction on the original code with synthetic data confirmed:

| Scenario | Before | After |
|---|---|---|
| INR 500 bill, INR 200 wallet, INR 100 advance | Saved balance 200; customer page requested 500 | Invoice/API Amount Due 200 |
| Same bill, Sales report | 200 | 500 |
| Unauthenticated Mark Paid | HTTP 200 | HTTP 401 |
| Top-up created Paid | Zero wallet credit entries | One credit, even after repeated payment actions |
| PDF export | Rendering exception | Real PDF with matching amounts |

Browser verification on a disposable preview: staff login, billing screen, wallet preview, bill submission, saved invoice, and discount edit. Synthetic invoice screenshot is available beside this change report in the original task workspace.

## Deployment boundaries and remaining integration

No live server was deployed or restarted, and no business database records or schema were changed. Code rollback copies are retained locally. Existing historical payment/wallet mistakes are not automatically corrected; they need a separate reconciliation with actual receipts and wallet history.

Before a deployment, configure private server settings and the exact customer-site origin described in README.md. The separate `pay.freshthreadslaundry.co.in` frontend is outside this repository. Its existing fields remain compatible, but it must use the new `amount_due` for UPI and honor `is_cancelled`; this repository cannot guarantee that external site's displayed payment amount. Automatic confirmation of UPI receipts remains outside the app, as before.
