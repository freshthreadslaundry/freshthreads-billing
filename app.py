from flask import g, abort, Flask, render_template, request, redirect, url_for, send_file, session, flash
from functools import wraps
from pathlib import Path
from io import BytesIO
from urllib.parse import urlencode
from werkzeug.security import check_password_hash
from billing_math import number, money, totals
import time
import re
import sqlite3
from datetime import date, datetime as _dt_datetime

# Python 3.12+ sqlite3 default date/datetime adapters are deprecated.
# Store dates/timestamps as ISO-8601 TEXT.
sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(_dt_datetime, lambda dt: dt.isoformat(timespec="seconds"))

from datetime import datetime, timedelta
from flask import jsonify
import qrcode
import os
from flask import render_template, send_from_directory
from invoice_pdf import create_invoice_pdf
import os
import pandas as pd
import secrets
from flask import jsonify
from flask_cors import CORS



# === Customer Insights thresholds (tune anytime) ===
VIP_MIN_SPEND = 5000          # ₹ threshold for VIP / review candidates
LAPSED_DAYS = 45              # inactive if not visited in N days
MIN_VISITS_FOR_REVIEW = 2     # prefer repeat customers for review ask

app = Flask(__name__, instance_relative_config=True)
# Deployment settings live outside version control. Environment overrides local config.
app.config.from_pyfile('config.py', silent=True)
for setting in ('SECRET_KEY', 'LOGIN_USERNAME', 'LOGIN_PASSWORD_HASH', 'ADMIN_PASSWORD_HASH'):
    if os.environ.get('FT_' + setting):
        app.config[setting] = os.environ['FT_' + setting]
missing_settings = [key for key in ('SECRET_KEY', 'LOGIN_USERNAME', 'LOGIN_PASSWORD_HASH', 'ADMIN_PASSWORD_HASH') if not app.config.get(key)]
if missing_settings:
    raise RuntimeError('Missing private settings: ' + ', '.join(missing_settings) + '. See README.md')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
if os.environ.get('FT_COOKIE_SECURE') == '1':
    app.config['SESSION_COOKIE_SECURE'] = True
public_origins = [x.strip() for x in os.environ.get('FT_PUBLIC_ORIGINS', app.config.get('PUBLIC_ORIGINS', '')).split(',') if x.strip()]
if public_origins:
    CORS(app, resources={r'/api/bill-info/*': {'origins': public_origins}})


@app.before_request
def protect_staff_routes():
    # Token-scoped customer invoices remain public. All other business data is staff-only.
    public = {'login', 'customer_view', 'api_bill_info', 'static'}
    if request.endpoint and request.endpoint not in public and 'user' not in session:
        if request.path.startswith('/api/') or request.endpoint in {'outstanding_summary', 'export_report', 'export_daily'}:
            return jsonify(success=False, message='Please log in'), 401
        return redirect(url_for('login'))
    # Reject browser requests initiated by a different origin, including login CSRF.
    if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
        origin = request.headers.get('Origin')
        expected_origin = os.environ.get('FT_PUBLIC_ORIGIN') or app.config.get('PUBLIC_ORIGIN') or request.host_url
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and origin.rstrip('/') != expected_origin.rstrip('/')):
            return jsonify(success=False, message='Cross-origin changes are not allowed'), 403
    if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
        supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
        expected = session.get('csrf_token', '')
        if not expected or not secrets.compare_digest(supplied, expected):
            return jsonify(success=False, message='Session expired. Reload the page and try again.'), 403
    if request.endpoint == 'static' and (request.view_args or {}).get('filename', '').split('/')[0] in {'reports', 'invoices'} and 'user' not in session:
        return jsonify(success=False, message='Please log in'), 401
    if request.endpoint == 'outstanding_report' and app.config.get('OUTSTANDING_PASSWORD_REQUIRED', True) and session.get('admin_verified_until', 0) < time.time():
        return jsonify(success=False, message='Enter the admin password to view this report'), 403


@app.errorhandler(sqlite3.IntegrityError)
def invalid_database_change(error):
    if request.path.startswith('/api/'):
        return jsonify(success=False, error='Change was not saved. Check that the record exists and is not in use or duplicated.'), 400
    return 'Change was not saved. Check the record and try again.', 400


@app.after_request
def attach_request_security(response):
    if response.mimetype == 'text/html' and not response.direct_passthrough:
        html = response.get_data(as_text=True)
        token = session.setdefault('csrf_token', secrets.token_hex(32))
        def secure_form(match):
            tag = match.group(0)
            if re.search(r'method\s*=\s*[\"\']?post', tag, re.I):
                return tag + f'<input type="hidden" name="csrf_token" value="{token}">'
            return tag
        html = re.sub(r'<form\b[^>]*>', secure_form, html, flags=re.I)
        if '</head>' in html:
            guard = f'<meta name="csrf-token" content="{token}"><script src="/static/request_security.js"></script>'
            html = html.replace('</head>', guard + '</head>', 1)
        response.set_data(html)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def require_internal_secret():
    expected = os.environ.get("FT_INTERNAL_SECRET", "")
    got = request.headers.get("X-FT-SECRET", "")
    return bool(expected) and secrets.compare_digest(got, expected)


def generate_token():
    return secrets.token_urlsafe(10)  # ~16 char secure URL-safe token

# Decorator to enforce login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

DB_PATH = os.environ.get("FT_DB_PATH", str(Path(__file__).resolve().parent / "database" / "fresh_threads.db"))

def generate_upi_qr(upi_id, name, amount, qr_path):
    Path(qr_path).parent.mkdir(parents=True, exist_ok=True)
    upi_url = "upi://pay?" + urlencode({"pa": upi_id, "pn": name, "am": f"{amount:.2f}", "cu": "INR"})
    img = qrcode.make(upi_url)
    img.save(qr_path)

