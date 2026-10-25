CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    email_address TEXT,
    address TEXT,
    customer_type TEXT
, review_requested TEXT DEFAULT 'NO', review_stars INTEGER);
CREATE TABLE services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rate REAL NOT NULL
);
CREATE TABLE bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    date TEXT DEFAULT CURRENT_TIMESTAMP,
    total REAL,
    pickup_date TEXT,
    pickup_time TEXT,
    delivery_date TEXT,
    dropoff_time TEXT,
    pickup_address TEXT, order_type TEXT DEFAULT 'Walk-in', discount_type TEXT DEFAULT 'Amount', discount_value REAL DEFAULT 0, advance_paid REAL DEFAULT 0, balance_amount REAL DEFAULT 0, void INTEGER DEFAULT 0, token TEXT, express_service INTEGER DEFAULT 0, surcharge_type TEXT DEFAULT 'Amount', surcharge_value REAL DEFAULT 0, surcharge_amount REAL DEFAULT 0,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
CREATE TABLE bill_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    service_id INTEGER,
    service_name TEXT NOT NULL,
    qty REAL NOT NULL,  -- ✅ now allows decimals
    rate REAL NOT NULL,
    total REAL NOT NULL, items INTEGER DEFAULT 1,
    FOREIGN KEY (bill_id) REFERENCES bills(id),
    FOREIGN KEY (service_id) REFERENCES services(id)
);
CREATE TABLE bill_status (
            bill_id INTEGER PRIMARY KEY,
            payment_status TEXT DEFAULT 'Pending',
            delivery_status TEXT DEFAULT 'Not Delivered',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE);
CREATE TABLE expense_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_type_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (expense_type_id) REFERENCES expense_types(id)
);
CREATE TABLE subscription_products (
    service_id     INTEGER PRIMARY KEY,   -- links to services.id
    credit_amount  REAL    NOT NULL CHECK (credit_amount >= 0),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
);
CREATE TABLE customer_subscriptions (
    customer_id      INTEGER PRIMARY KEY,  -- one row per customer
    current_balance  REAL    NOT NULL DEFAULT 0 CHECK (current_balance >= 0),
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    updated_at       TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE TABLE subscription_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    bill_id     INTEGER,  -- nullable; link DEBIT/REVERSAL/CREDIT to bills.id
    txn_type    TEXT NOT NULL CHECK (txn_type IN ('CREDIT','DEBIT','REVERSAL','ADJUST')),
    amount      REAL NOT NULL CHECK (amount > 0),
    created_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    notes       TEXT,

    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE SET NULL
);
