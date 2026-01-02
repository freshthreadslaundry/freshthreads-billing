from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from functools import wraps
import sqlite3
from datetime import datetime, timedelta
from flask import jsonify
import qrcode
import os
from flask import render_template, send_from_directory
import pdfkit
import os
import pandas as pd
import secrets
from flask import jsonify
from flask_cors import CORS



ADMIN_PASSWORD = "123"  # Change this as needed
# === Customer Insights thresholds (tune anytime) ===
VIP_MIN_SPEND = 5000          # ₹ threshold for VIP / review candidates
LAPSED_DAYS = 45              # inactive if not visited in N days
MIN_VISITS_FOR_REVIEW = 2     # prefer repeat customers for review ask

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Replace with a strong secret in production
CORS(app, resources={r"/api/*": {"origins": "*"}})  # allow cross-origin calls from Netlify
# Simple user store (can be expanded later)
USERS = {
    'admin': 'password123'  # Change this to your preferred username and password
}

def require_internal_secret():
    expected = os.environ.get("FT_INTERNAL_SECRET", "")
    got = request.headers.get("X-FT-SECRET", "")
    return bool(expected) and secrets.compare_digest(got, expected)


def generate_token():
    return secrets.token_urlsafe(10)  # ~16 char secure URL-safe token

def apply_subscription_credit_for_bill(bill_id, conn=None):
    """Apply subscription wallet credit for a PAID subscription bill.
    If `conn` is provided, uses the same connection/transaction (recommended) to avoid SQLite locks.
    """
    own_conn = False
    if conn is None:
        conn = get_db_connection()
        own_conn = True
    cur = conn.cursor()

    # 1. Check if this bill contains a subscription service
    cur.execute("""
        SELECT bi.service_id, bi.qty, sp.credit_amount, b.customer_id
        FROM bill_items bi
        JOIN subscription_products sp
            ON sp.service_id = bi.service_id
           AND sp.is_active = 1
        JOIN bills b ON b.id = bi.bill_id
        WHERE bi.bill_id = ?
    """, (bill_id,))
    rows = cur.fetchall()

    if not rows:
        conn.close()
        return  # not a subscription bill

    customer_id = rows[0]["customer_id"]

    # 2. Prevent double credit
    cur.execute("""
        SELECT 1 FROM subscription_ledger
        WHERE bill_id = ? AND txn_type = 'CREDIT'
        LIMIT 1
    """, (bill_id,))
    if cur.fetchone():
        conn.close()
        return

    total_credit = sum(r["credit_amount"] * r["qty"] for r in rows)

    # 3. Ensure customer_subscriptions exists
    cur.execute("""
        INSERT OR IGNORE INTO customer_subscriptions
        (customer_id, current_balance, is_active)
        VALUES (?, 0, 1)
    """, (customer_id,))

    # 4. Insert ledger CREDIT
    cur.execute("""
        INSERT INTO subscription_ledger
        (customer_id, bill_id, txn_type, amount, notes)
        VALUES (?, ?, 'CREDIT', ?, 'Subscription unlocked after payment')
    """, (customer_id, bill_id, total_credit))

    # 5. Update cached balance
    cur.execute("""
        UPDATE customer_subscriptions
        SET current_balance = current_balance + ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE customer_id = ?
    """, (total_credit, customer_id))

    if own_conn:
        conn.commit()
        conn.close()


def _is_subscription_bill(conn, bill_id: int) -> bool:
    row = conn.execute(
        """SELECT 1
             FROM bill_items bi
             JOIN subscription_products sp
               ON sp.service_id = bi.service_id
              AND sp.is_active = 1
            WHERE bi.bill_id = ?
            LIMIT 1""",
        (bill_id,),
    ).fetchone()
    return bool(row)

def _get_wallet_balance(conn, customer_id: int) -> float:
    row = conn.execute(
        "SELECT current_balance FROM customer_subscriptions WHERE customer_id = ? AND is_active = 1",
        (customer_id,),
    ).fetchone()
    return float(row[0] or 0.0) if row else 0.0