@app.route("/api/customers")
def api_customers():
    query = request.args.get("q", "")
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM customers
            WHERE phone LIKE ? OR name LIKE ?
            LIMIT 10
        """, (f"%{query}%", f"%{query}%")).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/subscription-status")
@login_required
def api_subscription_status():
    """Return subscription wallet status for a customer phone.
    This endpoint is used by index.html to show/hide the wallet UI.
    """
    phone = (request.args.get("phone") or "").strip()
    if not phone:
        return jsonify({"success": False, "error": "phone is required"}), 400

    with get_db_connection() as conn:
        cur = conn.cursor()

        cur.execute("SELECT id, name, phone FROM customers WHERE phone = ? LIMIT 1", (phone,))
        cust = cur.fetchone()
        if not cust:
            return jsonify({"success": True, "exists": False, "balance": 0, "is_active": False})

        customer_id = cust["id"]

        # customer_subscriptions may not exist in older DBs; fail gracefully.
        try:
            cur.execute(
                "SELECT current_balance, is_active, updated_at FROM customer_subscriptions WHERE customer_id = ? LIMIT 1",
                (customer_id,),
            )
            sub = cur.fetchone()
        except sqlite3.OperationalError:
            sub = None

        balance = float(sub["current_balance"]) if sub else 0.0
        is_active = bool(sub["is_active"]) if sub else False
        updated_at = sub["updated_at"] if sub else None

        return jsonify({
            "success": True,
            "exists": True,
            "customer_id": customer_id,
            "name": cust["name"],
            "phone": cust["phone"],
            "balance": balance,
            "is_active": is_active,
            "updated_at": updated_at
        })
@app.route("/api/services")
def api_services():
    query = request.args.get("q", "")
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT s.*, EXISTS(SELECT 1 FROM subscription_products sp WHERE sp.service_id=s.id AND sp.is_active=1) AS is_subscription_topup FROM services s
            WHERE s.name LIKE ?
            LIMIT 10
        """, (f"%{query}%",)).fetchall()
    return jsonify([dict(row) for row in rows])


def get_db_connection():
    # Use a longer timeout and WAL to reduce 'database is locked' on Windows.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    try:
        cur.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    cur.execute("PRAGMA busy_timeout = 30000;")
    if request:
        g.setdefault('db_connections', []).append(conn)
    return conn


@app.teardown_request
def close_connections(error=None):
    for connection in g.pop('db_connections', []):
        connection.close()  # Also rolls back any incomplete transaction.


