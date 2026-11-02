"""Date-range workflow and cash forecast report."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .database import connect
from .web import render_page
from .workflow import PENDING, READY, ORDER_ISSUE, PAID, REJECTED


def report_dates(start='', end=''):
    today=datetime.now(ZoneInfo('Europe/Zagreb')).date()
    try:
        end_date=datetime.strptime(end,'%Y-%m-%d').date() if end else today
    except ValueError:
        end_date=today
    try:
        start_date=datetime.strptime(start,'%Y-%m-%d').date() if start else end_date.replace(day=1)
    except ValueError:
        start_date=end_date.replace(day=1)
    if start_date>end_date:
        start_date,end_date=end_date,start_date
    return start_date.isoformat(),end_date.isoformat()


def report_data(start='', end=''):
    start,end=report_dates(start,end)
    with connect() as db:
        rows=db.execute('''SELECT b.*,
            COALESCE((SELECT h.status FROM bill_status_history h
                WHERE h.bill_id=b.id AND date(h.occurred_at)<=?
                ORDER BY h.occurred_at DESC,h.id DESC LIMIT 1),b.status) AS status_at_end,
            COALESCE((SELECT h.occurred_at FROM bill_status_history h
                WHERE h.bill_id=b.id AND date(h.occurred_at)<=?
                ORDER BY h.occurred_at DESC,h.id DESC LIMIT 1),b.created_at) AS status_at_end_at
            FROM bills b
            WHERE date(b.created_at)<=? AND b.status NOT IN ('Deleted','Void')
            ORDER BY COALESCE(NULLIF(b.invoice_date,''),substr(b.created_at,1,10)),b.id''',
            (end,end,end)).fetchall()
        snapshot_rows=db.execute('''SELECT * FROM bank_snapshots WHERE as_of_date<=?
            ORDER BY as_of_date DESC,id DESC''',(end,)).fetchall()
        allocation_rows=db.execute('''SELECT a.bill_id,
            SUM(CASE WHEN a.allocation_type='bill_payment' THEN a.amount ELSE 0 END) AS paid,
            SUM(CASE WHEN a.allocation_type='supplier_refund' THEN a.amount ELSE 0 END) AS refunded
            FROM payment_allocations a JOIN transactions t ON t.id=a.transaction_id
            WHERE t.txn_date<=? GROUP BY a.bill_id''',(end,)).fetchall()

    all_bills=[dict(row) for row in rows]
    allocations={row['bill_id']:max(0.0,float(row['paid'] or 0)-float(row['refunded'] or 0)) for row in allocation_rows}
    for bill in all_bills:
        status=bill['status_at_end']
        bill['outstanding']=max(0.0,float(bill['amount'] or 0)-allocations.get(bill['id'],0)) if status in (READY,ORDER_ISSUE) else 0.0
        bill['overdue']=bool(status==PENDING and bill['due_date'] and bill['due_date']<end)
    bills=[b for b in all_bills if start <= (b['invoice_date'] or (b['created_at'] or '')[:10]) <= end]

    counts={status:sum(b['status_at_end']==status for b in bills)
            for status in (PENDING,READY,ORDER_ISSUE,PAID,REJECTED)}
    # "Solved" is a rejected bill whose follow-up has been resolved. Keep it
    # in the rejected workflow total so it does not disappear from the report.
    rejected_count=counts[REJECTED]+sum(b['status_at_end']=='Solved' for b in bills)
    pending_overdue=sum(b['status_at_end']==PENDING and b['overdue'] for b in bills)
    pending_not_due=sum(b['status_at_end']==PENDING and not b['overdue'] for b in bills)
    currencies=sorted({(b['currency'] or 'EUR') for b in bills}|{r['currency'] for r in snapshot_rows})
    forecasts=[]
    latest_by_currency={}
    for snapshot in snapshot_rows:
        latest_by_currency.setdefault(snapshot['currency'],dict(snapshot))
    for currency in currencies:
        snapshot=latest_by_currency.get(currency)
        obligations=sum(b['outstanding'] for b in all_bills if (b['currency'] or 'EUR')==currency)
        forecasts.append({'currency':currency,'balance':snapshot['closing_balance'] if snapshot else None,
                          'as_of_date':snapshot['as_of_date'] if snapshot else '',
                          'obligations':obligations,
                          'forecast':snapshot['closing_balance']-obligations if snapshot else None})
    summary={'closed':counts[PAID]+rejected_count, 'pending_not_due':pending_not_due,
             'pending_overdue':pending_overdue,'ready':counts[READY],
             'order_issue':counts[ORDER_ISSUE],'paid':counts[PAID],
             'rejected':rejected_count}
    return {'start':start,'end':end,'bills':bills,'summary':summary,'forecasts':forecasts}


def report_page(start='',end='',message='',error=''):
    data=report_data(start,end)
    return render_page('report.html',title='Bill and cash report',message=message,error=error,**data)
