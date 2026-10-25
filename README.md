# Fresh Threads Laundry Billing

Flask/SQLite staff application for bills, customers, services, prepaid wallets, delivery tracking, reports, and expenses.

## Run locally

The exact requirements are verified with Python 3.11. Create a Python 3.11 virtual environment, then:

```sh
python -m pip install -r requirements.txt
python configure.py
python app.py
```

The local app uses port **5001**. `configure.py` is for a new installation; skip it if `instance/config.py` already exists. The settings file contains a persistent session key and password hashes and must stay private. It is ignored by Git.

The existing local login and report password were preserved during the fix installation; they are now hashes in private settings. A fresh session key signs new logins, so log in again after restarting. Rotate inherited weak passwords before deploying publicly by replacing the corresponding hashes with `werkzeug.security.generate_password_hash` output.

The app expects the existing database at `database/fresh_threads.db`. It does **not** automatically create or migrate the business database. Restore a known-good backup for a new installation. Never run the test fixture schema against your business database.

## Change both local passwords

Run `python configure.py --change-passwords` in the activated project environment. Enter and confirm the new staff password, then the new reports password. Input is hidden. Both update together only after confirmation; the username stays the same. Restart the app and log in again. The command refuses to change local passwords when active `FT_*` environment overrides would take precedence.

## Deployment configuration

Environment values override the private local settings:

- `FT_SECRET_KEY`: a strong, persistent, private session key.
- `FT_LOGIN_USERNAME`: staff username.
- `FT_LOGIN_PASSWORD_HASH`: Werkzeug password hash for staff login.
- `FT_ADMIN_PASSWORD_HASH`: Werkzeug password hash for the outstanding-report password.
- `FT_DB_PATH`: optional absolute database path.
- `FT_COOKIE_SECURE=1`: set when served through HTTPS.
- `FT_PUBLIC_ORIGINS`: comma-separated exact origins for the separate customer invoice site. Needed only for cross-origin browser requests to `/api/bill-info/<token>`. No cross-origin access is granted to staff APIs.

For the payment website currently linked by the invoices, configure `FT_PUBLIC_ORIGINS=https://pay.freshthreadslaundry.co.in` on the backend. Add any other real customer-site origin explicitly if used. The local settings file is not uploaded by Git, so configure these secrets on the server before deploying this version.

`render.yaml` describes a Render deployment but is not evidence of where the app currently runs. This change does not deploy or restart a server. Preserve the current persistent database storage and backup process.

PDF export now uses ReportLab. It creates a PDF directly and needs no Windows executable or wkhtmltopdf installation. Amounts and customer/order details come from the same calculation as HTML invoices; the PDF uses a simple paginated A4 layout and spells currency as INR. Existing on-screen/print invoice styling remains unchanged.

## Amount meanings

- **Subtotal:** sum of saved line totals. For new bills each line is quantity × rate, rounded to paise on the server.
- **Sales / full bill value:** subtotal minus discount plus express charge, before wallet and advance.
- **Final Bill / final_payable:** full bill value minus wallet usage, before advance. This existing API field keeps its meaning for compatibility.
- **Balance Amount:** final bill minus advance, clamped at zero; this records the balance calculated at billing/edit time.
- **Amount Due / amount_due:** balance still to collect; zero for paid or cancelled bills.

Payment status and delivery status remain separate. “Mark Paid” does not rewrite the original balance or record a bank receipt. No automatic UPI payment verification is added.

The separate payment website is outside this repository. Its API now includes `amount_due`, `advance_paid`, `balance_amount`, and `is_cancelled` alongside the existing fields. That website must use `amount_due` for UPI payment and suppress payment for paid/cancelled bills. Its source and deployed behavior were not changed here.

## Wallet rules

A paid subscription top-up credits the configured amount once. A normal bill automatically uses available wallet credit, but a bill containing an active subscription product never spends the wallet. The frontend now recognizes subscription products using the same database mapping as the backend.

Cancelling a normal bill restores its wallet deduction once. Cancelling a credited top-up removes its credited amount once, using an `ADJUST` ledger entry labelled “Subscription credit removed on bill cancellation”. Cancellation is blocked if available funds cannot cover that reversal; the bill and wallet remain unchanged. A cancelled bill cannot later be marked paid/delivered. No automatic refund of an external UPI/cash payment is performed.

Discount edits retain express charges, wallet usage, and advances. Paid/cancelled bills cannot be edited. A discount that would put the full bill value below wallet funds already used is rejected; cancel and recreate the bill through the existing workflow.

## Checks

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases with synthetic records. They never use the business database. See [FIXES.md](FIXES.md) for each before/after change and validation details.