def apply_subscription_credit_for_bill(bill_id, conn, cur):
    """If the bill contains subscription top-up service(s) and is Paid, credit the customer's wallet.
    Idempotent: will not double-credit the same bill.
    Uses the provided conn/cur (same transaction) to avoid DB locks.
    """
    if not cur.execute("SELECT 1 FROM bills WHERE id=? AND IFNULL(void,0)=0", (bill_id,)).fetchone():
        return 0.0
    # Check bill is marked Paid
    cur.execute("SELECT payment_status FROM bill_status WHERE bill_id = ?", (bill_id,))
    s = cur.fetchone()
    if not s or str(s["payment_status"]).lower() != "paid":
        return 0.0

    # Prevent double credit
    cur.execute(
        "SELECT 1 FROM subscription_ledger WHERE bill_id = ? AND txn_type = 'CREDIT' LIMIT 1",
        (bill_id,),
    )
    if cur.fetchone():
        return 0.0

    # Find subscription top-up items on this bill and compute credit
    cur.execute(
        """
        SELECT b.customer_id, bi.qty, sp.credit_amount
        FROM bill_items bi
        JOIN subscription_products sp
          ON sp.service_id = bi.service_id
         AND sp.is_active = 1
        JOIN bills b ON b.id = bi.bill_id
        WHERE bi.bill_id = ?
        """,
        (bill_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return 0.0

    customer_id = rows[0]["customer_id"]
    total_credit = money(0)
    for r in rows:
        total_credit += number(r["qty"]) * number(r["credit_amount"])

    total_credit = float(money(total_credit))
    if total_credit <= 0:
        return 0.0

    # Ensure wallet row exists
    cur.execute(
        """
        INSERT OR IGNORE INTO customer_subscriptions (customer_id, current_balance, is_active)
        VALUES (?, 0, 1)
        """,
        (customer_id,),
    )

    # Ledger credit
    cur.execute(
        """
        INSERT INTO subscription_ledger (customer_id, bill_id, txn_type, amount, notes)
        VALUES (?, ?, 'CREDIT', ?, 'Subscription credit unlocked after payment')
        """,
        (customer_id, bill_id, total_credit),
    )

    # Update cached balance
    cur.execute(
        """
        UPDATE customer_subscriptions
        SET current_balance = ROUND(current_balance + ?, 2),
            updated_at = CURRENT_TIMESTAMP
        WHERE customer_id = ?
        """,
        (total_credit, customer_id),
    )

    return total_credit


@app.route("/")
@login_required
def home():
    return render_template("home.html")


@app.route("/services", methods=["GET", "POST"])
@login_required
def services():
    with get_db_connection() as conn:
        cursor = conn.cursor()

        if request.args.get('delete'):
            return 'Delete requires a POST request', 405
        if request.method == 'POST' and request.form.get('action') == 'delete':
            try:
                conn.execute('DELETE FROM services WHERE id=?', (request.form.get('service_id'),))
                conn.commit()
            except sqlite3.IntegrityError:
                return 'This service is used by existing bills and cannot be deleted', 409
            return redirect('/services')
        # Save service (insert or update)
        if request.method == "POST":
            service_id = request.form.get("service_id")
            name = request.form.get("name")
            try:
                rate = float(money(request.form.get('rate')))
                if not (name or '').strip():
                    raise ValueError('Service name is required')
            except ValueError as exc:
                return str(exc), 400

            if service_id:
                cursor.execute("UPDATE services SET name = ?, rate = ? WHERE id = ?", (name, rate, service_id))
            else:
                cursor.execute("INSERT INTO services (name, rate) VALUES (?, ?)", (name, rate))
            conn.commit()
            return redirect("/services")

        # Edit
        if request.args.get("edit"):
            service_id = request.args.get("edit")
            edit_service = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        else:
            edit_service = None

        services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()

    return render_template("services.html", services=services, edit_service=edit_service)

@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    view = request.args.get("view", "all")  # all | review | lapsed | vip
    q = (request.args.get("q") or "").strip()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Handle form submission (add/edit basic profile)
        if request.method == "POST":
            customer_id = request.form.get("customer_id")
            name = request.form.get("name")
            phone = request.form.get("phone")
            email = request.form.get("email_address")
            address = request.form.get("address")
            cust_type = request.form.get("customer_type")

            if customer_id:  # update
                cursor.execute("""
                    UPDATE customers
                    SET name=?, phone=?, email_address=?, address=?, customer_type=?
                    WHERE id=?""",
                    (name, phone, email, address, cust_type, customer_id))
            else:  # insert
                cursor.execute("""
                    INSERT INTO customers (name, phone, email_address, address, customer_type, review_requested, review_stars)
                    VALUES (?, ?, ?, ?, ?, 'NO', NULL)
                """, (name, phone, email, address, cust_type))
            conn.commit()
            return redirect(url_for("customers", view=view, q=q))

        # Handle edit
        edit_customer = None
        if request.args.get("edit"):
            edit_id = request.args.get("edit")
            edit_customer = conn.execute("SELECT * FROM customers WHERE id = ?", (edit_id,)).fetchone()

        if request.args.get('delete'):
            return 'Customer deletion is not supported through a link', 405

        # --- Customer stats (spend, visits, last visit, etc.) ---
        # Note: bills.total is your stored subtotal. We exclude void bills.
        stats_sql = """
        WITH customer_stats AS (
            SELECT
                c.id, c.name, c.phone, c.email_address, c.address, c.customer_type,
                c.review_requested, c.review_stars,
                COUNT(CASE WHEN IFNULL(b.void,0) != 1 THEN b.id END) AS visit_count,
                COALESCE(SUM(CASE WHEN IFNULL(b.void,0) != 1 THEN b.total ELSE 0 END), 0) AS total_spend,
                MAX(CASE WHEN IFNULL(b.void,0) != 1 THEN DATE(b.date) ELSE NULL END) AS last_visit
            FROM customers c
            LEFT JOIN bills b ON b.customer_id = c.id
            GROUP BY c.id
        ),
        enriched AS (
            SELECT
                *,
                CASE
                    WHEN visit_count > 0 THEN (total_spend * 1.0 / visit_count)
                    ELSE 0
                END AS avg_bill,
                CASE
                    WHEN last_visit IS NOT NULL THEN CAST((julianday('now') - julianday(last_visit)) AS INT)
                    ELSE NULL
                END AS days_since_last_visit
            FROM customer_stats
        )
        SELECT *
        FROM enriched
        WHERE 1=1
        """

        params = []
        if q:
            stats_sql += " AND (name LIKE ? OR phone LIKE ?) "
            params.extend([f"%{q}%", f"%{q}%"])

        title = "All Customers"
        if view == "review":
            title = "Review Candidates"
            stats_sql += """
            AND (review_requested IS NULL OR review_requested = 'NO')
            AND visit_count >= ?
            AND total_spend >= ?
            AND days_since_last_visit IS NOT NULL
            AND days_since_last_visit <= ?
            """
            params.extend([MIN_VISITS_FOR_REVIEW, VIP_MIN_SPEND, 21])

        elif view == "lapsed":
            title = "Lapsed High-Value Customers"
            stats_sql += """
              AND total_spend >= ?
              AND days_since_last_visit IS NOT NULL
              AND days_since_last_visit >= ?
            """
            params.extend([VIP_MIN_SPEND, LAPSED_DAYS])

        elif view == "vip":
            title = "VIP Customers"
            stats_sql += " AND total_spend >= ? "
            params.append(VIP_MIN_SPEND)

        stats_sql += " ORDER BY total_spend DESC, visit_count DESC, name ASC LIMIT 500"

        customers = conn.execute(stats_sql, params).fetchall()

        # --- Insights tiles ---
        insights = {}

        insights["total_customers"] = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]

        insights["active_30d"] = conn.execute("""
            SELECT COUNT(DISTINCT c.id)
            FROM customers c
            JOIN bills b ON b.customer_id = c.id
            WHERE IFNULL(b.void,0) != 1
              AND DATE(b.date) >= DATE('now', '-30 day')
        """).fetchone()[0]

        insights["new_this_month"] = conn.execute("""
            SELECT COUNT(DISTINCT c.id)
            FROM customers c
            JOIN bills b ON b.customer_id = c.id
            WHERE IFNULL(b.void,0) != 1
              AND STRFTIME('%Y-%m', b.date) = STRFTIME('%Y-%m', 'now')
              AND NOT EXISTS (SELECT 1 FROM bills earlier WHERE earlier.customer_id=c.id AND IFNULL(earlier.void,0)=0 AND DATE(earlier.date)<DATE('now','start of month'))
        """).fetchone()[0]

        repeat = conn.execute("""
            WITH v AS (
              SELECT customer_id, COUNT(*) AS n
              FROM bills
              WHERE IFNULL(void,0) != 1
              GROUP BY customer_id
            )
            SELECT
              SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) AS repeaters,
              COUNT(*) AS total
            FROM v
        """).fetchone()
        repeaters = repeat[0] or 0
        total_visiting = repeat[1] or 0
        insights["repeat_rate"] = int(round((repeaters * 100.0 / total_visiting), 0)) if total_visiting else 0

    return render_template(
        "customers.html",
        customers=customers,
        edit_customer=edit_customer,
        view=view,
        q=q,
        title=title,
        insights=insights
    )
@app.route("/api/customers/update-review", methods=["POST"])
@login_required
def api_update_customer_review():
    data = request.get_json() or {}

    customer_id = data.get("customer_id")
    review_requested = (data.get("review_requested") or "NO").upper()
    review_stars = data.get("review_stars")

    if review_requested not in ("YES", "NO"):
        return jsonify(success=False, error="review_requested must be YES/NO"), 400

    if review_requested == "NO":
        # 🔑 If review is reset, clear stars
        review_stars = None
    else:
        if review_stars is not None:
            try:
                review_stars = int(review_stars)
                if review_stars < 1 or review_stars > 5:
                    return jsonify(success=False, error="Stars must be 1–5"), 400
            except:
                return jsonify(success=False, error="Invalid stars"), 400

    if not customer_id:
        return jsonify(success=False, error="customer_id required"), 400

    db = get_db_connection()
    db.execute("""
        UPDATE customers
        SET review_requested = ?, review_stars = ?
        WHERE id = ?
    """, (review_requested, review_stars, customer_id))
    db.commit()

    return jsonify(success=True)


@app.route("/api/customers/mark-review-requested", methods=["POST"])
@login_required
def api_mark_review_requested_bulk():
    data = request.get_json() or {}
    ids = data.get("customer_ids") or []
    ids = [int(x) for x in ids if str(x).isdigit()]

    if not ids:
        return jsonify(success=False, error="customer_ids required"), 400

    placeholders = ",".join(["?"] * len(ids))
    db = get_db_connection()
    db.execute(f"""
        UPDATE customers
        SET review_requested = 'YES'
        WHERE id IN ({placeholders})
    """, ids)
    db.commit()
    return jsonify(success=True, updated=len(ids))


@app.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    if request.method == "POST":
        data = request.form

        try:
            validated_items = []
            names, quantities, rates_in = data.getlist('service[]'), data.getlist('qty[]'), data.getlist('rate[]')
            if not names or len(names) != len(quantities) or len(names) != len(rates_in):
                raise ValueError('Each service needs a quantity and rate')
            for name_in, qty_in, rate_in in zip(names, quantities, rates_in):
                qty_n, rate_n = number(qty_in, 'Quantity'), money(rate_in)
                if not name_in.strip() or qty_n <= 0:
                    raise ValueError('Service name and positive quantity are required')
                validated_items.append((float(qty_n), float(rate_n), float(money(qty_n * rate_n))))
            for field in ('discountValue', 'surchargeValue', 'advancePaid'):
                number(data.get(field), field)
            for field in ('discount', 'surcharge'):
                if data.get(field + 'Type', 'Amount') not in ('%', 'Rs', 'Amount'):
                    raise ValueError('Invalid discount or surcharge type')
                if data.get(field + 'Type') == '%' and number(data.get(field + 'Value')) > 100:
                    raise ValueError('Percentage must be between 0 and 100')
            for piece in data.getlist('pieces[]'):
                if piece and (number(piece, 'Pieces') != int(number(piece, 'Pieces'))):
                    raise ValueError('Pieces must be a whole number')
            if not (data.get('customerPhone') or '').strip() or not (data.get('customerName') or '').strip():
                raise ValueError('Customer name and phone are required')
        except ValueError as exc:
            return str(exc), 400

        # Customer handling
        phone = data.get("customerPhone").strip()
        name = data.get("customerName").strip()
        pickup_address = (data.get("pickupAddress") or "").strip()

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("SELECT id FROM customers WHERE phone = ?", (phone,))
            row = cur.fetchone()
            if row:
                customer_id = row[0]
                cur.execute("UPDATE customers SET name=?, address=? WHERE id=?", (name, pickup_address, customer_id))
            else:
                cur.execute("INSERT INTO customers (name, phone, address) VALUES (?, ?, ?)", (name, phone, pickup_address))
                customer_id = cur.lastrowid

            # Collect billing fields
            order_type = data.get("orderType", "Walk-in")
            delivery_date = data.get("deliveryDate")

            discount_type = data.get("discountType", "Amount")
            discount_value = float(data.get("discountValue") or 0)

            # Express / Surcharge (bill-level)
            express_service = 1 if data.get("expressService") in ("on", "1", "true", "yes") else 0
            surcharge_type = data.get("surchargeType", "Rs")  # "Rs" or "%"
            surcharge_value = float(data.get("surchargeValue") or 0)

            advance_paid = float(data.get("advancePaid") or 0)

            token = secrets.token_urlsafe(10)
            cur.execute("""
                INSERT INTO bills (
                    customer_id, date, total, pickup_date, pickup_time,
                    delivery_date, dropoff_time, pickup_address,
                    order_type, discount_type, discount_value,
                    express_service, surcharge_type, surcharge_value, surcharge_amount,
                    advance_paid, balance_amount, token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                customer_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                0, "", "", delivery_date, "", pickup_address,
                order_type, discount_type, discount_value,
                express_service, surcharge_type, surcharge_value, 0,
                advance_paid, 0, token
            ))

            bill_id = cur.lastrowid

            # Read checkbox value
            is_paid = "isPaid" in data
            payment_status = "Paid" if is_paid else "Pending"
            delivery_status = "Not Delivered"

            # Insert into bill_status
            cur.execute("""
                INSERT INTO bill_status (bill_id, payment_status, delivery_status)
                VALUES (?, ?, ?)
            """, (bill_id, payment_status, delivery_status))

            # Add services
            services = data.getlist("service[]")
            qtys = data.getlist("qty[]")
            rates = data.getlist("rate[]")
            posted_totals = data.getlist("total[]")
            pieces_list = data.getlist("pieces[]")

            grand_total = 0
            for i in range(len(services)):
                name = services[i]
                qty, rate, total = validated_items[i]
                grand_total += total
                original_name = name.strip()
                # Get pieces[] and conditionally append

                cur.execute("SELECT id FROM services WHERE name = ?", (original_name,))
                service_row = cur.fetchone()
                service_id = service_row[0] if service_row else None

                pieces = pieces_list[i] if i < len(pieces_list) else ""
                if "kg" in name.lower() and pieces.strip():
                    name = f"{name} ({qty:.1f} KG)"
                    items = int(pieces.strip())
                else:
                    items = int(qty)

                cur.execute("""
                    INSERT INTO bill_items (bill_id, service_id, service_name, qty, rate, total, items)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (bill_id, service_id, name, qty, rate, total, items))


            # ---- Bill-level totals (server-side truth) ----
            subtotal = grand_total

            # Discount
            if discount_type == "%":
                discount_amount = subtotal * discount_value / 100
            else:
                discount_amount = discount_value

            # Surcharge
            if express_service and surcharge_value > 0:
                if surcharge_type == "%":
                    surcharge_amount = subtotal * surcharge_value / 100
                else:
                    surcharge_amount = surcharge_value
            else:
                surcharge_amount = 0.0
                surcharge_type = "Rs"
                surcharge_value = 0.0
                express_service = 0

            computed = totals({
                'discount_type': discount_type, 'discount_value': discount_value,
                'express_service': express_service, 'surcharge_type': surcharge_type,
                'surcharge_value': surcharge_value, 'advance_paid': advance_paid,
            }, [{'total': line[2]} for line in validated_items])
            subtotal = computed['subtotal']
            surcharge_amount = computed['surcharge_amount']
            final_total = computed['total_before_wallet']

            # ---- Subscription wallet auto-deduction (server-side truth) ----
            # Auto-apply wallet on normal bills when customer has balance.
            # Do NOT apply wallet on subscription top-up bills (items that exist in subscription_products).
            wallet_used = 0.0
            wallet_balance_before = 0.0

            # Is this a subscription top-up bill?
            cur.execute("""
                SELECT 1
                FROM bill_items bi
                JOIN subscription_products sp
                  ON sp.service_id = bi.service_id
                 AND sp.is_active = 1
                WHERE bi.bill_id = ?
                LIMIT 1
            """, (bill_id,))
            is_subscription_topup_bill = cur.fetchone() is not None

            if not is_subscription_topup_bill:
                # Fetch wallet balance (only customers who bought subscription will have a row)
                cur.execute("SELECT current_balance FROM customer_subscriptions WHERE customer_id = ? AND is_active = 1", (customer_id,))
                r = cur.fetchone()
                wallet_balance_before = float(r[0]) if r else 0.0

                if wallet_balance_before > 0:
                    wallet_used = float(min(money(wallet_balance_before), money(final_total)))

                    if wallet_used > 0:
                        # Prevent double debit for same bill
                        cur.execute("""
                            SELECT 1 FROM subscription_ledger
                            WHERE bill_id = ? AND txn_type = 'DEBIT'
                            LIMIT 1
                        """, (bill_id,))
                        if not cur.fetchone():
                            cur.execute("""
                                INSERT INTO subscription_ledger (customer_id, bill_id, txn_type, amount, notes)
                                VALUES (?, ?, 'DEBIT', ?, 'Auto wallet deduction on bill')
                            """, (customer_id, bill_id, wallet_used))

                            cur.execute("""
                                UPDATE customer_subscriptions
                                SET current_balance = ROUND(current_balance - ?, 2),
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE customer_id = ?
                            """, (wallet_used, customer_id))

            # Payable after wallet
            final_total = max(0.0, final_total - wallet_used)

            balance_amount = float(max(money(0), money(final_total) - money(advance_paid)))

            cur.execute(
                """UPDATE bills
                   SET total = ?,
                       balance_amount = ?,
                       express_service = ?,
                       surcharge_type = ?,
                       surcharge_value = ?,
                       surcharge_amount = ?
                   WHERE id = ?""",
                (subtotal, balance_amount, express_service, surcharge_type, surcharge_value, surcharge_amount, bill_id)
            )
            if is_paid:
                apply_subscription_credit_for_bill(bill_id, conn, cur)
            conn.commit()

            return redirect(url_for("bill_view", bill_id=bill_id))
        except (ValueError, sqlite3.IntegrityError) as exc:
            conn.rollback()
            return f'Bill was not saved: {exc}', 400
        finally:
            conn.close()

    return render_template("index.html")

@app.route("/api/generate-bill", methods=["POST"])
def generate_bill():
    return jsonify(success=False, message='Use the billing form to create a saved invoice'), 501


def invoice_context(bill_id=None, token=None, make_qr=True):
    conn = get_db_connection()
    try:
        key, value = ('b.id', bill_id) if bill_id is not None else ('b.token', token)
        row = conn.execute(f"SELECT b.*, c.name AS customer_name, c.phone AS customer_phone FROM bills b JOIN customers c ON c.id=b.customer_id WHERE {key}=?", (value,)).fetchone()
        if not row:
            abort(404)
        bill = dict(row)
        items = conn.execute('SELECT * FROM bill_items WHERE bill_id=?', (bill['id'],)).fetchall()
        status = conn.execute('SELECT payment_status FROM bill_status WHERE bill_id=?', (bill['id'],)).fetchone()
        bill['is_paid'] = bool(status and status['payment_status'] == 'Paid')
        bill['is_cancelled'] = bool(bill.get('void'))
        for source, dest, fmt in [('date', 'formatted_date', '%Y-%m-%d %H:%M:%S'), ('delivery_date', 'formatted_due_date', '%Y-%m-%d')]:
            try:
                bill[dest] = datetime.strptime(bill[source], fmt).strftime('%d-%b-%Y')
            except (ValueError, TypeError):
                bill[dest] = bill[source]
        debit = conn.execute("SELECT COALESCE(SUM(amount),0) FROM subscription_ledger WHERE bill_id=? AND txn_type='DEBIT'", (bill['id'],)).fetchone()[0]
        amounts = totals(bill, items, debit, bill['is_paid'])
        wallet = conn.execute('SELECT current_balance FROM customer_subscriptions WHERE customer_id=?', (bill['customer_id'],)).fetchone()
        # QR payments are temporarily removed from bills. Keep the context field
        # and function argument for compatibility with existing invoice callers.
        qr_filename = None
        return dict(bill=bill, items=items, qr_image=qr_filename, final_total=amounts['final_payable'], wallet_balance=float(wallet[0]) if wallet else 0, **amounts)
    finally:
        conn.close()


@app.route('/bill/<int:bill_id>')
@login_required
def bill_view(bill_id):
    return render_template('invoice.html', **invoice_context(bill_id=bill_id))


@app.route('/invoice/pdf/<int:bill_id>')
@login_required
def generate_pdf(bill_id):
    context = invoice_context(bill_id=bill_id, make_qr=False)
    return send_file(create_invoice_pdf(context), mimetype='application/pdf', as_attachment=True,
                     download_name=f'invoice_{bill_id}.pdf')


@app.route("/reports", methods=["GET"])
@login_required
def reports():
    conn = get_db_connection()
    cur = conn.cursor()

    start_date = request.args.get("start")
    end_date = request.args.get("end")

    bills = []
    bill_dicts = []
    total_amount = total_advance = total_balance = 0

    if start_date and end_date:
        cur.execute("""
            SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
            FROM bills b
            JOIN customers c ON b.customer_id = c.id
            WHERE DATE(b.date) BETWEEN ? AND ?
              AND IFNULL(b.void, 0) != 1
            ORDER BY b.date ASC
        """, (start_date, end_date))
        bills = cur.fetchall()
        
        for b in bills:
            b=dict(b)
            item_subtotal = conn.execute('SELECT COALESCE(SUM(total),0) FROM bill_items WHERE bill_id=?', (b['id'],)).fetchone()[0]
            total_amount += totals(b, [{'total': item_subtotal}])['total_before_wallet']
            total_advance += float(b["advance_paid"] or 0)
            total_balance += float(b["balance_amount"] or 0)

            # Format date in dd-Mon-yy format
            try:
                bill_date = datetime.strptime(b["date"], "%Y-%m-%d %H:%M:%S")
                b["formatted_date"] = bill_date.strftime("%d-%b-%y")
            except:
                b["formatted_date"] = b["date"]
            bill_dicts.append(b)
    return render_template("reports.html",
        bills=bill_dicts,
        start_date=start_date,
        end_date=end_date,
        total_amount=total_amount,
        total_advance=total_advance,
        total_balance=total_balance
    )

def excel_response(df, filename):
    # Customer-entered names/notes must remain text when opened in Excel.
    for column in df.select_dtypes(include=['object']).columns:
        df[column] = df[column].map(lambda value: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route("/reports/export")
def export_report():
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT b.id AS bill_id, b.date, c.name AS customer_name, c.phone AS customer_phone,
               bi.service_name, bi.qty, bi.rate, bi.total
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        JOIN bill_items bi ON b.id = bi.bill_id
        WHERE DATE(b.date) BETWEEN ? AND ?
        AND b.void != 1
        ORDER BY b.date ASC, b.id ASC
    """, (start_date, end_date))

    rows = cur.fetchall()
    df = pd.DataFrame([tuple(row) for row in rows], columns=[desc[0] for desc in cur.description])
    return excel_response(df, f'report_{start_date}_to_{end_date}.xlsx')


@app.route("/api/daily-report")
def api_daily_report():
    report_date = request.args.get("date")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT b.id AS bill_id, b.delivery_date,
               c.name AS customer_name, c.phone AS customer_phone,
               bi.service_name, bi.items, bi.rate, bi.total,
               s.payment_status, s.delivery_status
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        JOIN bill_items bi ON b.id = bi.bill_id
        LEFT JOIN bill_status s ON b.id = s.bill_id
        WHERE DATE(b.date) = ?
        AND b.void != 1
        ORDER BY b.id
    """, (report_date,))
    rows = cur.fetchall()

    return jsonify([dict(row) for row in rows])

    return jsonify([dict(row) for row in rows])
@app.route("/reports/export-daily")
def export_daily():
    report_date = request.args.get("date")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            b.id AS bill_id,
            c.name AS customer_name,
            c.phone AS customer_phone,
            bi.service_name,
            bi.items,
            bi.rate,
            bi.total,
            b.delivery_date
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        JOIN bill_items bi ON b.id = bi.bill_id
        WHERE DATE(b.date) = ?
          AND IFNULL(b.void,0)=0
        ORDER BY b.id
    """, (report_date,))
    rows = cur.fetchall()

    # ✅ Explicit column names
    columns = [desc[0] for desc in cur.description]

    import pandas as pd
    df = pd.DataFrame(rows, columns=columns)
    return excel_response(df, f'daily_{report_date}.xlsx')

@app.route("/api/delivery-report")
def api_delivery_report():
    delivery_date = request.args.get("date")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT b.id AS bill_id, b.delivery_date,c.name AS customer_name, c.phone AS customer_phone,
               bi.service_name, bi.items, bi.rate, bi.total
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        JOIN bill_items bi ON b.id = bi.bill_id
        WHERE DATE(b.delivery_date) = ?
        AND b.void != 1
        ORDER BY b.id
    """, (delivery_date,))
    
    rows = cur.fetchall()
    return jsonify([dict(row) for row in rows])