def _ensure_wallet_row(conn, customer_id: int):
    conn.execute(
        """INSERT OR IGNORE INTO customer_subscriptions (customer_id, current_balance, is_active)
             VALUES (?, 0, 1)""",
        (customer_id,),
    )

def apply_subscription_debit_for_bill(bill_id: int, customer_id: int, amount: float, conn):
    """Debits subscription wallet for a bill (DEBIT), using the SAME connection/transaction.
    Idempotent: will not insert another DEBIT for the same bill if one already exists.
    """
    if amount <= 0:
        return 0.0

    # Block redemption on subscription purchase bills
    if _is_subscription_bill(conn, bill_id):
        return 0.0

    # Prevent double-debit
    already = conn.execute(
        """SELECT 1 FROM subscription_ledger
             WHERE bill_id = ? AND txn_type = 'DEBIT'
             LIMIT 1""",
        (bill_id,),
    ).fetchone()
    if already:
        return 0.0

    bal = _get_wallet_balance(conn, customer_id)
    use = min(float(amount), float(bal))
    if use <= 0:
        return 0.0

    _ensure_wallet_row(conn, customer_id)

    conn.execute(
        """INSERT INTO subscription_ledger (customer_id, bill_id, txn_type, amount, notes)
             VALUES (?, ?, 'DEBIT', ?, ?)""",
        (customer_id, bill_id, use, f"Subscription wallet used on bill #{bill_id}"),
    )
    conn.execute(
        """UPDATE customer_subscriptions
             SET current_balance = current_balance - ?,
                 updated_at = CURRENT_TIMESTAMP
             WHERE customer_id = ?""",
        (use, customer_id),
    )
    return use

def reverse_subscription_debit_for_bill(bill_id: int, conn):
    """If bill had a wallet DEBIT, insert REVERSAL once and restore balance."""
    debit_row = conn.execute(
        """SELECT customer_id, SUM(amount) AS amt
             FROM subscription_ledger
             WHERE bill_id = ? AND txn_type = 'DEBIT'
             GROUP BY customer_id""",
        (bill_id,),
    ).fetchone()
    if not debit_row:
        return 0.0

    customer_id = int(debit_row["customer_id"])
    debit_amt = float(debit_row["amt"] or 0.0)
    if debit_amt <= 0:
        return 0.0

    # Idempotent: do not reverse twice
    rev_exists = conn.execute(
        """SELECT 1 FROM subscription_ledger
             WHERE bill_id = ? AND txn_type = 'REVERSAL'
             LIMIT 1""",
        (bill_id,),
    ).fetchone()
    if rev_exists:
        return 0.0

    _ensure_wallet_row(conn, customer_id)
    conn.execute(
        """INSERT INTO subscription_ledger (customer_id, bill_id, txn_type, amount, notes)
             VALUES (?, ?, 'REVERSAL', ?, ?)""",
        (customer_id, bill_id, debit_amt, f"Reversal for cancelled bill #{bill_id}"),
    )
    conn.execute(
        """UPDATE customer_subscriptions
             SET current_balance = current_balance + ?,
                 updated_at = CURRENT_TIMESTAMP
             WHERE customer_id = ?""",
        (debit_amt, customer_id),
    )
    return debit_amt

def get_wallet_used_for_bill(conn, bill_id: int) -> float:
    """Net wallet used on a bill = DEBIT - REVERSAL (never negative)."""
    row = conn.execute(
        """SELECT
               COALESCE(SUM(CASE WHEN txn_type='DEBIT' THEN amount ELSE 0 END), 0) AS deb,
               COALESCE(SUM(CASE WHEN txn_type='REVERSAL' THEN amount ELSE 0 END), 0) AS rev
             FROM subscription_ledger
             WHERE bill_id = ? AND txn_type IN ('DEBIT','REVERSAL')""",
        (bill_id,),
    ).fetchone()
    deb = float(row["deb"] or 0.0)
    rev = float(row["rev"] or 0.0)
    return max(0.0, deb - rev)

