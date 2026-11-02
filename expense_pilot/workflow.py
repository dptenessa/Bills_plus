"""Shared bill workflow rules and status history helpers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def local_timestamp():
    return datetime.now(ZoneInfo('Europe/Zagreb')).isoformat(timespec='seconds')

DOCUMENT_TYPES = {
    'invoice': 'Invoice',
    'minimax_invoice': 'Minimax invoice',
    'credit_card_charge': 'Credit card charge',
    'offer': 'Ponuda / Offer',
    'refund': 'Employee reimbursement',
}

PENDING = 'Pending for approval'
READY = 'Ready for payment'
ORDER_ISSUE = 'Order of payment issued'
PAID = 'Paid at bank'
REJECTED = 'Rejected'


def initial_status(document_type, amount, currency='EUR'):
    if document_type == 'credit_card_charge':
        return PAID
    if document_type == 'refund':
        return PENDING if float(amount or 0) > 150 else READY
    if document_type in ('invoice', 'minimax_invoice', 'offer'):
        return PENDING
    return 'Needs review'


def set_bill_status(db, bill_id, status, actor_id=None, note=''):
    current = db.execute('SELECT status FROM bills WHERE id=?', (bill_id,)).fetchone()
    if not current:
        return
    if current['status'] == status:
        return
    now = local_timestamp()
    db.execute('UPDATE bills SET status=? WHERE id=?', (status, bill_id))
    db.execute('INSERT INTO bill_status_history(bill_id,status,occurred_at,actor_id,note) VALUES(?,?,?,?,?)',
               (bill_id, status, now, actor_id, note[:500]))


def record_initial_status(db, bill_id, status, actor_id=None, note=''):
    now = local_timestamp()
    db.execute('INSERT INTO bill_status_history(bill_id,status,occurred_at,actor_id,note) VALUES(?,?,?,?,?)',
               (bill_id, status, now, actor_id, note[:500]))


def status_history(db, bill_id):
    return [dict(row) for row in db.execute('''SELECT h.*,u.username actor_name
        FROM bill_status_history h LEFT JOIN users u ON u.id=h.actor_id
        WHERE h.bill_id=? ORDER BY h.id''', (bill_id,)).fetchall()]


def offer_matches(db, supplier, supplier_oib, amount, currency=None, exclude_id=None):
    """Find paid offers by supplier and total within the requested ten percent."""
    params = [PAID, float(amount or 0) * .9, float(amount or 0) * 1.1]
    sql = "SELECT * FROM bills WHERE status=? AND document_type='offer' AND amount BETWEEN ? AND ?"
    if exclude_id:
        sql += ' AND id<>?'
        params.append(exclude_id)
    if currency:
        sql += ' AND currency=?'
        params.append(currency.upper())
    rows = db.execute(sql + ' ORDER BY id DESC', params).fetchall()
    oib = ''.join(ch for ch in (supplier_oib or '') if ch.isdigit())
    name = ' '.join((supplier or '').casefold().split())
    matched = []
    for row in rows:
        candidate_oib = ''.join(ch for ch in (row['supplier_oib'] or '') if ch.isdigit())
        same_supplier = (oib and candidate_oib and oib == candidate_oib) or (
            not oib and not candidate_oib and name == ' '.join((row['supplier'] or '').casefold().split()))
        if same_supplier:
            matched.append(dict(row))
    return matched


def attach_invoice_to_paid_offer(db, invoice_id, offer_id, actor):
    """Promote the invoice document and fields into a paid offer's existing flow."""
    invoice = db.execute('SELECT * FROM bills WHERE id=?', (invoice_id,)).fetchone()
    offer = db.execute('SELECT * FROM bills WHERE id=?', (offer_id,)).fetchone()
    if not invoice or not offer or offer_id not in {
        row['id'] for row in offer_matches(db, invoice['supplier'], invoice['supplier_oib'], invoice['amount'], invoice['currency'], invoice_id)
    }:
        return False
    old_offer_path = offer['stored_path'] or ''
    if old_offer_path:
        db.execute('''INSERT INTO bill_documents(bill_id,document_type,original_name,stored_path,added_at,note)
            VALUES(?,?,?,?,?,?)''', (offer_id, 'Ponuda / Offer', offer['original_name'], old_offer_path,
                                    local_timestamp(),
                                    f'Original offer {offer["invoice_number"]} retained as a related document'))
    fields = ('supplier','invoice_number','invoice_date','due_date','amount','currency','category','cost_center',
              'description','fingerprint','original_name','stored_path','raw_text','extraction_method','supplier_address',
              'supplier_oib','supplier_vat_id','supplier_iban','vat_amount','payment_model','payment_reference',
              'payment_purpose','payment_description','supplier_profile_id')
    assignments = ','.join(f'{name}=?' for name in fields)
    values = [invoice[name] for name in fields]
    db.execute(f"UPDATE bills SET {assignments},document_type='invoice' WHERE id=?", (*values, offer_id))
    db.execute('DELETE FROM bill_items WHERE bill_id=?', (offer_id,))
    db.execute('UPDATE bill_items SET bill_id=? WHERE bill_id=?', (offer_id, invoice_id))
    db.execute('UPDATE bill_comments SET bill_id=? WHERE bill_id=?', (offer_id, invoice_id))
    db.execute('DELETE FROM audit_log WHERE entity_type="bill" AND entity_id=?', (str(invoice_id),))
    db.execute('DELETE FROM bills WHERE id=?', (invoice_id,))
    db.execute('''INSERT INTO audit_log(occurred_at,user_id,username,action,entity_type,entity_id,details)
                                  VALUES(?,?,?,?,?,?,?)''', (local_timestamp(), actor['id'], actor['username'],
                                  'linked invoice to paid offer', 'bill', str(offer_id),
                                  f'Invoice {invoice["invoice_number"]} replaced offer {offer["invoice_number"]} as the primary document'))
    return True