@app.route("/api/cancel-bill", methods=["POST"])
def cancel_bill():
    bill_id = (request.get_json(silent=True) or {}).get('bill_id')
    if not bill_id:
        return jsonify(success=False, message='Bill ID is required'), 400
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        bill = conn.execute('SELECT customer_id, void FROM bills WHERE id=?', (bill_id,)).fetchone()
        if not bill:
            return jsonify(success=False, message='Bill not found'), 404
        if bill['void']:
            return jsonify(success=True, message=f'Bill #{bill_id} was already cancelled')
        customer_id = bill['customer_id']
        credit = money(conn.execute("SELECT COALESCE(SUM(amount),0) FROM subscription_ledger WHERE bill_id=? AND txn_type='CREDIT'", (bill_id,)).fetchone()[0])
        debit = money(conn.execute("SELECT COALESCE(SUM(amount),0) FROM subscription_ledger WHERE bill_id=? AND txn_type='DEBIT'", (bill_id,)).fetchone()[0])
        wallet = conn.execute('SELECT current_balance FROM customer_subscriptions WHERE customer_id=?', (customer_id,)).fetchone()
        balance = money(wallet[0]) if wallet else money(0)
        if credit and (not wallet or balance + debit < credit):
            return jsonify(success=False, message='Cannot cancel: subscription credit has been spent. Settle the wallet first.'), 409
        if debit:
            conn.execute("INSERT INTO subscription_ledger(customer_id,bill_id,txn_type,amount,notes) VALUES (?,?,'REVERSAL',?,'Wallet reversal on bill cancellation')", (customer_id,bill_id,float(debit)))
        if credit:
            # ADJUST is supported by the existing schema; notes specify the debit direction.
            conn.execute("INSERT INTO subscription_ledger(customer_id,bill_id,txn_type,amount,notes) VALUES (?,?,'ADJUST',?,'Subscription credit removed on bill cancellation')", (customer_id,bill_id,float(credit)))
        if debit or credit:
            conn.execute('UPDATE customer_subscriptions SET current_balance=?,updated_at=CURRENT_TIMESTAMP WHERE customer_id=?', (float(balance + debit - credit),customer_id))
        conn.execute('UPDATE bills SET void=1 WHERE id=?', (bill_id,))
        conn.commit()
        return jsonify(success=True, message=f'Bill #{bill_id} cancelled')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/bill/<int:bill_id>/embed")