# Decorator to enforce login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

DB_PATH = "database/fresh_threads.db"

def generate_upi_qr(upi_id, name, amount, qr_path):
    upi_url = f"upi://pay?pa={upi_id}&pn={name}&am={amount:.2f}&cu=INR"
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

@app.route("/api/services")
def api_services():
    query = request.args.get("q", "")
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM services
            WHERE name LIKE ?
            LIMIT 10
        """, (f"%{query}%",)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/subscription-status")
@login_required
def api_subscription_status():
    """Return subscription wallet status for a customer by phone.
    Does NOT auto-create wallet rows. Balance is 0 if customer has no wallet row.
    """
    phone = (request.args.get("phone") or "").strip()
    if not phone:
        return jsonify(success=False, error="phone is required"), 400

    with get_db_connection() as conn:
        cust = conn.execute("SELECT id, name, phone FROM customers WHERE phone = ? LIMIT 1", (phone,)).fetchone()
        if not cust:
            return jsonify(success=True, exists=False, balance=0.0, is_active=False)

        wallet = conn.execute(
            "SELECT current_balance, is_active, updated_at FROM customer_subscriptions WHERE customer_id = ?",
            (cust["id"],),
        ).fetchone()

        if wallet:
            balance = float(wallet["current_balance"] or 0)
            is_active = bool(wallet["is_active"])
            updated_at = wallet["updated_at"]
        else:
            balance = 0.0
            is_active = False
            updated_at = None

    return jsonify(
        success=True,
        exists=True,
        customer_id=int(cust["id"]),
        name=cust["name"],
        phone=cust["phone"],
        balance=balance,
        is_active=is_active,
        updated_at=updated_at,
    )


def get_db_connection():
    # Use a generous timeout + WAL to reduce 'database is locked' errors on Windows
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
    except Exception:
        pass
    return conn

@app.route("/")
@login_required
def home():
    return render_template("home.html")


@app.route("/services", methods=["GET", "POST"])
@login_required
def services():
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Save service (insert or update)
        if request.method == "POST":
            service_id = request.form.get("service_id")
            name = request.form.get("name")
            rate = float(request.form.get("rate"))

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

        # Delete
        if request.args.get("delete"):
            service_id = request.args.get("delete")
            conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
            conn.commit()
            return redirect("/services")

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

        # Handle delete
        if request.args.get("delete"):
            del_id = request.args.get("delete")
            conn.execute("DELETE FROM customers WHERE id = ?", (del_id,))
            conn.commit()
            return redirect(url_for("customers", view=view, q=q))

        # --- Customer stats (spend, visits, last visit, etc.) ---
        # Note: bills.total is your stored subtotal. We exclude void bills.
        stats_sql = """
        WITH customer_stats AS (
            SELECT
                c.id, c.name, c.phone, c.email_address, c.address, c.customer_type,
                c.review_requested, c.review_stars,
                COUNT(b.id) AS visit_count,
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




# ------------------------------------------------------------
# Subscription Wallet: apply credits for PAID subscription bills
# Rule: credit is unlocked ONLY after bill_status.payment_status becomes 'Paid'
# No changes to bills table; all audit trail is via subscription_ledger
# ------------------------------------------------------------

def _ensure_customer_wallet_row(conn, customer_id: int):
    conn.execute(
        """INSERT OR IGNORE INTO customer_subscriptions (customer_id, current_balance, is_active)
           VALUES (?, 0, 1)""",
        (customer_id,),
    )

def _compute_subscription_credit_for_bill(conn, bill_id: int) -> float:
    row = conn.execute(
        """SELECT COALESCE(SUM(sp.credit_amount * COALESCE(NULLIF(bi.qty, 0), 1)), 0) AS credit
             FROM bill_items bi
             JOIN subscription_products sp
               ON sp.service_id = bi.service_id
              AND sp.is_active = 1
            WHERE bi.bill_id = ?""",
        (bill_id,),
    ).fetchone()
    return float(row[0] or 0)

@app.route("/api/subscription/apply-paid-credits", methods=["POST"])
@login_required
def api_apply_paid_subscription_credits():
    """
    Finds PAID bills that include subscription services (service_id present in subscription_products)
    and applies wallet CREDIT exactly once per bill (idempotent).
    """
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Find eligible paid bills that have subscription items and are not yet credited
        bills = conn.execute(
            """SELECT b.id AS bill_id, b.customer_id
                 FROM bills b
                 JOIN bill_status bs ON bs.bill_id = b.id
                WHERE bs.payment_status = 'Paid'
                  AND EXISTS (
                      SELECT 1
                        FROM bill_items bi
                        JOIN subscription_products sp
                          ON sp.service_id = bi.service_id
                         AND sp.is_active = 1
                       WHERE bi.bill_id = b.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                        FROM subscription_ledger sl
                       WHERE sl.bill_id = b.id
                         AND sl.txn_type = 'CREDIT'
                  )
                ORDER BY b.id ASC"""
        ).fetchall()

        processed = []
        for r in bills:
            bill_id = int(r["bill_id"])
            customer_id = int(r["customer_id"])

            credit = _compute_subscription_credit_for_bill(conn, bill_id)
            if credit <= 0:
                continue

            _ensure_customer_wallet_row(conn, customer_id)

            # Insert ledger CREDIT (audit trail)
            conn.execute(
                """INSERT INTO subscription_ledger (customer_id, bill_id, txn_type, amount, notes)
                     VALUES (?, ?, 'CREDIT', ?, ?)""",
                (customer_id, bill_id, credit, f"Subscription credit unlocked for PAID bill #{bill_id}"),
            )

            # Update cached balance
            conn.execute(
                """UPDATE customer_subscriptions
                       SET current_balance = current_balance + ?,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE customer_id = ?""",
                (credit, customer_id),
            )

            processed.append({"bill_id": bill_id, "customer_id": customer_id, "credit": credit})

        conn.commit()
        return jsonify(success=True, processed_count=len(processed), processed=processed)

    except Exception as e:
        conn.rollback()
        return jsonify(success=False, error=str(e)), 500
    finally:
        conn.close()
@app.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    if request.method == "POST":
        data = request.form

        # Customer handling
        phone = data.get("customerPhone").strip()
        name = data.get("customerName").strip()
        pickup_address = data.get("pickupAddress").strip()

        conn = get_db_connection()
        cur = conn.cursor()

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
        totals = data.getlist("total[]")
        pieces_list = data.getlist("pieces[]")

        grand_total = 0
        for i in range(len(services)):
            name = services[i]
            qty = float(qtys[i] or 0)
            rate = float(rates[i] or 0)
            total = float(totals[i] or 0)
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

        final_total = max(0.0, subtotal - discount_amount + surcharge_amount)
        balance_amount = max(0.0, final_total - advance_paid)

        # ---- Subscription Wallet redemption (optional) ----
        # Rules:
        #  - Only redeem on NON-subscription bills
        #  - Debit up to wallet balance and up to final_total
        use_subscription = str(data.get("useSubscription") or "").lower() in ("on", "1", "true", "yes")
        requested_use = float(data.get("subscriptionUseAmount") or 0)

        wallet_used = 0.0
        if use_subscription and requested_use > 0:
            # First, ensure this bill is not a subscription purchase bill
            cur.execute(
                """SELECT 1
                     FROM bill_items bi
                     JOIN subscription_products sp
                       ON sp.service_id = bi.service_id
                      AND sp.is_active = 1
                    WHERE bi.bill_id = ?
                    LIMIT 1""",
                (bill_id,),
            )
            is_sub_bill = bool(cur.fetchone())

            if not is_sub_bill:
                # Current wallet balance
                cur.execute(
                    "SELECT current_balance FROM customer_subscriptions WHERE customer_id = ? AND is_active = 1",
                    (customer_id,),
                )
                wrow = cur.fetchone()
                wallet_balance = float(wrow[0] or 0.0) if wrow else 0.0

                wallet_used = min(requested_use, wallet_balance, final_total)
                if wallet_used > 0:
                    # Idempotency: prevent double debit for same bill
                    cur.execute(
                        """SELECT 1 FROM subscription_ledger
                             WHERE bill_id = ? AND txn_type = 'DEBIT'
                             LIMIT 1""",
                        (bill_id,),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """INSERT INTO subscription_ledger
                                   (customer_id, bill_id, txn_type, amount, notes)
                                 VALUES (?, ?, 'DEBIT', ?, ?)""",
                            (customer_id, bill_id, wallet_used, f"Wallet used at billing for bill #{bill_id}"),
                        )
                        cur.execute(
                            """UPDATE customer_subscriptions
                                   SET current_balance = current_balance - ?,
                                       updated_at = CURRENT_TIMESTAMP
                                 WHERE customer_id = ?""",
                            (wallet_used, customer_id),
                        )

                    # Reduce payable
                    final_total = max(0.0, final_total - wallet_used)
                    balance_amount = max(0.0, final_total - advance_paid)

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
        conn.commit()
        conn.close()

        return redirect(url_for("bill_view", bill_id=bill_id))

    return render_template("index.html")

@app.route("/api/generate-bill", methods=["POST"])
def generate_bill():
    # For now, just respond with a fake bill
    return jsonify({
        "success": True,
        "bill_id": f"INV{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "bill_date": datetime.now().strftime("%Y-%m-%d")
    })


@app.route("/bill/<int:bill_id>")
@login_required
def bill_view(bill_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE b.id = ?
    """, (bill_id,))
    row = cur.fetchone()

    cur.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,))
    items = cur.fetchall()
    conn.close()

    if row is None:
        abort(404)

    bill = dict(row)
    bill_is_cancelled = bool(bill.get("void")) and int(bill.get("void")) == 1
    bill["is_cancelled"] = bill_is_cancelled
    # Format date
    try:
        dt = datetime.strptime(bill['date'], "%Y-%m-%d %H:%M:%S")
        bill['formatted_date'] = dt.strftime("%d-%b-%Y")
    except Exception:
        bill['formatted_date'] = bill['date']
  
    try:
        dt1 = datetime.strptime(bill['delivery_date'], "%Y-%m-%d")
        bill['formatted_due_date'] = dt1.strftime("%d-%b-%Y")
    except Exception:
        bill['formatted_due_date'] = bill['delivery_date']

    # ✅ Calculate subtotal from items
    subtotal = sum(float(item['qty']) * float(item['rate']) for item in items)

    # ✅ Calculate discount
    discount_value = float(bill['discount_value']) if bill.get('discount_value') else 0.0
    if bill.get('discount_type') == "%":
        discount_amount = subtotal * discount_value / 100
    else:
        discount_amount = discount_value

    # ✅ Surcharge (Express)
    surcharge_amount = float(bill.get("surcharge_amount") or 0.0)
    # Backward compatibility: if old bill has type/value but no stored amount
    if surcharge_amount == 0 and bill.get("express_service") and float(bill.get("surcharge_value") or 0) > 0:
        s_type = bill.get("surcharge_type") or "Rs"
        s_val = float(bill.get("surcharge_value") or 0)
        surcharge_amount = subtotal * s_val / 100 if s_type == "%" else s_val

    # ✅ Final Bill = Subtotal - Discount + Surcharge
    final_total = max(0.0, subtotal - discount_amount + surcharge_amount)


    # ✅ Generate QR for final total
    qr_filename = f"qr_{bill_id}.png"
    qr_path = os.path.join("static/qr", qr_filename)
    wallet_used = 0.0
    try:
        with get_db_connection() as wconn:
            wallet_used = get_wallet_used_for_bill(wconn, bill_id)
    except Exception:
        wallet_used = 0.0

    payable_total = max(0.0, final_total - wallet_used)

    generate_upi_qr("freshthreads0549@iob", "Fresh Threads Laundry", payable_total, qr_path)
        # ✅ Fetch payment status
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT payment_status FROM bill_status WHERE bill_id = ?", (bill_id,))
    status = cur.fetchone()
    conn.close()

    # ✅ Set paid flag for template
    bill["is_paid"] = status and status["payment_status"] == "Paid"

    return render_template(
        "invoice.html",
        bill=bill,
        items=items,
        qr_image=qr_filename,
        subtotal=subtotal,
        discount_amount=discount_amount,
        surcharge_amount=surcharge_amount,
        final_total=final_total,
    )

@app.route('/invoice/pdf/<int:bill_id>')
@login_required
def generate_pdf(bill_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE b.id = ?
    """, (bill_id,))
    bill = dict(cur.fetchone())

    cur.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,))
    items = cur.fetchall()
    conn.close()

    rendered = render_template("invoice.html", bill=bill, items=items)
    config = pdfkit.configuration(wkhtmltopdf=r'\bin\wkhtmltopdf.exe')
    pdfkit.from_string(rendered, pdf_path, configuration=config)

    pdf_path = f"static/invoices/invoice_{bill_id}.pdf"
    pdfkit.from_string(rendered, pdf_path)  # Add config= if wkhtmltopdf is not in PATH

    return send_from_directory('static/invoices', f"invoice_{bill_id}.pdf")

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
            total_amount += float(b["total"])
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
    df = pd.DataFrame(rows)

    output_path = f"static/reports/report_{start_date}_to_{end_date}.xlsx"
    df.to_excel(output_path, index=False)

    return send_file(output_path, as_attachment=True)


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
        ORDER BY b.id
    """, (report_date,))
    rows = cur.fetchall()

    # ✅ Explicit column names
    columns = [desc[0] for desc in cur.description]

    import pandas as pd
    df = pd.DataFrame(rows, columns=columns)
    path = f"static/reports/daily_{report_date}.xlsx"
    df.to_excel(path, index=False)

    from flask import send_file
    return send_file(path, as_attachment=True)

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
    bill_id = request.json.get("bill_id")
    if not bill_id:
        return jsonify({"success": False, "message": "Bill ID is required"}), 400

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Mark void
        conn.execute("UPDATE bills SET void = 1 WHERE id = ?", (bill_id,))

        # If any wallet was used on this bill, reverse it once
        reversed_amt = reverse_subscription_debit_for_bill(int(bill_id), conn)

        conn.commit()
        msg = f"Bill #{bill_id} cancelled"
        if reversed_amt and reversed_amt > 0:
            msg += f" (wallet reversal ₹{reversed_amt:.2f})"
        return jsonify({"success": True, "message": msg})
    except sqlite3.OperationalError as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

@app.route("/bill/<int:bill_id>/embed")
@login_required
def bill_embed(bill_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE b.id = ?
    """, (bill_id,))
    bill = cur.fetchone()

    cur.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,))
    items = cur.fetchall()
    conn.close()

    if not bill:
        return "Bill not found", 404

    return render_template("invoice_embed.html", bill=bill, items=items)

