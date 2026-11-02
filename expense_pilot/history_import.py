"""Excel import for bills and their previously recorded workflow history."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook

from .bank_services import parse_bank_amount
from .database import connect
from .identifiers import find_duplicate_bill, normalize_invoice_number
from .supplier_services import get_or_create_supplier
from .web import audit


HEADERS = (
    'supplier', 'invoice_number', 'invoice_date', 'due_date', 'amount', 'currency',
    'document_type', 'category', 'description', 'status', 'status_at',
    'pending_for_approval_at', 'ready_for_payment_at',
    'order_of_payment_issues_at', 'paid_at_bank_at', 'rejected_at', 'stored_photo',
)
OPTIONAL_HEADERS = {'stored_photo'}
STATUS_DATES = {
    'Pending for approval': 'pending_for_approval_at',
    'Ready for payment': 'ready_for_payment_at',
    'Order of payment issued': 'order_of_payment_issues_at',
    'Paid at bank': 'paid_at_bank_at',
    'Rejected': 'rejected_at',
}
LEGACY_STATUS_ALIASES = {'Order of payment issues': 'Order of payment issued'}
ALLOWED_STATUSES = set(STATUS_DATES) | set(LEGACY_STATUS_ALIASES) | {'Solved'}
DOCUMENT_TYPES = {
    'invoice': 'invoice', 'minimax_invoice': 'minimax_invoice',
    'credit_card_charge': 'credit_card_charge', 'offer': 'offer', 'refund': 'refund',
    'invoice (invoice)': 'invoice', 'minimax invoice': 'minimax_invoice',
    'credit card charge': 'credit_card_charge', 'ponuda / offer': 'offer',
    'refund (refund)': 'refund',
}


def _date_value(value, field, row_number):
    if value in (None, ''):
        return ''
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    value = str(value).strip()
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f'Row {row_number}: {field} must use YYYY-MM-DD.') from exc


def _timestamp(value, field, row_number):
    if value in (None, ''):
        return ''
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat(timespec='seconds')
    value = str(value).strip()
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time()).isoformat(timespec='seconds')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.isoformat(timespec='seconds')
    except ValueError as exc:
        raise ValueError(f'Row {row_number}: {field} must be YYYY-MM-DD or ISO date/time.') from exc


def _read_xlsx(data):
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError('The file is not a readable Excel workbook. Use the downloaded .xlsx template.') from exc
    if 'Bill history' not in workbook.sheetnames:
        raise ValueError('The workbook must contain the “Bill history” sheet from the downloaded template.')
    sheet = workbook['Bill history']
    iterator = sheet.iter_rows(values_only=True)
    headers = [str(item or '').strip().lower() for item in next(iterator, ())]
    missing = [name for name in HEADERS if name not in headers and name not in OPTIONAL_HEADERS]
    if missing:
        raise ValueError('Template columns are missing: ' + ', '.join(missing))
    positions = {name: headers.index(name) for name in HEADERS if name in headers}
    rows = []
    for row_number, source in enumerate(iterator, start=2):
        row = {name: source[index] if index < len(source) else None for name, index in positions.items()}
        row.setdefault('stored_photo', None)
        if not any(value not in (None, '') for value in row.values()):
            continue
        rows.append((row_number, row))
    workbook.close()
    if not rows:
        raise ValueError('The workbook has headers but no bill rows.')
    return rows


def import_history(data, actor):
    """Validate first, then import rows atomically. Existing bills are skipped."""
    parsed = []
    for row_number, row in _read_xlsx(data):
        supplier = str(row.get('supplier') or '').strip()
        invoice_number = str(row.get('invoice_number') or '').strip()
        if not supplier or not invoice_number:
            raise ValueError(f'Row {row_number}: supplier and invoice_number are required.')
        invoice_date = _date_value(row.get('invoice_date'), 'invoice_date', row_number)
        due_date = _date_value(row.get('due_date'), 'due_date', row_number)
        try:
            amount = parse_bank_amount(row.get('amount'))
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Row {row_number}: amount is not a valid number.') from exc
        if amount <= 0:
            raise ValueError(f'Row {row_number}: amount must be greater than zero.')
        currency = str(row.get('currency') or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{3}', currency):
            raise ValueError(f'Row {row_number}: currency must be a three-letter code such as EUR.')
        status = str(row.get('status') or '').strip()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f'Row {row_number}: status must be one of: {", ".join(sorted(ALLOWED_STATUSES))}.')
        status = LEGACY_STATUS_ALIASES.get(status,status)
        status_at = _timestamp(row.get('status_at'), 'status_at', row_number)
        if not status_at:
            raise ValueError(f'Row {row_number}: status_at is required.')
        doc_input = str(row.get('document_type') or '').strip().lower()
        document_type = DOCUMENT_TYPES.get(doc_input, 'invoice' if not doc_input else '')
        if not document_type:
            raise ValueError(f'Row {row_number}: document_type must be invoice, minimax_invoice, credit_card_charge, offer, or refund.')
        stored_photo = str(row.get('stored_photo') or '').strip()
        if len(stored_photo) > 2000:
            raise ValueError(f'Row {row_number}: stored_photo must be 2,000 characters or fewer.')
        history = []
        for history_status, column in STATUS_DATES.items():
            if history_status == status:
                continue
            occurred_at = _timestamp(row.get(column), column, row_number)
            if occurred_at:
                if occurred_at > status_at:
                    raise ValueError(f'Row {row_number}: {column} cannot be later than status_at.')
                history.append((history_status, occurred_at))
        if status == 'Solved':
            history.append(('Solved', status_at))
        else:
            history.append((status, status_at))
        history.sort(key=lambda item: item[1])
        parsed.append({
            'row_number': row_number, 'supplier': supplier, 'invoice_number': invoice_number,
            'invoice_date': invoice_date, 'due_date': due_date, 'amount': amount,
            'currency': currency, 'document_type': document_type,
            'category': str(row.get('category') or '').strip(),
            'description': str(row.get('description') or '').strip(), 'status': status, 'status_at': status_at,
            'stored_photo': stored_photo, 'history': history,
        })

    inserted = 0
    skipped = []
    with connect() as db:
        for item in parsed:
            duplicate = find_duplicate_bill(db, item['invoice_number'], item['supplier'])
            if duplicate:
                skipped.append((item['row_number'], duplicate['id']))
                continue
            supplier_id = get_or_create_supplier(db, item['supplier'])
            created_at = min(event[1] for event in item['history'])
            fingerprint = hashlib.sha256('|'.join((
                re.sub(r'\W+', '', item['supplier'].casefold()),
                normalize_invoice_number(item['invoice_number']),
                f"{item['amount']:.2f}", item['currency'],
            )).encode('utf-8')).hexdigest()
            cursor = db.execute('''INSERT INTO bills(
                supplier,invoice_number,invoice_date,due_date,amount,currency,category,description,
                fingerprint,original_name,status,created_at,extraction_method,supplier_profile_id,
                document_type,workflow_route,created_by,paid_at,decided_at,legacy_photo_path,is_legacy
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                item['supplier'], item['invoice_number'], item['invoice_date'], item['due_date'],
                item['amount'], item['currency'], item['category'], item['description'], fingerprint,
                'Historical record (Excel import)', item['status'], created_at, 'Historical Excel import',
                supplier_id, item['document_type'], 'history_import', actor['id'],
                item['status_at'] if item['status'] == 'Paid at bank' else '',
                item['status_at'] if item['status'] in ('Rejected', 'Solved') else '',
                item['stored_photo'], 1,
            ))
            bill_id = cursor.lastrowid
            for history_status, occurred_at in item['history']:
                db.execute('''INSERT INTO bill_status_history(bill_id,status,occurred_at,actor_id,note)
                              VALUES(?,?,?,NULL,?)''',
                           (bill_id, history_status, occurred_at, 'Imported historical workflow status'))
            inserted += 1
        audit(db, 'imported bill history', 'bill_history', '',
              f'{inserted} imported; {len(skipped)} duplicate rows skipped')
    return {'inserted': inserted, 'skipped': skipped}