@login_required
def bill_embed(bill_id):
    return render_template('invoice_embed.html', **invoice_context(bill_id=bill_id))

@app.route("/api/outstanding-report")
def outstanding_report():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id AS bill_id, b.delivery_date,
               c.name AS customer_name, c.phone AS customer_phone,
               sum(bi.items) as items, CASE WHEN s.payment_status='Paid' THEN 0 ELSE b.balance_amount END AS balance_amount,
               s.payment_status, s.delivery_status
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        JOIN bill_items bi ON b.id = bi.bill_id
        LEFT JOIN bill_status s ON b.id = s.bill_id
        WHERE b.void != 1
          AND (s.payment_status != 'Paid' OR s.delivery_status != 'Delivered')
                GROUP BY bi.bill_id, b.delivery_Date, c.name, c.phone, b.balance_amount, s.payment_status, s.delivery_status
        ORDER BY b.date DESC
    """)
    rows = cur.fetchall()
    return jsonify([dict(row) for row in rows])

@app.route("/api/mark-paid", methods=["POST"])
def mark_paid():
    bill_id = (request.get_json(silent=True) or {}).get("bill_id")
    if not bill_id:
        return jsonify({"success": False, "message": "bill_id is required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('BEGIN IMMEDIATE')
        row = cur.execute('SELECT void FROM bills WHERE id=?', (bill_id,)).fetchone()
        if not row:
            return jsonify(success=False, message='Bill not found'), 404
        if row['void']:
            return jsonify(success=False, message='Cancelled bills cannot be marked paid'), 409
        cur.execute("INSERT INTO bill_status(bill_id,payment_status) VALUES (?,'Paid') ON CONFLICT(bill_id) DO UPDATE SET payment_status='Paid',updated_at=CURRENT_TIMESTAMP", (bill_id,))
        # Unlock subscription credit if this is a subscription top-up bill
        credited = apply_subscription_credit_for_bill(bill_id, conn, cur)
        conn.commit()
        msg = f"Bill #{bill_id} marked as Paid"
        if credited and credited > 0:
            msg += f" (Wallet credited ₹{credited:.2f})"
        return jsonify({"success": True, "message": msg, "credited": float(credited or 0)})
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/api/mark-delivered", methods=["POST"])
def mark_delivered():
    bill_id = (request.get_json(silent=True) or {}).get('bill_id')
    with get_db_connection() as conn:
        row = conn.execute('SELECT void FROM bills WHERE id=?', (bill_id,)).fetchone()
        if not row:
            return jsonify(success=False, message='Bill not found'), 404
        if row['void']:
            return jsonify(success=False, message='Cancelled bills cannot be marked delivered'), 409
        conn.execute("INSERT INTO bill_status(bill_id,delivery_status) VALUES (?,'Delivered') ON CONFLICT(bill_id) DO UPDATE SET delivery_status='Delivered',updated_at=CURRENT_TIMESTAMP", (bill_id,))
    return jsonify(success=True, message=f'Bill #{bill_id} marked as Delivered')

@app.route("/outstanding_summary")
@login_required
def outstanding_summary():
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    # Pending Deliveries Today
    cur.execute("""
        SELECT COUNT(*)
        FROM bills
        JOIN bill_status ON bills.id = bill_status.bill_id
        WHERE date(delivery_date) = ? AND bill_status.delivery_status = 'Not Delivered' AND bills.void = 0
    """, (today,))
    pending_today = cur.fetchone()[0]

    # Pending Deliveries Tomorrow
    cur.execute("""
        SELECT COUNT(*)
        FROM bills
        JOIN bill_status ON bills.id = bill_status.bill_id
        WHERE date(delivery_date) = ? AND bill_status.delivery_status = 'Not Delivered' AND bills.void = 0
    """, (tomorrow,))
    pending_tomorrow = cur.fetchone()[0]

    # Pending Payments Count
    cur.execute("""
        SELECT COUNT(*)
        FROM bill_status
        JOIN bills ON bills.id = bill_status.bill_id
        WHERE bill_status.payment_status = 'Pending' AND bills.void = 0
    """)
    pending_payment_count = cur.fetchone()[0]

    # Pending Amount (only for pending payment bills)
    cur.execute("""
        SELECT SUM(bills.balance_amount)
        FROM bills
        JOIN bill_status ON bills.id = bill_status.bill_id
        WHERE bill_status.payment_status = 'Pending' AND bills.void = 0
    """)
    pending_amount = cur.fetchone()[0] or 0.0

    conn.close()
    
    return jsonify({
        "pending_today": pending_today,
        "pending_tomorrow": pending_tomorrow,
        "pending_payment_count": pending_payment_count,
        "pending_amount": f"{pending_amount:.2f}"
    })

def sales_records(start, end):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone,
               (SELECT COALESCE(SUM(bi.total),0) FROM bill_items bi WHERE bi.bill_id=b.id) AS item_subtotal
        FROM bills b JOIN customers c ON c.id=b.customer_id
        WHERE DATE(b.date) BETWEEN ? AND ? AND IFNULL(b.void,0)=0
        ORDER BY b.date, b.id
    """, (start,end)).fetchall()
    result = []
    for row in rows:
        bill = dict(row)
        bill['sale_total'] = totals(bill, [{'total': bill.pop('item_subtotal')}])['total_before_wallet']
        result.append(bill)
    return result


