"""Database for the expense pilot."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from .config import DB_URL, ROOT

# Schema creation and SQLite connection settings live here. Supplier linking is
# imported lazily by init_db to keep the database and supplier modules separate.

def connect():
    # Adapter boundary: local SQLite now; future PostgreSQL adapter can consume
    # DATABASE_URL while preserving the schema and application operations.
    if not DB_URL.startswith("sqlite:///"):
        raise RuntimeError("This pilot currently runs on SQLite. See README for the PostgreSQL migration seam.")
    path = Path(DB_URL.removeprefix("sqlite:///"))
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers (
          id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '', cost_center TEXT NOT NULL DEFAULT '',
          default_currency TEXT NOT NULL DEFAULT 'EUR', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supplier_profiles (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL UNIQUE,
          address TEXT NOT NULL DEFAULT '', oib TEXT NOT NULL DEFAULT '', vat_id TEXT NOT NULL DEFAULT '',
          iban TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bills (
          id INTEGER PRIMARY KEY, supplier TEXT NOT NULL DEFAULT '', invoice_number TEXT NOT NULL DEFAULT '',
          invoice_date TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', amount REAL NOT NULL DEFAULT 0,
          currency TEXT NOT NULL DEFAULT 'EUR', category TEXT NOT NULL DEFAULT '', cost_center TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '', original_name TEXT NOT NULL DEFAULT '',
          stored_path TEXT NOT NULL DEFAULT '', raw_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Needs review',
          transaction_id INTEGER, created_at TEXT NOT NULL, extraction_method TEXT NOT NULL DEFAULT '',
          supplier_address TEXT NOT NULL DEFAULT '', supplier_oib TEXT NOT NULL DEFAULT '',
          supplier_vat_id TEXT NOT NULL DEFAULT '', supplier_iban TEXT NOT NULL DEFAULT '', vat_amount REAL NOT NULL DEFAULT 0,
          payment_model TEXT NOT NULL DEFAULT '', payment_reference TEXT NOT NULL DEFAULT '',
          payment_purpose TEXT NOT NULL DEFAULT '', payment_description TEXT NOT NULL DEFAULT '',
          supplier_profile_id INTEGER, decision_reason TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL DEFAULT '',
          requires_approval INTEGER NOT NULL DEFAULT 0, workflow_route TEXT NOT NULL DEFAULT '',
          payment_plan TEXT NOT NULL DEFAULT '', scheduled_pay_date TEXT NOT NULL DEFAULT '',
          submitter_response TEXT NOT NULL DEFAULT '', paid_at TEXT NOT NULL DEFAULT '',
          document_type TEXT NOT NULL DEFAULT '', legacy_photo_path TEXT NOT NULL DEFAULT '',
          is_legacy INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bill_items (
          id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL, item_index INTEGER NOT NULL,
          description TEXT NOT NULL DEFAULT '', quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT '',
          unit_price REAL NOT NULL DEFAULT 0, discount TEXT NOT NULL DEFAULT '', vat_rate REAL NOT NULL DEFAULT 0,
          amount REAL NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bill_comments (
          id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL, author_id INTEGER,
          author_name TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE,
          FOREIGN KEY(author_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS bill_comment_reads (
          comment_id INTEGER NOT NULL, user_id INTEGER NOT NULL, read_at TEXT NOT NULL,
          PRIMARY KEY(comment_id,user_id),
          FOREIGN KEY(comment_id) REFERENCES bill_comments(id) ON DELETE CASCADE,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bill_status_history (
          id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL, status TEXT NOT NULL,
          occurred_at TEXT NOT NULL, actor_id INTEGER, note TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE,
          FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS bill_documents (
          id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL, document_type TEXT NOT NULL,
          original_name TEXT NOT NULL DEFAULT '', stored_path TEXT NOT NULL DEFAULT '',
          added_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS transactions (
          id INTEGER PRIMARY KEY, txn_date TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL,
          currency TEXT NOT NULL DEFAULT 'EUR', reference TEXT NOT NULL DEFAULT '', matched_bill_id INTEGER,
          source_file TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT 'outgoing',
          kind TEXT NOT NULL DEFAULT 'unclassified', signed_amount REAL NOT NULL DEFAULT 0,
          external_key TEXT NOT NULL DEFAULT '', counterparty TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(matched_bill_id) REFERENCES bills(id)
        );
        CREATE TABLE IF NOT EXISTS payment_allocations (
          id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL, bill_id INTEGER NOT NULL,
          amount REAL NOT NULL, allocation_type TEXT NOT NULL,
          FOREIGN KEY(transaction_id) REFERENCES transactions(id), FOREIGN KEY(bill_id) REFERENCES bills(id)
        );
        CREATE TABLE IF NOT EXISTS bank_snapshots (
          id INTEGER PRIMARY KEY, account_name TEXT NOT NULL DEFAULT 'Main account',
          as_of_date TEXT NOT NULL, closing_balance REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR',
          source_file TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_statement_periods (
          id INTEGER PRIMARY KEY, account_ref TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT 'EUR',
          period_start TEXT NOT NULL, period_end TEXT NOT NULL, source_file TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS other_costs (
          id INTEGER PRIMARY KEY, txn_date TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL,
          currency TEXT NOT NULL DEFAULT 'EUR', category TEXT NOT NULL DEFAULT 'Unclassified',
          source_file TEXT NOT NULL DEFAULT '', transaction_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'submitter',
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL, csrf_token TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, user_id INTEGER,
          username TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budget_plan (
          year INTEGER NOT NULL, month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
          version TEXT NOT NULL DEFAULT 'Budget',
          category TEXT NOT NULL, currency TEXT NOT NULL, amount REAL NOT NULL CHECK(amount>=0),
          updated_at TEXT NOT NULL, updated_by INTEGER,
          PRIMARY KEY(year,version,month,category,currency),
          FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS invitations (
          id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
          invited_by INTEGER, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          accepted_at TEXT NOT NULL DEFAULT '', accepted_user_id INTEGER, revoked_at TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(invited_by) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY(accepted_user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bills_fingerprint ON bills(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_txns_date ON transactions(txn_date);
        """)
        if 'version' not in {r['name'] for r in db.execute('PRAGMA table_info(budget_plan)')}:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE budget_plan_versioned (
                  year INTEGER NOT NULL, month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                  version TEXT NOT NULL DEFAULT 'Budget', category TEXT NOT NULL, currency TEXT NOT NULL,
                  amount REAL NOT NULL CHECK(amount>=0), updated_at TEXT NOT NULL, updated_by INTEGER,
                  PRIMARY KEY(year,version,month,category,currency),
                  FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
                );
                INSERT INTO budget_plan_versioned(year,month,category,currency,amount,updated_at,updated_by)
                  SELECT year,month,category,currency,amount,updated_at,updated_by FROM budget_plan;
                DROP TABLE budget_plan;
                ALTER TABLE budget_plan_versioned RENAME TO budget_plan;
                COMMIT;
            """)
        cols={r['name'] for r in db.execute('PRAGMA table_info(bills)')}
        if 'extraction_method' not in cols:
            db.execute("ALTER TABLE bills ADD COLUMN extraction_method TEXT NOT NULL DEFAULT ''")
        transaction_cols={r['name'] for r in db.execute('PRAGMA table_info(transactions)')}
        for name,declaration in {'direction':"TEXT NOT NULL DEFAULT 'outgoing'",'kind':"TEXT NOT NULL DEFAULT 'unclassified'",'signed_amount':"REAL NOT NULL DEFAULT 0",'external_key':"TEXT NOT NULL DEFAULT ''",'counterparty':"TEXT NOT NULL DEFAULT ''"}.items():
            if name not in transaction_cols:db.execute(f'ALTER TABLE transactions ADD COLUMN {name} {declaration}')
        for name, declaration in {
            'supplier_address': "TEXT NOT NULL DEFAULT ''", 'supplier_oib': "TEXT NOT NULL DEFAULT ''",
            'supplier_vat_id': "TEXT NOT NULL DEFAULT ''", 'supplier_iban': "TEXT NOT NULL DEFAULT ''",
            'vat_amount': "REAL NOT NULL DEFAULT 0",
            'payment_model': "TEXT NOT NULL DEFAULT ''", 'payment_reference': "TEXT NOT NULL DEFAULT ''",
            'payment_purpose': "TEXT NOT NULL DEFAULT ''", 'payment_description': "TEXT NOT NULL DEFAULT ''",
            'supplier_profile_id': "INTEGER",
            'decision_reason': "TEXT NOT NULL DEFAULT ''", 'decided_at': "TEXT NOT NULL DEFAULT ''",
            'created_by': "INTEGER", 'validated_by': "INTEGER",
            'requires_approval': "INTEGER NOT NULL DEFAULT 0",
            'workflow_route': "TEXT NOT NULL DEFAULT ''",
            'payment_plan': "TEXT NOT NULL DEFAULT ''", 'scheduled_pay_date': "TEXT NOT NULL DEFAULT ''",
            'submitter_response': "TEXT NOT NULL DEFAULT ''", 'paid_at': "TEXT NOT NULL DEFAULT ''",
            'document_type': "TEXT NOT NULL DEFAULT ''",
            'legacy_photo_path': "TEXT NOT NULL DEFAULT ''", 'is_legacy': "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in cols:
                db.execute(f"ALTER TABLE bills ADD COLUMN {name} {declaration}")
        # Keep legacy app records visible under the current workflow vocabulary.
        db.execute("UPDATE bills SET status='Pending for approval' WHERE status IN ('Pending approval','Needs details')")
        db.execute("UPDATE bills SET status='Ready for payment' WHERE status IN ('Validated','Approved for payment')")
        db.execute("UPDATE bills SET status='Paid at bank' WHERE status='Paid'")
        db.execute("UPDATE bills SET status='Order of payment issued' WHERE status='Order of payment issues'")
        db.execute("UPDATE bill_status_history SET status='Order of payment issued' WHERE status='Order of payment issues'")
        # Seed history for records created before the timeline was introduced.
        db.execute('''INSERT INTO bill_status_history(bill_id,status,occurred_at,note)
            SELECT b.id,b.status,COALESCE(NULLIF(b.decided_at,''),NULLIF(b.paid_at,''),b.created_at),'Existing status'
            FROM bills b WHERE NOT EXISTS (SELECT 1 FROM bill_status_history h WHERE h.bill_id=b.id)''')
        for profile in db.execute("SELECT id,oib FROM supplier_profiles WHERE oib<>''").fetchall():
            normalized_oib=re.sub(r'\D','',profile['oib'])
            if normalized_oib!=profile['oib']:db.execute('UPDATE supplier_profiles SET oib=? WHERE id=?',(normalized_oib,profile['id']))
        duplicate_oibs=db.execute("SELECT oib,MIN(id) keeper FROM supplier_profiles WHERE oib<>'' GROUP BY oib HAVING COUNT(*)>1").fetchall()
        for group in duplicate_oibs:
            duplicates=db.execute('SELECT id FROM supplier_profiles WHERE oib=? AND id<>?',(group['oib'],group['keeper'])).fetchall()
            for duplicate in duplicates:
                db.execute('UPDATE bills SET supplier_profile_id=? WHERE supplier_profile_id=?',(group['keeper'],duplicate['id']))
                db.execute('DELETE FROM supplier_profiles WHERE id=?',(duplicate['id'],))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_oib_unique ON supplier_profiles(oib) WHERE oib<>''")
        supplier_cols={r['name'] for r in db.execute('PRAGMA table_info(suppliers)')}
        for name,declaration in {
            'supplier_profile_id':'INTEGER','supplier_oib':"TEXT NOT NULL DEFAULT ''",
            'supplier_iban':"TEXT NOT NULL DEFAULT ''",'supplier_address':"TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in supplier_cols:db.execute(f'ALTER TABLE suppliers ADD COLUMN {name} {declaration}')
        db.execute("INSERT INTO app_settings(key,value) VALUES('approval_threshold','150') ON CONFLICT(key) DO UPDATE SET value='150'")
        invite_cols={r['name'] for r in db.execute('PRAGMA table_info(invitations)')}
        if 'revoked_at' not in invite_cols:db.execute("ALTER TABLE invitations ADD COLUMN revoked_at TEXT NOT NULL DEFAULT ''")
        for b in db.execute("SELECT * FROM bills WHERE supplier_profile_id IS NULL AND supplier<>''").fetchall():
            from .supplier_services import get_or_create_supplier
            sid=get_or_create_supplier(db,b['supplier'],b['supplier_address'],b['supplier_oib'],b['supplier_vat_id'],b['supplier_iban'])
            db.execute('UPDATE bills SET supplier_profile_id=? WHERE id=?',(sid,b['id']))