@app.route("/api/outstanding-report")
def outstanding_report():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.id AS bill_id, b.delivery_date,
               c.name AS customer_name, c.phone AS customer_phone,
               sum(bi.items) as items, b.balance_amount,
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
    data = request.get_json(silent=True) or {}
    bill_id = data.get("bill_id")
    if not bill_id:
        return jsonify({"success": False, "message": "bill_id is required"}), 400

    conn = get_db_connection()
    try:
        # Take a write lock early, so we don't collide with other writes (reduces 'database is locked')
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE bill_status SET payment_status = 'Paid' WHERE bill_id = ?", (bill_id,))

        # Unlock subscription credit (if this is a subscription bill) using the SAME connection/transaction
        apply_subscription_credit_for_bill(bill_id, conn=conn)

        conn.commit()
        return jsonify({"success": True, "message": f"Bill #{bill_id} marked as Paid"})
    except sqlite3.OperationalError as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/mark-delivered", methods=["POST"])
def mark_delivered():
    data = request.get_json(silent=True) or {}
    bill_id = data.get("bill_id")
    if not bill_id:
        return jsonify({"success": False, "message": "bill_id is required"}), 400

    conn = get_db_connection()
    try:
        conn.execute("UPDATE bill_status SET delivery_status = 'Delivered' WHERE bill_id = ?", (bill_id,))
        conn.commit()
        return jsonify({"success": True, "message": f"Bill #{bill_id} marked as Delivered"})
    except sqlite3.OperationalError as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()

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