@app.route("/api/summary-daily")
def api_summary_daily():
    report_date = request.args.get('date')
    if not report_date:
        return jsonify(error='Date is required'), 400
    rows = sales_records(report_date, report_date)
    return jsonify(total_bills=len(rows), total_sales=f"{sum(money(b['sale_total']) for b in rows):.2f}")


@app.route('/api/summary-range')
def api_summary_range():
    start, end = request.args.get('start'), request.args.get('end')
    if not start or not end:
        return jsonify(error='Start and End date required'), 400
    rows = sales_records(start,end)
    return jsonify(total_bills=len(rows), total_sales=f"{sum(money(b['sale_total']) for b in rows):.2f}")


@app.route('/api/range-report')
def api_range_report():
    start, end = request.args.get('start'), request.args.get('end')
    if not start or not end:
        return jsonify([])
    return jsonify([dict(bill_id=b['id'], date=b['date'], customer_name=b['customer_name'], customer_phone=b['customer_phone'], total=b['sale_total']) for b in sales_records(start,end)])

@app.route("/api/verify-password", methods=["POST"])
def verify_password():
    data = request.get_json()
    if not data or not check_password_hash(app.config.get("ADMIN_PASSWORD_HASH", ""), data.get("password") or ""):
        return jsonify({"success": False, "message": "Invalid password"}), 403
    session["admin_verified_until"] = time.time() + 300
    return jsonify({"success": True})

