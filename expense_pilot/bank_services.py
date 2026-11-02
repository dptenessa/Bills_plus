"""Bank Services for the expense pilot."""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from .database import connect
from .web import audit
from .identifiers import normalized_reference
from .workflow import READY, ORDER_ISSUE, PAID, set_bill_status, record_initial_status

def parse_bank_amount(value):
    value=str(value or '').strip().replace('€','').replace('EUR','').replace(' ','')
    negative=value.startswith('(') and value.endswith(')')
    value=value.strip('()')
    value=re.sub(r'[^0-9,.-]','',value)
    if ',' in value and '.' in value:
        dec=',' if value.rfind(',')>value.rfind('.') else '.'; group='.' if dec==',' else ','
        value=value.replace(group,'').replace(dec,'.')
    elif ',' in value:
        tail=value.rsplit(',',1)[1]; value=value.replace(',','.') if len(tail) in (1,2) else value.replace(',','')
    elif '.' in value and len(value.rsplit('.',1)[1])==3:
        value=value.replace('.','')
    amount=float(value)
    return -amount if negative else amount


def read_bank_rows(data,filename):
    suffix=Path(filename.replace('\\','/')).suffix.lower()
    if suffix in ('.xls','.xlsx'):
        if suffix=='.xls':
            try:
                import xlrd
            except ImportError as exc:
                raise ValueError('Legacy .xls support needs xlrd. Install the dependencies from requirements.txt, then restart the app.') from exc
            book=xlrd.open_workbook(file_contents=data,on_demand=True)
            sheet=book.sheet_by_index(0)
            rows=[]
            for r in range(sheet.nrows):
                values=[]
                for c in range(sheet.ncols):
                    cell=sheet.cell(r,c)
                    if cell.ctype==xlrd.XL_CELL_DATE:
                        values.append(xlrd.xldate_as_datetime(cell.value,book.datemode))
                    else:values.append(cell.value)
                rows.append(values)
        else:
            import openpyxl
            book=openpyxl.load_workbook(io.BytesIO(data),read_only=True,data_only=True)
            sheet=book.active;rows=[list(row) for row in sheet.iter_rows(values_only=True)]
        def key(value):
            value=unicodedata.normalize('NFKD',str(value or '').casefold())
            value=''.join(char for char in value if not unicodedata.combining(char))
            return re.sub(r'[^a-z0-9]+','',value)
        date_headers={'datumknjizenja','datumvalute','date','bookingdate','transactiondate','valuedate'}
        amount_headers={'iznos','amount','transactionamount','debitcredit'}
        header_index=None
        # Croatian bank exports commonly place report metadata in rows 1–2 and
        # the actual column headings on row 3. Prefer that row, then search a
        # short prefix for other exports with a similar preamble.
        candidate_indices=([2] if len(rows)>2 else [])+[i for i in range(min(40,len(rows))) if i!=2]
        for index in candidate_indices:
            row=rows[index]
            labels={key(value) for value in row if value not in (None,'')}
            has_date=any(label in date_headers or label.startswith(('datumknjizenja','datumvalute')) for label in labels)
            has_amount=any(label in amount_headers or label.startswith(('iznos','amount')) for label in labels)
            if has_date and has_amount:
                header_index=index;break
        if header_index is None:raise ValueError('Could not find a statement header row with date and amount columns.')
        headers=[str(value or '').strip().casefold() for value in rows[header_index]]
        result=[]
        for row in rows[header_index+1:]:
            record={headers[i]:value for i,value in enumerate(row[:len(headers)]) if i<len(headers) and headers[i]}
            if any(value not in (None,'') for value in record.values()):result.append(record)
        if not result:
            raise ValueError('The statement header was found, but there are no transaction rows below it.')
        return result
    decoded=data.decode('utf-8-sig','replace')
    try:dialect=csv.Sniffer().sniff(decoded[:4096],delimiters=',;\t')
    except csv.Error:dialect=csv.excel
    return list(csv.DictReader(decoded.splitlines(),dialect=dialect))


