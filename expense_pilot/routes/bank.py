import hashlib
import unicodedata
from html import escape
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode
from flask import Blueprint, g, redirect, request
from ..core import (UPLOAD_DIR, audit, auto_match_exact_reference, connect,
                    read_bank_rows, reconcile_page, normalize_bank_date,
                    normalized_reference, refresh_bill_payment_status, page)
from ..bank_services import parse_bank_amount
from ..bank_services import read_statement_period
from ..web import parse_european_number
from ..security import login_required, roles_required
from ..workflow import READY, PAID, ORDER_ISSUE

bp=Blueprint('bank',__name__)

def _return_to_reconcile():
    params={key:request.form.get(key,'') for key in ('statement','tab','view','from','to','bill_id','search','min_amount','max_amount','page') if request.form.get(key,'')}
    query=urlencode(params)
    return redirect('/reconcile'+('?'+query if query else ''))


@bp.get('/reconcile')
@login_required
@roles_required('admin','validator','submitter')
def reconcile():
    view=request.args.get('view') or 'matching'
    read_only=view=='overall'
    return reconcile_page(request.args.get('statement',''),request.args.get('from',''),
                          request.args.get('to',''),request.args.get('tab','orders'),
                          request.args.get('bill_id','0'),read_only=read_only,
                          search=request.args.get('search',''),min_amount=request.args.get('min_amount',''),
                          max_amount=request.args.get('max_amount',''),page_number=request.args.get('page',1),
                          submitter_id=None,
                          can_match=True)