@app.route("/api/range-summary-by-date")
def api_range_summary_by_date():
    start, end = request.args.get('start'), request.args.get('end')
    if not start or not end:
        return jsonify([])
    grouped = {}
    for bill in sales_records(start,end):
        day = bill['date'][:10]
        row = grouped.setdefault(day, dict(report_date=day, bill_count=0, total_sales=0, walkin_count=0, online_count=0, walkin_sales=0, online_sales=0))
        row['bill_count'] += 1
        row['total_sales'] = float(money(row['total_sales']) + money(bill['sale_total']))
        category = {'Walk-in':'walkin','Online':'online'}.get(bill['order_type'])
        if category:
            row[category+'_count'] += 1
            row[category+'_sales'] = float(money(row[category+'_sales']) + money(bill['sale_total']))
    return jsonify(list(grouped.values()))

# === Expense Types ===



@app.route("/api/expense-types", methods=["GET"])
def get_expense_types():
    q = request.args.get("q")
    conn = get_db_connection()
    cur = conn.cursor()
    if q:
        cur.execute("SELECT id, name FROM expense_types WHERE name LIKE ?", (f"%{q}%",))
    else:
        cur.execute("SELECT id, name FROM expense_types ORDER BY name")
    results = [{"id": row["id"], "name": row["name"]} for row in cur.fetchall()]
    conn.close()
    return jsonify(results)


@app.route("/api/expense-types", methods=["POST"])
def add_expense_type():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify(success=False, error="Name required")

    db = get_db_connection()
    cur = db.execute("INSERT INTO expense_types (name) VALUES (?)", (name,))
    db.commit()
    return jsonify(success=True, id=cur.lastrowid)

