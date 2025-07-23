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

ADMIN_PASSWORD = "fresh@123"  # Change this as needed
app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Replace with a strong secret in production

# Simple user store (can be expanded later)
USERS = {
    'admin': 'password123'  # Change this to your preferred username and password
}

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


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Handle form submission
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
                    INSERT INTO customers (name, phone, email_address, address, customer_type)
                    VALUES (?, ?, ?, ?, ?)""",
                    (name, phone, email, address, cust_type))
            conn.commit()
            return redirect("/customers")

        # Handle edit
        if request.args.get("edit"):
            edit_id = request.args.get("edit")
            customer = conn.execute("SELECT * FROM customers WHERE id = ?", (edit_id,)).fetchone()
        else:
            customer = None

        # Handle delete
        if request.args.get("delete"):
            del_id = request.args.get("delete")
            conn.execute("DELETE FROM customers WHERE id = ?", (del_id,))
            conn.commit()
            return redirect("/customers")

        customers = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()

    return render_template("customers.html", customers=customers, edit_customer=customer)
@app.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    if request.method == "POST":
        data = request.form

        # Customer handling
        phone = data.get("customerPhone").strip()
        name = data.get("customerName").strip()
        pickup_address = data.get("pickupAddress").strip()

        conn = sqlite3.connect(DB_PATH)
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
        advance_paid = float(data.get("advancePaid") or 0)
        balance_amount = float(data.get("balanceAmount") or 0)

        cur.execute("""
            INSERT INTO bills (
                customer_id, date, total, pickup_date, pickup_time,
                delivery_date, dropoff_time, pickup_address,
                order_type, discount_type, discount_value,
                advance_paid, balance_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            customer_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            0, "", "", delivery_date, "", pickup_address,
            order_type, discount_type, discount_value,
            advance_paid, balance_amount
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


        cur.execute("UPDATE bills SET total = ? WHERE id = ?", (grand_total, bill_id))
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

    # ✅ Final Bill = Subtotal - Discount
    final_total = subtotal - discount_amount

    # ✅ Generate QR for final total
    qr_filename = f"qr_{bill_id}.png"
    qr_path = os.path.join("static/qr", qr_filename)
    generate_upi_qr("freshthreads0549@iob", "Fresh Threads Laundry", final_total, qr_path)
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
    cur = conn.cursor()
    cur.execute("UPDATE bills SET void = 1 WHERE id = ?", (bill_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Bill #{bill_id} cancelled"})

@app.route("/bill/<int:bill_id>/embed")
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
        ORDER BY b.date ASC
    """)
    rows = cur.fetchall()
    return jsonify([dict(row) for row in rows])

@app.route("/api/mark-paid", methods=["POST"])
def mark_paid():
    bill_id = request.json.get("bill_id")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE bill_status SET payment_status = 'Paid' WHERE bill_id = ?", (bill_id,))
    conn.commit()
    return jsonify({"success": True, "message": f"Bill #{bill_id} marked as Paid"})

@app.route("/api/mark-delivered", methods=["POST"])
def mark_delivered():
    bill_id = request.json.get("bill_id")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE bill_status SET delivery_status = 'Delivered' WHERE bill_id = ?", (bill_id,))
    conn.commit()
    return jsonify({"success": True, "message": f"Bill #{bill_id} marked as Delivered"})

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
    q = request.args.get("q", "")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM expense_types WHERE name LIKE ?", (f"%{q}%",))
    results = [{"id": row["id"], "name": row["name"]} for row in cur.fetchall()]
    conn.close()
    return jsonify(results)

@app.route("/api/expense-types", methods=["POST"])
def add_expense_type():
    name = request.form.get("name")
    if not name:
        return jsonify(success=False, error="Missing name")

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO expense_types (name) VALUES (?)", (name,))
        conn.commit()
        id = cur.lastrowid
        return jsonify(success=True, id=id)
    except sqlite3.IntegrityError:
        return jsonify(success=False, error="Expense type already exists")
    finally:
        conn.close()
# === Expenses ===
@app.route("/api/expenses", methods=["POST"])
def add_expense():
    expense_type_id = request.form.get("expense_type_id")
    amount = request.form.get("amount")
    date = request.form.get("date")
    notes = request.form.get("notes", "")

    if not all([expense_type_id, amount, date]):
        return jsonify(success=False, error="Missing fields")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO expenses (expense_type_id, amount, date, notes)
        VALUES (?, ?, ?, ?)
    """, (expense_type_id, amount, date, notes))
    conn.commit()
    conn.close()
    return jsonify(success=True)
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

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))
if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
