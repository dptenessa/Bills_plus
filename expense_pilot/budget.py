"""Monthly category plans and payment-based actuals, without currency conversion."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from .config import EXPENSE_CONFIG
from .database import connect
from .web import audit


PLAN_HEADERS = ('month', 'category', 'currency', 'planned_amount')
DEFAULT_VERSIONS = ('Budget', 'FCQ1', 'FCQ2', 'FCQ3', 'FCQ4')


def plan_identity(year, version):
    try:
        year = int(year)
        if not 2000 <= year <= 2099:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('Choose an accounting year between 2000 and 2099.') from None
    version = str(version or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9 _-]{0,31}', version):
        raise ValueError('Choose a plan version (Budget, FCQ1, FCQ2, etc.; at most 32 letters, numbers, spaces, hyphens or underscores).')
    return year, 'Budget' if version == 'BUDGET' else version


def workbook_bytes(headers, rows, title):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    sheet.append(list(headers))
    for number, row in enumerate(rows, 2):
        # Supplier names, invoice references, and categories are plain text,
        # even when they start with an Excel formula character.
        for index, value in enumerate(row, 1):
            cell = sheet.cell(number, index, value)
            if isinstance(cell.value, str):
                cell.data_type = 's'
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='343B4D')
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        width = max(len(str(cell.value or '')) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(45, max(15, width + 2))
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def budget_template(year, version='Budget'):
    year, version = plan_identity(year, version)
    output = workbook_bytes(PLAN_HEADERS,
        ((f'{year}-{month:02}', category, 'EUR', 0)
         for category in EXPENSE_CONFIG['categories'] for month in range(1, 13)), 'Budget plan')
    workbook = load_workbook(output)
    info = workbook.create_sheet('Instructions')
    for row in (
        ('Accounting year', year), ('Version', version),
        ('Period', f'January–December {year} (calendar accounting year)'),
        ('Format', 'Keep the Budget plan sheet and its four column headings.'),
        ('Month', f'Use {year}-01 through {year}-12; all rows must match the selected accounting year.'),
        ('Amounts', 'Numeric values, non-negative, at most two decimals. No formulas.'),
        ('Upload', 'Choose this year and version before uploading. Other versions are preserved.'),
        ('Updates', 'Only included entries are updated. Omitted entries stay unchanged; use zero to clear an amount.'),
    ):
        info.append(row)
    info.column_dimensions['A'].width = 22
    info.column_dimensions['B'].width = 110
    result = BytesIO()
    workbook.save(result)
    result.seek(0)
    return result


def import_plan(data, user, year, version):
    year, version = plan_identity(year, version)
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:
        raise ValueError('Choose a readable Excel .xlsx budget file.') from exc
    entries = []
    seen = set()
    try:
        if 'Budget plan' not in workbook.sheetnames:
            raise ValueError('The workbook must contain a Budget plan sheet. Download the template first.')
        if 'Instructions' in workbook.sheetnames:
            metadata = dict(workbook['Instructions'].iter_rows(min_row=1, max_row=2, max_col=2, values_only=True))
            if plan_identity(metadata.get('Accounting year'), metadata.get('Version')) != (year, version):
                raise ValueError('The template year/version does not match your upload selection. Download the matching template.')
        rows = workbook['Budget plan'].iter_rows(values_only=True)
        headers = [str(value or '').strip().lower() for value in next(rows, ())]
        if any(headers.count(name) != 1 for name in PLAN_HEADERS):
            raise ValueError('Required columns: ' + ', '.join(PLAN_HEADERS) + '.')
        positions = {name: headers.index(name) for name in PLAN_HEADERS}
        with connect() as db:
            known = list(EXPENSE_CONFIG['categories']) + [r[0] for r in db.execute(
                "SELECT category FROM bills UNION SELECT category FROM budget_plan")]
        categories = {name.casefold(): name for name in known if name}
        for number, source in enumerate(rows, 2):
            if number > 10001:
                raise ValueError('The plan may contain at most 10,000 rows.')
            row = {name: source[index] if index < len(source) else None for name, index in positions.items()}
            if all(value in (None, '') for value in row.values()):
                continue
            month_value = row['month']
            month_text = month_value.strftime('%Y-%m') if isinstance(month_value, (date, datetime)) else str(month_value or '').strip()
            if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', month_text):
                raise ValueError(f'Row {number}: month must use YYYY-MM (2000–2099).')
            if int(month_text[:4]) != year:
                raise ValueError(f'Row {number}: month must belong to accounting year {year} (January–December).')
            category = str(row['category'] or '').strip()
            if not category or len(category) > 150 or category.startswith('='):
                raise ValueError(f'Row {number}: enter a category of 1–150 characters.')
            category = categories.setdefault(category.casefold(), category)
            currency = str(row['currency'] or '').strip().upper()
            if not re.fullmatch(r'[A-Z]{3}', currency):
                raise ValueError(f'Row {number}: currency must be a three-letter code, for example EUR.')
            raw_amount = row['planned_amount']
            try:
                text = str(raw_amount).strip().replace(' ', '')
                if ',' in text:
                    text = text.replace('.', '').replace(',', '.')
                amount = Decimal(text)
                if not amount.is_finite() or amount < 0 or amount > Decimal('999999999999.99'):
                    raise InvalidOperation
                if amount != amount.quantize(Decimal('.01')):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise ValueError(f'Row {number}: planned_amount must be a non-negative number with at most two decimals. Formulas are not accepted.') from None
            key = (int(month_text[:4]), int(month_text[5:]), category, currency)
            if key in seen:
                raise ValueError(f'Row {number}: duplicate month, category, and currency in the file.')
            seen.add(key)
            entries.append((*key, float(amount)))
    finally:
        workbook.close()
    if not entries:
        raise ValueError('The file contains no budget rows.')
    # Validate the complete workbook before changing any saved plan values.
    with connect() as db:
        now = datetime.now().isoformat(timespec='seconds')
        db.executemany('''INSERT INTO budget_plan(year,month,category,currency,amount,version,updated_at,updated_by)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(year,version,month,category,currency) DO UPDATE SET
            amount=excluded.amount,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
            ((*entry, version, now, user['id']) for entry in entries))
        audit(db, 'uploaded budget plan', 'budget', year, details=f'{version}: {len(entries)} monthly category values')
    return len(entries)


def budget_data(year, month=0, currency='EUR', version='Budget'):
    year, version = plan_identity(year, version)
    with connect() as db:
        plans = db.execute('SELECT * FROM budget_plan WHERE year=?', (year,)).fetchall()
        bills = db.execute('''SELECT b.*, (SELECT h.occurred_at FROM bill_status_history h
            WHERE h.bill_id=b.id AND h.status IN ('Paid at bank','Paid')
            ORDER BY h.occurred_at DESC,h.id DESC LIMIT 1) payment_history_date
            FROM bills b WHERE b.status NOT IN ('Deleted','Void')''').fetchall()
        allocations = db.execute('''SELECT a.bill_id,a.amount,a.allocation_type,t.txn_date,t.currency,t.id transaction_id
            FROM payment_allocations a JOIN transactions t ON t.id=a.transaction_id
            WHERE a.allocation_type IN ('bill_payment','supplier_refund')''').fetchall()
    bill_map = {b['id']: b for b in bills}
    allocated = {a['bill_id'] for a in allocations}
    payments = []
    undated = 0
    def payment_entry(bill, paid_date, currency, amount, source, transaction_id=None):
        return dict(bill_id=bill['id'], supplier=bill['supplier'], invoice_number=bill['invoice_number'],
                    invoice_date=bill['invoice_date'], bill_amount=bill['amount'], bill_currency=bill['currency'],
                    description=bill['description'], status=bill['status'], created_by=bill['created_by'],
                    paid_date=paid_date, category=bill['category'] or 'Uncategorized', currency=currency,
                    amount=amount, source=source, transaction_id=transaction_id)
    for allocation in allocations:
        bill = bill_map.get(allocation['bill_id'])
        if bill:
            sign = -1 if allocation['allocation_type'] == 'supplier_refund' else 1
            payments.append(payment_entry(bill, allocation['txn_date'], allocation['currency'],
                Decimal(str(allocation['amount'])) * sign,
                'Bank refund' if sign < 0 else 'Bank payment', allocation['transaction_id']))
    for bill in bills:
        if bill['id'] in allocated or bill['status'] not in ('Paid at bank', 'Paid'):
            continue
        paid_date = bill['paid_at'] or bill['payment_history_date'] or ''
        try:
            date.fromisoformat(paid_date[:10])
        except ValueError:
            undated += 1
            continue
        payments.append(payment_entry(bill, paid_date, bill['currency'], Decimal(str(bill['amount'])),
                                      'Marked paid'))
    currencies = sorted({'EUR', currency} | {p['currency'] for p in plans} | {p['currency'] for p in payments})
    # Match category spelling consistently without changing source bills.
    labels = {name.casefold(): name for name in EXPENSE_CONFIG['categories']}
    buckets = defaultdict(lambda: {'planned': Decimal(0), 'paid': Decimal(0), 'has_plan': False})
    for plan in plans:
        if plan['currency'] != currency or plan['version'] != version:
            continue
        category = labels.setdefault(plan['category'].casefold(), plan['category'])
        bucket = buckets[(plan['month'], category)]
        bucket['planned'] += Decimal(str(plan['amount']))
        bucket['has_plan'] = True
    payment_details = []
    for payment in payments:
        paid_date, category, paid_currency, amount = (payment[key] for key in ('paid_date', 'category', 'currency', 'amount'))
        if paid_currency != currency or str(paid_date)[:4] != str(year):
            continue
        try:
            payment_month = date.fromisoformat(paid_date[:10]).month
        except ValueError:
            undated += 1
            continue
        category = labels.setdefault(category.casefold(), category)
        buckets[(payment_month, category)]['paid'] += amount
        if not month or payment_month == month:
            payment_details.append(dict(payment, category=category))
    monthly = []
    totals = defaultdict(lambda: {'planned': Decimal(0), 'paid': Decimal(0), 'has_plan': False})
    for (number, category), values in sorted(buckets.items()):
        if month and number != month:
            continue
        monthly.append(dict(month=number, category=category, remaining=values['planned']-values['paid'], **values))
        total = totals[category]
        total['planned'] += values['planned']
        total['paid'] += values['paid']
        total['has_plan'] |= values['has_plan']
    categories = [dict(category=category, remaining=values['planned']-values['paid'],
                       percent=values['paid']/values['planned']*100 if values['planned'] else None, **values)
                  for category, values in sorted(totals.items())]
    planned = sum((r['planned'] for r in categories), Decimal(0))
    paid = sum((r['paid'] for r in categories), Decimal(0))
    versions = list(dict.fromkeys((*DEFAULT_VERSIONS, version, *(p['version'] for p in plans))))
    version_comparisons = []
    for name in versions:
        matching = [p for p in plans if p['version'] == name and p['currency'] == currency and (not month or p['month'] == month)]
        total = sum((Decimal(str(p['amount'])) for p in matching), Decimal(0))
        version_comparisons.append(dict(version=name, planned=total, paid=paid, remaining=total-paid, has_plan=bool(matching)))
    return dict(year=year, month=month, currency=currency, currencies=currencies,
                version=version, versions=versions, version_comparisons=version_comparisons,
                payment_details=sorted(payment_details, key=lambda row: (row['paid_date'], row['bill_id'])),
                categories=categories, monthly=monthly, planned=planned, paid=paid,
                remaining=planned-paid, undated=undated)