# === Expenses ===
@app.route("/api/expenses", methods=["POST"])
def create_expense():
    data = request.get_json(silent=True) or request.form
    try:
        amount = float(money(data.get('amount')))
        if amount <= 0 or not data.get('expense_type_id'):
            raise ValueError('A positive amount and expense type are required')
        datetime.strptime(data.get('date', ''), '%Y-%m-%d')
    except (ValueError, TypeError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    db = get_db_connection()
    cur = db.execute("""
        INSERT INTO expenses (expense_type_id, amount, date, notes)
        VALUES (?, ?, ?, ?)
    """, (
        data["expense_type_id"],
        amount,
        data["date"],
        data.get("notes", "")
    ))
    db.commit()
    return jsonify(success=True, id=cur.lastrowid)

@app.route("/expenses")
@login_required
def expenses_page():
    return render_template("expenses.html")


@app.route("/api/expense-report")
def expense_report():
    start = request.args.get("start")
    end = request.args.get("end")

    if not start or not end:
        return jsonify(success=False, error="Missing date range")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.date, et.name AS type, e.amount, e.notes
        FROM expenses e
        JOIN expense_types et ON e.expense_type_id = et.id
        WHERE date BETWEEN ? AND ?
        ORDER BY e.date DESC
    """, (start, end))
    data = [dict(row) for row in cur.fetchall()]
    conn.close()

    return jsonify(success=True, data=data)

@app.route("/bill/edit/<int:bill_id>", methods=["GET", "POST"])
@login_required
def edit_bill(bill_id):
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        cur.execute("BEGIN IMMEDIATE")
        discount_type = request.form.get("discountType")
        try:
            discount_value = float(number(request.form.get('discountValue'), 'Discount'))
        except ValueError as exc:
            conn.close()
            return str(exc), 400

        # Fetch current total and advance_paid
        cur.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
        row = cur.fetchone()

        if not row:
            conn.close()
            return "Bill not found", 404

        state = cur.execute('SELECT payment_status FROM bill_status WHERE bill_id=?', (bill_id,)).fetchone()
        if row['void'] or (state and state['payment_status'] == 'Paid'):
            conn.close()
            return 'Paid or cancelled bills cannot be edited', 409
        try:
            if discount_type not in ('Rs', 'Amount', '%'):
                raise ValueError('Invalid discount type')
            number(discount_value, 'Discount')
            if discount_type == '%' and discount_value > 100:
                raise ValueError('Percentage must not exceed 100')
            updated = dict(row, discount_type=discount_type, discount_value=discount_value)
            items = cur.execute('SELECT * FROM bill_items WHERE bill_id=?', (bill_id,)).fetchall()
            wallet = cur.execute("SELECT COALESCE(SUM(amount),0) FROM subscription_ledger WHERE bill_id=? AND txn_type='DEBIT'", (bill_id,)).fetchone()[0]
            computed = totals(updated, items, wallet)
            if computed['total_before_wallet'] < float(wallet):
                raise ValueError('Discount would reduce the bill below wallet money already used; cancel and recreate the bill')
            balance_amount = computed['balance_amount']
        except ValueError as exc:
            conn.close()
            return str(exc), 400

        # Update bill
        cur.execute("""
            UPDATE bills
            SET discount_type = ?, discount_value = ?, balance_amount = ?
            WHERE id = ?
        """, (discount_type, discount_value, balance_amount, bill_id))

        conn.commit()
        conn.close()
        return redirect(url_for("bill_view", bill_id=bill_id))

    # Load current bill for display
    bill = cur.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    conn.close()
    if not bill:
        abort(404)
    return render_template("edit_bill.html", bill=bill)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == app.config.get('LOGIN_USERNAME') and check_password_hash(app.config.get('LOGIN_PASSWORD_HASH', ''), password or ''):
            session.clear()
            session['user'] = username
            return redirect(url_for('home'))  # or billing dashboard
        else:
            flash('Invalid username or password', 'danger')
    return render_template('login.html')
@app.route("/pay/<token>")
def customer_view(token):
    return render_template('customer_invoice.html', **invoice_context(token=token))


@app.route('/api/bill-info/<token>')
def api_bill_info(token):
    context = invoice_context(token=token, make_qr=False)
    return jsonify({
        'bill': context['bill'], 'items': [dict(i) for i in context['items']],
        'payment_status': 'Paid' if context['bill']['is_paid'] else 'Pending',
        **{key: context[key] for key in ('wallet_used', 'wallet_balance', 'final_payable', 'total_before_wallet', 'advance_paid', 'balance_amount', 'amount_due')},
        'is_cancelled': context['bill']['is_cancelled'],
    })

@app.route("/api/expense-types/<int:type_id>", methods=["PUT"])
def update_expense_type(type_id):
    name = request.form.get("name")
    db = get_db_connection()
    db.execute("UPDATE expense_types SET name = ? WHERE id = ?", (name, type_id))
    db.commit()
    return jsonify(success=True)

@app.route("/api/expense-types/<int:type_id>", methods=["DELETE"])
def delete_expense_type(type_id):
    db = get_db_connection()
    used = db.execute("SELECT COUNT(*) FROM expenses WHERE expense_type_id = ?", (type_id,)).fetchone()[0]
    if used > 0:
        return jsonify(success=False, error="This type is in use and cannot be deleted")
    db.execute("DELETE FROM expense_types WHERE id = ?", (type_id,))
    db.commit()
    return jsonify(success=True)

@app.route("/api/expenses/<int:expense_id>", methods=["PUT"])
def update_expense(expense_id):
    data = request.get_json(silent=True) or {}
    try:
        amount = float(money(data.get('amount')))
        if amount <= 0 or not data.get('expense_type_id'):
            raise ValueError('A positive amount and expense type are required')
        datetime.strptime(data.get('date', ''), '%Y-%m-%d')
    except (ValueError, TypeError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    db = get_db_connection()
    db.execute("""
        UPDATE expenses
        SET expense_type_id = ?, amount = ?, date = ?, notes = ?
        WHERE id = ?
    """, (
        data["expense_type_id"],
        amount,
        data["date"],
        data.get("notes", ""),
        expense_id
    ))
    db.commit()
    return jsonify(success=True)


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    db = get_db_connection()
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    return jsonify(success=True)

@app.route("/expense-types")
def expense_types_page():
    return render_template("expense_types.html")

@app.route("/api/expenses/all")
def get_expenses_by_date():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify([])

    db = get_db_connection()
    rows = db.execute("""
        SELECT e.id, e.amount, e.date, e.notes,
               e.expense_type_id, t.name AS type_name
        FROM expenses e
        JOIN expense_types t ON e.expense_type_id = t.id
        WHERE e.date BETWEEN ? AND ?
        ORDER BY e.date DESC
    """, (start, end)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/bill-meta/<int:bill_id>")
def api_bill_meta(bill_id):
    conn = get_db_connection()
    cur = conn.cursor()
    row = cur.execute("SELECT IFNULL(void, 0) AS void FROM bills WHERE id = ?", (bill_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"is_cancelled": int(row[0]) == 1})


@app.route("/api/customers/mark-review-requested-single", methods=["POST"])
@login_required
def api_mark_review_requested_single():
    data = request.get_json() or {}
    customer_id = data.get("customer_id")

    if not customer_id:
        return jsonify(success=False, error="customer_id required"), 400

    db = get_db_connection()
    db.execute("""
        UPDATE customers
        SET review_requested = 'YES'
        WHERE id = ?
    """, (customer_id,))
    db.commit()

    return jsonify(success=True)


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5001, debug=False)