def read_statement_period(data,filename):
    """Read the Croatian bank export period in cell A2 when present."""
    suffix=Path(filename.replace('\\','/')).suffix.lower()
    if suffix not in ('.xls','.xlsx'):
        return None
    if suffix=='.xls':
        import xlrd
        book=xlrd.open_workbook(file_contents=data,on_demand=True)
        sheet=book.sheet_by_index(0)
        value=sheet.cell_value(1,0) if sheet.nrows>1 and sheet.ncols else ''
    else:
        import openpyxl
        book=openpyxl.load_workbook(io.BytesIO(data),read_only=True,data_only=True)
        value=book.active['A2'].value or ''
        book.close()
    text=str(value or '')
    match=re.search(r'(?i)period\s+od\s+(\d{1,2}\.\d{1,2}\.\d{4})\s+do\s+(\d{1,2}\.\d{1,2}\.\d{4})',text)
    if not match:return None
    start_text,end_text=match.groups()
    account=re.search(r'(?i)\b(HR\d{18,})\b',text)
    currency_match=re.search(r'(?i)\b(EUR|USD|GBP|CHF)\b',text)
    account_ref=account.group(1).upper() if account else ''
    currency=currency_match.group(1).upper() if currency_match else 'EUR'
    start=datetime.strptime(start_text,'%d.%m.%Y').date().isoformat()
    end=datetime.strptime(end_text,'%d.%m.%Y').date().isoformat()
    if start>end:raise ValueError('Statement period start date is after its end date.')
    return {'account_ref':account_ref,'currency':currency,'period_start':start,'period_end':end,'raw':text}


def normalize_bank_date(value):
    if isinstance(value,(datetime,date)):return value.date().isoformat() if isinstance(value,datetime) else value.isoformat()
    value=str(value or '').strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}',value):return value[:10]
    found=re.match(r'\d{1,2}[./-]\d{1,2}[./-]\d{2,4}',value)
    if not found:raise ValueError('unrecognized transaction date')
    date_text=found.group(0).replace('.','/').replace('-','/')
    return datetime.strptime(date_text,'%d/%m/%Y' if len(date_text.split('/')[-1])==4 else '%d/%m/%y').date().isoformat()


def counterparty_similarity(left,right):
    """Simple, explainable token overlap used only to rank human review candidates."""
    def tokens(value):
        value=str(value or '').casefold()
        value=value.translate(str.maketrans({'č':'c','ć':'c','ž':'z','š':'s','đ':'d'}))
        return {token for token in re.findall(r'[a-z0-9]+',value) if len(token)>2}
    a,b=tokens(left),tokens(right)
    return len(a&b)/len(a|b) if a and b else 0.0



def refresh_bill_payment_status(db,bill_id):
    bill=db.execute('SELECT amount,status FROM bills WHERE id=?',(bill_id,)).fetchone()
    if not bill or bill['status'] in ('Rejected','Deleted','Void'):return
    rows=db.execute('SELECT allocation_type,COALESCE(SUM(amount),0) total FROM payment_allocations WHERE bill_id=? GROUP BY allocation_type',(bill_id,)).fetchall()
    totals={r['allocation_type']:r['total'] for r in rows}
    paid=totals.get('bill_payment',0); refunds=totals.get('supplier_refund',0); net=paid-refunds
    if paid and net<=0:status=PAID
    elif net>=bill['amount'] and bill['amount']>0:status=PAID
    elif net>0:status=ORDER_ISSUE
    elif bill['status'] in (PAID,ORDER_ISSUE):status=READY
    else:return
    if status==bill['status']==PAID:
        record_initial_status(db,bill_id,PAID,None,'Confirmed from bank reconciliation')
    else:
        set_bill_status(db,bill_id,status,None,'Confirmed from bank reconciliation' if status==PAID else 'Payment allocation updated')


def auto_match_exact_reference(transaction_id):
    with connect() as db:
        tx=db.execute('SELECT * FROM transactions WHERE id=?',(transaction_id,)).fetchone()
        if not tx or not tx['reference']:return False
        ref=normalized_reference(tx['reference'])
        eligible=(READY,ORDER_ISSUE) if tx['direction']=='outgoing' else (PAID,)
        placeholders=','.join('?' for _ in eligible)
        candidates=db.execute(f'SELECT * FROM bills WHERE currency=? AND ABS(amount-?)<0.01 AND status IN ({placeholders})',(tx['currency'],tx['amount'],*eligible)).fetchall()
        candidates=[b for b in candidates if ref in {normalized_reference(b['payment_reference']),normalized_reference(b['payment_model']+b['payment_reference']),normalized_reference(b['payment_model']+'/'+b['payment_reference'])}]
        if len(candidates)!=1:return False
        bill=candidates[0];kind='supplier_refund' if tx['direction']=='incoming' else 'bill_payment'
        db.execute('UPDATE transactions SET matched_bill_id=?,kind=? WHERE id=?',(bill['id'],kind,transaction_id))
        db.execute('INSERT INTO payment_allocations(transaction_id,bill_id,amount,allocation_type) VALUES(?,?,?,?)',(transaction_id,bill['id'],tx['amount'],kind))
        refresh_bill_payment_status(db,bill['id'])
        audit(db,'auto-matched payment' if kind=='bill_payment' else 'auto-matched refund','bill',bill['id'],f'transaction #{transaction_id} · {tx["amount"]:.2f} {tx["currency"]}')
        return True