@app.route("/api/summary-daily")
def api_summary_daily():
    report_date = request.args.get("date")
    if not report_date:
        return jsonify({"error": "Date is required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS total_bills, SUM(balance_amount) AS total_sales
        FROM bills
        WHERE DATE(date) = ? AND IFNULL(void, 0) = 0
    """, (report_date,))
    row = cur.fetchone()
    conn.close()

    return jsonify({
        "total_bills": row["total_bills"] or 0,
        "total_sales": f"{(row['total_sales'] or 0):.2f}"
    })

@app.route("/api/summary-range")
def api_summary_range():
    start = request.args.get("start")
    end = request.args.get("end")

    if not start or not end:
        return jsonify({"error": "Start and End date required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS total_bills, SUM(balance_amount) AS total_sales
        FROM bills
        WHERE DATE(date) BETWEEN ? AND ? AND IFNULL(void, 0) = 0
    """, (start, end))
    row = cur.fetchone()
    conn.close()

    return jsonify({
        "total_bills": row["total_bills"] or 0,
        "total_sales": f"{(row['total_sales'] or 0):.2f}"
    })

@app.route("/api/range-report")
def api_range_report():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT b.id AS bill_id, b.date, c.name AS customer_name, c.phone AS customer_phone,b.balance_amount as total
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE DATE(b.date) BETWEEN ? AND ?
          AND IFNULL(b.void, 0) = 0
        ORDER BY b.date ASC, b.id ASC
    """, (start, end))

    rows = cur.fetchall()
    return jsonify([dict(row) for row in rows])
@app.route("/api/verify-password", methods=["POST"])
def verify_password():
    data = request.get_json()
    if not data or data.get("password") != ADMIN_PASSWORD:
        return jsonify({"success": False, "message": "Invalid password"}), 403
    return jsonify({"success": True})

@app.route("/api/range-summary-by-date")
def api_range_summary_by_date():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            DATE(date) AS report_date,
            COUNT(*) AS bill_count,
            SUM(balance_amount) AS total_sales,
            SUM(CASE WHEN order_type = 'Walk-in' THEN 1 ELSE 0 END) AS walkin_count,
            SUM(CASE WHEN order_type = 'Online' THEN 1 ELSE 0 END) AS online_count,
            SUM(CASE WHEN order_type = 'Walk-in' THEN balance_amount ELSE 0 END) AS walkin_sales,
            SUM(CASE WHEN order_type = 'Online' THEN balance_amount ELSE 0 END) AS online_sales
        FROM bills
        WHERE DATE(date) BETWEEN ? AND ? AND IFNULL(void, 0) = 0
        GROUP BY DATE(date)
        ORDER BY DATE(date)
    """, (start, end))
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

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
    data = request.get_json() or request.form
    db = get_db_connection()
    cur = db.execute("""
        INSERT INTO expenses (expense_type_id, amount, date, notes)
        VALUES (?, ?, ?, ?)
    """, (
        data["expense_type_id"],
        data["amount"],
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
        discount_type = request.form.get("discountType")
        discount_value = float(request.form.get("discountValue") or 0)

        # Fetch current total and advance_paid
        cur.execute("SELECT total, advance_paid FROM bills WHERE id = ?", (bill_id,))
        row = cur.fetchone()

        if not row:
            conn.close()
            return "Bill not found", 404

        total = float(row["total"])
        advance_paid = float(row["advance_paid"] or 0)

        # Calculate discount and new balance
        if discount_type == "%":
            discount_amount = total * discount_value / 100
        else:
            discount_amount = discount_value

        final_total = max(0, total - discount_amount)
        balance_amount = max(0, final_total - advance_paid)

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
    return render_template("edit_bill.html", bill=bill)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if USERS.get(username) == password:
            session['user'] = username
            return redirect(url_for('home'))  # or billing dashboard
        else:
            flash('Invalid username or password', 'danger')
    return render_template('login.html')
@app.route("/pay/<token>")
def customer_view(token):
    conn = get_db_connection()
    cur = conn.cursor()

    bill = cur.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE b.token = ?
    """, (token,)).fetchone()

    if not bill:
        return "Invalid or expired link", 404

    items = cur.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill["id"],)).fetchall()
    status = cur.execute("SELECT payment_status FROM bill_status WHERE bill_id = ?", (bill["id"],)).fetchone()
    conn.close()

    bill = dict(bill)
    bill["is_paid"] = status and status["payment_status"] == "Paid"

    # Format dates
    try:
        bill['formatted_date'] = datetime.strptime(bill['date'], "%Y-%m-%d %H:%M:%S").strftime("%d-%b-%Y")
    except:
        bill['formatted_date'] = bill['date']
    try:
        bill['formatted_due_date'] = datetime.strptime(bill['delivery_date'], "%Y-%m-%d").strftime("%d-%b-%Y")
    except:
        bill['formatted_due_date'] = bill['delivery_date']

    # Totals
    subtotal = sum(float(i['qty']) * float(i['rate']) for i in items)
    discount = float(bill['discount_value'] or 0)
    discount_amount = subtotal * discount / 100 if bill['discount_type'] == "%" else discount
    surcharge_amount = float(bill.get("surcharge_amount") or 0.0)
    if surcharge_amount == 0 and bill.get("express_service") and float(bill.get("surcharge_value") or 0) > 0:
        s_type = bill.get("surcharge_type") or "Rs"
        s_val = float(bill.get("surcharge_value") or 0)
        surcharge_amount = subtotal * s_val / 100 if s_type == "%" else s_val

    final_total = max(0.0, subtotal - discount_amount + surcharge_amount)

    # Generate QR
    qr_filename = f"qr_{bill['id']}.png"
    qr_path = os.path.join("static/qr", qr_filename)
    generate_upi_qr("freshthreads0549@iob", "Fresh Threads Laundry", final_total, qr_path)

    return render_template(
        "customer_invoice.html",
        bill=bill,
        items=items,
        qr_image=qr_filename,
        subtotal=subtotal,
        discount_amount=discount_amount,
        surcharge_amount=surcharge_amount,
        final_total=final_total,
    )

@app.route("/api/bill-info/<token>")
def api_bill_info(token):
    if not require_internal_secret():
        return jsonify({"error": "Forbidden"}), 403
    conn = get_db_connection()
    cur = conn.cursor()

    bill = cur.execute("""
        SELECT b.*, c.name AS customer_name, c.phone AS customer_phone
        FROM bills b
        JOIN customers c ON b.customer_id = c.id
        WHERE b.token = ?
    """, (token,)).fetchone()

    if not bill:
        return jsonify({"error": "Invalid token"}), 404

    status = cur.execute("SELECT payment_status FROM bill_status WHERE bill_id = ?", (bill["id"],)).fetchone()
    items = cur.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill["id"],)).fetchall()
    conn.close()

    return jsonify({
        "bill": dict(bill),
        "items": [dict(i) for i in items],
        "payment_status": status["payment_status"] if status else "Pending"
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
    data = request.get_json()
    db = get_db_connection()
    db.execute("""
        UPDATE expenses
        SET expense_type_id = ?, amount = ?, date = ?, notes = ?
        WHERE id = ?
    """, (
        data["expense_type_id"],
        data["amount"],
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
    app.run(host='0.0.0.0', port=5000, debug=False)



