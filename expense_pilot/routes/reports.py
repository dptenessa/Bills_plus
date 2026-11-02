import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Blueprint, abort, make_response, request, send_file, g
from ..budget import budget_data, budget_template, import_plan, plan_identity, workbook_bytes
from ..database import connect
from ..web import render_page
from ..report_views import report_data, report_page
from ..history_import import import_history
from ..security import login_required, roles_required

bp=Blueprint('reports',__name__)


@bp.get('/bills-situation.xlsx')
@login_required
def bills_situation():
    def clean_date(value):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat() if value else ''
        except (TypeError, ValueError):
            return ''
    start_date=clean_date(request.args.get('from',''))
    end_date=clean_date(request.args.get('to',''))
    if start_date and end_date and start_date>end_date:
        start_date,end_date=end_date,start_date
    def event_filter(expression):
        clauses=[];values=[]
        if start_date:
            clauses.append(f"{expression}>=?");values.append(start_date)
        if end_date:
            clauses.append(f"{expression}<=?");values.append(end_date)
        return '('+' AND '.join(clauses)+')',values
    invoice_match,invoice_params=event_filter("NULLIF(substr(b.invoice_date,1,10),'')")
    order_event,order_params=event_filter('substr(h.occurred_at,1,10)')
    order_match="EXISTS (SELECT 1 FROM bill_status_history h WHERE h.bill_id=b.id AND h.status='Order of payment issued' AND "+order_event+")"
    paid_at="NULLIF(substr(b.paid_at,1,10),'')"
    paid_match,paid_params=event_filter(paid_at)
    transaction_event,transaction_params=event_filter('substr(t.txn_date,1,10)')
    where='';params=[]
    if start_date or end_date:
        where=(' WHERE ('+invoice_match+' OR '+order_match+' OR '+paid_match+
               " OR EXISTS (SELECT 1 FROM payment_allocations a JOIN transactions t ON t.id=a.transaction_id WHERE a.bill_id=b.id AND a.allocation_type='bill_payment' AND "+transaction_event+'))')
        params=[*invoice_params,*order_params,*paid_params,*transaction_params]
    # This shared export includes every user's bills and workflow statuses;
    # the date range matches any invoice, payment-order, or bank-payment date.
    with connect() as db:
        bills = db.execute('''SELECT b.*,u.username submitter,
            (SELECT h.occurred_at FROM bill_status_history h WHERE h.bill_id=b.id
             ORDER BY h.occurred_at DESC,h.id DESC LIMIT 1) status_changed_at,
            (SELECT GROUP_CONCAT(DISTINCT substr(h.occurred_at,1,10)) FROM bill_status_history h
             WHERE h.bill_id=b.id AND h.status='Order of payment issued') payment_order_dates,
            (SELECT GROUP_CONCAT(DISTINCT substr(t.txn_date,1,10))
             FROM payment_allocations a JOIN transactions t ON t.id=a.transaction_id
             WHERE a.bill_id=b.id AND a.allocation_type='bill_payment') bank_payment_dates,
            COALESCE((SELECT SUM(CASE WHEN a.allocation_type='bill_payment' THEN a.amount
                WHEN a.allocation_type='supplier_refund' THEN -a.amount ELSE 0 END)
                FROM payment_allocations a WHERE a.bill_id=b.id),0) bank_paid_net
            FROM bills b LEFT JOIN users u ON u.id=b.created_by'''+where+' ORDER BY b.id',params).fetchall()
    columns = ('id', 'supplier', 'invoice_number', 'invoice_date', 'due_date', 'amount',
               'currency', 'category', 'cost_center', 'description', 'document_type',
               'status', 'status_changed_at', 'payment_order_dates', 'paid_at', 'bank_payment_dates', 'bank_paid_net',
               'scheduled_pay_date', 'payment_plan', 'decision_reason', 'decided_at',
               'submitter', 'created_at')
    output = workbook_bytes(columns, ([bill[name] for name in columns] for bill in bills), 'All bills')
    return send_file(output, as_attachment=True, download_name='all_bills_situation.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _budget_filters(source):
    try:
        year = int(source.get('year', ''))
        if not 2000 <= year <= 2099:
            raise ValueError
    except (TypeError, ValueError):
        year = datetime.now(ZoneInfo('Europe/Zagreb')).year
    try:
        month = int(source.get('month', 0))
        if not 0 <= month <= 12:
            raise ValueError
    except (TypeError, ValueError):
        month = 0
    currency = source.get('currency', 'EUR').strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        currency = 'EUR'
    try:
        _, version = plan_identity(year, source.get('version', 'Budget'))
    except ValueError:
        version = 'Budget'
    return year, month, currency, version


@bp.get('/budget')
@login_required
def budget_alignment():
    return render_page('budget.html', title='Budget alignment', **budget_data(*_budget_filters(request.args)))


@bp.get('/budget/bills')
@login_required
def budget_bills():
    data = budget_data(*_budget_filters(request.args))
    category = request.args.get('category', '').strip()
    payments = [row for row in data['payment_details'] if not category or row['category'].casefold() == category.casefold()]
    planned = sum((row['planned'] for row in data['categories']
                   if not category or row['category'].casefold() == category.casefold()), 0)
    return render_page('budget_bills.html', title='Bills behind budget spending', category=category,
                       payments=payments, detail_planned=planned, detail_paid=sum((row['amount'] for row in payments), 0), **data)


@bp.get('/budget/bills/<int:bill_id>')
@login_required
def budget_bill_detail(bill_id):
    # Shared read-only accounting detail. Existing workflow and document
    # permissions remain enforced by their own routes.
    data = budget_data(*_budget_filters(request.args))
    with connect() as db:
        bill = db.execute("SELECT * FROM bills WHERE id=? AND status NOT IN ('Deleted','Void')", (bill_id,)).fetchone()
    if not bill:
        abort(404)
    payments = [row for row in data['payment_details'] if row['bill_id'] == bill_id]
    return render_page('budget_bill.html', title='Budget bill detail', bill=dict(bill), payments=payments,
                       category=request.args.get('category', ''), **data)


@bp.get('/budget-template.xlsx')
@login_required
def download_budget_template():
    try:
        year, version = plan_identity(request.args.get('year'), request.args.get('version'))
    except ValueError as exc:
        return render_page('budget.html', title='Budget alignment', error=str(exc), **budget_data(*_budget_filters(request.args))), 400
    return send_file(budget_template(year, version), as_attachment=True, download_name=f'budget_plan_{year}_{version}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.post('/budget-upload')
@login_required
def upload_budget():
    filters = _budget_filters(request.form)
    item = request.files.get('budget_file')
    try:
        year, version = plan_identity(request.form.get('year'), request.form.get('version'))
        if not item or not item.filename or Path(item.filename).suffix.lower() != '.xlsx':
            raise ValueError('Choose an Excel .xlsx budget plan. Download the template to get started.')
        count = import_plan(item.read(), g.user, year, version)
    except ValueError as exc:
        return render_page('budget.html', title='Budget alignment', error=str(exc), **budget_data(*filters)), 400
    from flask import redirect, url_for
    return redirect(url_for('reports.budget_alignment', year=filters[0], month=filters[1],
                            currency=filters[2], version=version, imported=count), code=303)


@bp.get('/report')
@login_required
@roles_required('submitter','validator','admin')
def weekly_report():
    return report_page(request.args.get('from',''),request.args.get('to',''))


@bp.get('/history-template.xlsx')
@login_required
@roles_required('submitter','validator','admin')
def history_template():
    return send_file(Path(__file__).resolve().parents[2] / 'static' / 'bill_history_template.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name='bill_history_template.xlsx')


@bp.post('/history-upload')
@login_required
@roles_required('submitter','validator','admin')
def history_upload():
    item=request.files.get('history_file')
    if not item or not item.filename:
        return report_page(request.form.get('from',''),request.form.get('to',''),error='Choose an Excel history file first.'),400
    if Path(item.filename).suffix.lower()!='.xlsx':
        return report_page(request.form.get('from',''),request.form.get('to',''),error='History upload accepts Excel .xlsx files. Use the downloaded template.'),400
    try:
        result=import_history(item.read(),g.user)
    except ValueError as exc:
        return report_page(request.form.get('from',''),request.form.get('to',''),error=str(exc)),400
    skipped=', '.join(f'row {row} (existing bill #{bill_id})' for row,bill_id in result['skipped'])
    message=f"Imported {result['inserted']} bill record(s)."
    if skipped:
        message+=f' Skipped duplicates: {skipped}.'
    return report_page(request.form.get('from',''),request.form.get('to',''),message=message)


@bp.get('/report.csv')
@login_required
@roles_required('submitter','validator','admin')
def report_csv():
    data=report_data(request.args.get('from',''),request.args.get('to',''))
    output=StringIO();writer=csv.writer(output)
    writer.writerow(['bill_id','supplier','invoice_number','invoice_date','due_date','description','category','amount','currency','status_at_end','status_changed_at_end'])
    for bill in data['bills']:
        writer.writerow([bill['id'],bill['supplier'],bill['invoice_number'],bill['invoice_date'],bill['due_date'],bill['description'],bill['category'],bill['amount'],bill['currency'],bill['status_at_end'],bill['status_at_end_at']])
    response=make_response(output.getvalue().encode('utf-8-sig'))
    response.headers['Content-Type']='text/csv; charset=utf-8'
    response.headers['Content-Disposition']=f'attachment; filename="bill_status_{data["start"]}_{data["end"]}.csv"'
    return response