@bp.post('/statement')
@login_required
@roles_required('submitter','validator','admin')
def import_statement():
    item=request.files.get('statement')
    if not item or not item.filename:return page('<h1>No statement selected</h1><p>Choose a CSV, XLS, or XLSX export.</p>'),400
    filename=item.filename.replace('\\','/').rsplit('/',1)[-1]
    if Path(filename).suffix.lower() not in ('.csv','.xls','.xlsx'):
        return page('<h1>Unsupported statement format</h1><p class="sub">Please upload a CSV, XLS, or XLSX statement.</p>'),400
    data=item.read()
    try:
        reader=read_bank_rows(data,filename)
        statement_period=read_statement_period(data,filename)
    except Exception as exc:
        detail=escape(str(exc) or 'Unknown file parsing error.')
        return page('<h1>Could not read statement</h1><p class="sub">'+detail+'</p><p><a href="/reconcile">Return to reconciliation</a></p>'),400
    # Keep only the statement rows and metadata in SQLite; the source upload
    # is read from memory and never persisted in the uploads directory.
    safe=datetime.now().strftime('%Y%m%d_%H%M%S_%f_')+filename
    def normalize(value):
        value=unicodedata.normalize('NFKD',str(value or '').casefold())
        value=''.join(char for char in value if not unicodedata.combining(char))
        return ''.join(char for char in value if char.isalnum())
    def col(row,*names):
        lookup={normalize(key):value for key,value in row.items() if key}
        for name in names:
            if normalize(name) in lookup:
                value=lookup[normalize(name)]
                if isinstance(value,(datetime,date)):return value
                return str(value or '').strip()
        return ''
    inserted=0;new_ids=[];closing_balance=None;balance_date='';currency_seen='EUR'
    account_name=request.form.get('account_name','Main account').strip() or 'Main account'
    with connect() as db:
        # Recover period metadata from older statement files already stored by the app.
        known_files={row['source_file'] for row in db.execute('SELECT DISTINCT source_file FROM bank_statement_periods').fetchall()}
        old_files=[row['source_file'] for row in db.execute("SELECT DISTINCT source_file FROM transactions WHERE source_file<>''").fetchall()]
        for old_name in old_files:
            old_path=UPLOAD_DIR/Path(old_name).name
            if old_name in known_files or not old_path.is_file():continue
            try:old_period=read_statement_period(old_path.read_bytes(),old_path.name)
            except Exception:old_period=None
            if old_period:
                db.execute('INSERT INTO bank_statement_periods(account_ref,currency,period_start,period_end,source_file,created_at) VALUES(?,?,?,?,?,?)',
                    (old_period['account_ref'] or account_name,old_period['currency'],old_period['period_start'],old_period['period_end'],old_name,datetime.now().isoformat(timespec='seconds')))
        if statement_period:
            db.execute('INSERT INTO bank_statement_periods(account_ref,currency,period_start,period_end,source_file,created_at) VALUES(?,?,?,?,?,?)',
                (statement_period['account_ref'] or account_name,statement_period['currency'],statement_period['period_start'],statement_period['period_end'],safe,datetime.now().isoformat(timespec='seconds')))
        for row in reader:
            dt=col(row,'booking date','datum knjiženja','date','transaction date','value date','datum valute')
            description=col(row,'description','opis knjiženja','details','opis')
            counterparty=col(row,'counterparty','payee','name','naziv')
            description=description or counterparty
            amount=col(row,'amount','transaction amount','debit/credit','iznos')
            debit=col(row,'debit','debit amount','duguje','terećenje')
            credit=col(row,'credit','credit amount','potražuje','odobrenje')
            currency=col(row,'currency','ccy','valuta') or 'EUR'
            reference=col(row,'reference','ref','transaction id','poziv na br.odob','poziv na broj primatelja','poziv na broj','creditor reference','payment reference')
            model=col(row,'model','reference model','model odob.','model poziva na broj')
            debtor_ref=col(row,'poziv na br.zad.')
            debtor_model=col(row,'model zad.')
            if not reference and debtor_ref:
                reference=debtor_ref
                model=debtor_model
            if model and reference:reference=model+' '+reference
            try:
                if debit:signed=-abs(parse_bank_amount(debit))
                elif credit:signed=abs(parse_bank_amount(credit))
                else:signed=parse_bank_amount(amount)
            except (ValueError,TypeError):continue
            absolute=abs(signed);direction='incoming' if signed>0 else 'outgoing'
            try:dt=normalize_bank_date(dt)
            except ValueError:continue
            if not dt or not description:continue
            key=hashlib.sha256('|'.join([dt,description.casefold(),counterparty.casefold(),f'{signed:.2f}',currency.upper(),normalized_reference(reference)]).encode('utf-8')).hexdigest()
            if db.execute('SELECT 1 FROM transactions WHERE external_key=?',(key,)).fetchone():continue
            cursor=db.execute('INSERT INTO transactions(txn_date,description,amount,currency,reference,source_file,direction,kind,signed_amount,external_key,counterparty) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(dt,description,absolute,currency,reference,safe,direction,'unclassified',signed,key,counterparty))
            new_ids.append(cursor.lastrowid);inserted+=1
            balance=col(row,'balance','running balance','closing balance','stanje','saldo')
            if balance:
                try:closing_balance=parse_bank_amount(balance);balance_date=dt;currency_seen=currency
                except ValueError:pass
        if closing_balance is not None:
            db.execute('INSERT INTO bank_snapshots(account_name,as_of_date,closing_balance,currency,source_file,created_at) VALUES(?,?,?,?,?,?)',(account_name,balance_date,closing_balance,currency_seen,safe,datetime.now().isoformat(timespec='seconds')))
        period_note=(f"; period {statement_period['period_start']} to {statement_period['period_end']}" if statement_period else '; no period found in A2')
        audit(db,'imported bank statement','statement',safe,f'{inserted} new transactions{period_note}')
    for transaction_id in new_ids:auto_match_exact_reference(transaction_id)
    return redirect('/reconcile?'+urlencode({'statement':safe,'tab':'statement'}))


@bp.post('/match')
@login_required
@roles_required('submitter','validator','admin')
def match_transaction():
    try:transaction_id=int(request.form.get('txn_id','0'))
    except ValueError:transaction_id=0
    selected=[int(value) for value in request.form.getlist('bill_ids') if value.isdigit()]
    with connect() as db:
        tx=db.execute('SELECT * FROM transactions WHERE id=?',(transaction_id,)).fetchone()
        if tx and selected and not db.execute('SELECT 1 FROM payment_allocations WHERE transaction_id=?',(transaction_id,)).fetchone():
            kind='supplier_refund' if tx['direction']=='incoming' else 'bill_payment'
            entries=[];valid=True
            for bill_id in selected:
                bill=db.execute('SELECT * FROM bills WHERE id=?',(bill_id,)).fetchone()
                if not bill or bill['currency']!=tx['currency'] or (tx['direction']=='outgoing' and bill['status'] not in (READY,ORDER_ISSUE)) or (tx['direction']=='incoming' and bill['status'] not in (PAID,ORDER_ISSUE)):
                    valid=False;break
                raw_amount=request.form.get(f'allocation_{bill_id}','')
                if not raw_amount and len(selected)==1:allocated=tx['amount']
                else:
                    try:allocated=parse_european_number(raw_amount)
                    except ValueError:valid=False;break
                if allocated<=0:valid=False;break
                sums=db.execute('SELECT allocation_type,COALESCE(SUM(amount),0) total FROM payment_allocations WHERE bill_id=? GROUP BY allocation_type',(bill_id,)).fetchall()
                totals={row['allocation_type']:row['total'] for row in sums}
                net=totals.get('bill_payment',0)-totals.get('supplier_refund',0)
                available=(bill['amount']-net) if kind=='bill_payment' else net
                if allocated>available+0.009:valid=False;break
                entries.append((bill_id,allocated))
            if valid and abs(sum(amount for _,amount in entries)-tx['amount'])<0.011:
                for bill_id,allocated in entries:
                    db.execute('INSERT INTO payment_allocations(transaction_id,bill_id,amount,allocation_type) VALUES(?,?,?,?)',(transaction_id,bill_id,allocated,kind))
                    refresh_bill_payment_status(db,bill_id)
                legacy_id=entries[0][0] if len(entries)==1 else None
                db.execute('UPDATE transactions SET matched_bill_id=?,kind=? WHERE id=?',(legacy_id,kind,transaction_id))
                audit(db,'matched refund' if kind=='supplier_refund' else 'matched payment','transaction',transaction_id,', '.join(f'bill #{bid}: {amount:.2f}' for bid,amount in entries))
    return _return_to_reconcile()


@bp.post('/other-cost')
@login_required
@roles_required('submitter','validator','admin')
def classify_cost():
    try:transaction_id=int(request.form.get('txn_id','0'))
    except ValueError:transaction_id=0
    with connect() as db:
        transaction=db.execute('SELECT * FROM transactions WHERE id=?',(transaction_id,)).fetchone()
        if transaction:
            db.execute('INSERT INTO other_costs(txn_date,description,amount,currency,source_file,transaction_id) VALUES(?,?,?,?,?,?)',(transaction['txn_date'],transaction['description'],transaction['amount'],transaction['currency'],transaction['source_file'],transaction_id))
            db.execute("UPDATE transactions SET kind='other_cost' WHERE id=?",(transaction_id,))
            audit(db,'classified transaction','transaction',transaction_id,'other cost')
    return _return_to_reconcile()


@bp.post('/other-income')
@login_required
@roles_required('submitter','validator','admin')
def classify_income():
    try:transaction_id=int(request.form.get('txn_id','0'))
    except ValueError:transaction_id=0
    with connect() as db:
        db.execute("UPDATE transactions SET kind='other_income' WHERE id=? AND direction='incoming'",(transaction_id,))
        audit(db,'classified transaction','transaction',transaction_id,'other income')
    return _return_to_reconcile()
