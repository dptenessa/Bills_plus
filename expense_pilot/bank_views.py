"""Bank Views for the expense pilot."""
from __future__ import annotations

from .database import connect
from .web import render_page, european_amount
from .identifiers import normalized_reference
from .bank_services import counterparty_similarity,parse_bank_amount
from .workflow import READY, ORDER_ISSUE, PAID
from datetime import date,timedelta
from urllib.parse import urlencode


def suggested_matches(candidates):
    """Show at most two supported matches, rather than every eligible record."""
    references=[item for item in candidates if item['reference_match']]
    supported=references or [item for item in candidates
        if item.get('invoice_match') or (item['amount_difference']<0.01 and item['name_score']>=0.6)]
    return sorted(supported,key=lambda item:(not item['reference_match'],not item.get('invoice_match',False),
        item['amount_difference']>=0.01,-item['name_score'],
        item['day_gap'] if item['day_gap'] is not None else 9999))[:2]


def bank_forecast(db):
    """Current known balance and remaining pipeline, independent of statement filters."""
    latest={}
    for row in db.execute('SELECT * FROM bank_snapshots ORDER BY as_of_date DESC,id DESC'):
        latest.setdefault((row['account_name'],row['currency']),dict(row))
    balances={}
    for row in latest.values():
        bucket=balances.setdefault(row['currency'],{'currency':row['currency'],'balance':None,'pipeline':0.0,'snapshots':[]})
        bucket['balance']=(bucket['balance'] or 0)+row['closing_balance']
        bucket['snapshots'].append(row)
    for row in db.execute('''SELECT b.id,b.amount,b.currency,b.document_type,
        COALESCE(SUM(CASE WHEN a.allocation_type='bill_payment' THEN a.amount
                         WHEN a.allocation_type='supplier_refund' THEN -a.amount ELSE 0 END),0) paid
        FROM bills b LEFT JOIN payment_allocations a ON a.bill_id=b.id
        WHERE b.status IN ('Needs review','Pending for approval','Ready for payment','Order of payment issued')
        GROUP BY b.id'''):
        bucket=balances.setdefault(row['currency'],{'currency':row['currency'],'balance':None,'pipeline':0.0,'snapshots':[]})
        remaining=max(0.0,float(row['amount'])-float(row['paid']))
        bucket['pipeline']+=remaining
    for bucket in balances.values():
        bucket['forecast']=bucket['balance']-bucket['pipeline'] if bucket['balance'] is not None else None
    return sorted(balances.values(),key=lambda item:item['currency'])


def reconcile_page(source_file='', start_date='', end_date='', tab='orders', bill_id=0,read_only=False,search='',min_amount='',max_amount='',page_number=1,submitter_id=None,can_match=False):
    def valid_date(value):
        try:return date.fromisoformat(value).isoformat() if value else ''
        except (TypeError,ValueError):return ''
    start_date=valid_date(start_date);end_date=valid_date(end_date)
    search=(search or '').strip()
    def amount_bound(value):
        if not str(value or '').strip():return None
        try:
            parsed=parse_bank_amount(value)
            return parsed if parsed>=0 else None
        except (TypeError,ValueError):return None
    min_value=amount_bound(min_amount);max_value=amount_bound(max_amount)
    if start_date and end_date and start_date>end_date:start_date,end_date=end_date,start_date
    try:page_number=max(1,int(page_number or 1))
    except (TypeError,ValueError):page_number=1
    page_size=100
    tab=tab if tab in ('statement','orders') else 'orders'
    try:focus_bill_id=int(bill_id or 0)
    except (TypeError,ValueError):focus_bill_id=0
    with connect() as db:
        bank_situation=bank_forecast(db)
        period_rows=db.execute('SELECT account_ref,currency,period_start,period_end,source_file,created_at FROM bank_statement_periods ORDER BY account_ref,currency,period_start,period_end').fetchall()
        transaction_sources=db.execute("SELECT source_file,MAX(id) last_id FROM transactions WHERE source_file<>'' GROUP BY source_file").fetchall()
        source_ids={row['source_file']:row['last_id'] for row in transaction_sources}
        tracked_order=[]
        for row in db.execute("SELECT source_file,MAX(created_at) imported_at,MAX(id) latest_id FROM bank_statement_periods WHERE source_file<>'' GROUP BY source_file ORDER BY imported_at DESC,latest_id DESC").fetchall():
            if row['source_file'] in source_ids and row['source_file'] not in tracked_order:tracked_order.append(row['source_file'])
        statements=tracked_order+[name for name,_ in sorted(source_ids.items(),key=lambda item:item[1],reverse=True) if name not in tracked_order]
        tracked_files={row['source_file'] for row in period_rows}
        untracked_statement_count=sum(1 for name in statements if name not in tracked_files)
        statement_ranges={}
        for row in period_rows:
            name=row['source_file']
            if not name:continue
            bounds=statement_ranges.setdefault(name,{'start':row['period_start'],'end':row['period_end']})
            bounds['start']=min(bounds['start'],row['period_start'])
            bounds['end']=max(bounds['end'],row['period_end'])
        all_statements=source_file=='__all__'
        if all_statements:source_file=''
        elif not source_file and statements:source_file=statements[0]
        if source_file and source_file not in statements:source_file=''
        selected_periods=[row for row in period_rows if row['source_file']==source_file] if source_file else []
        if selected_periods:
            if not start_date:start_date=min(row['period_start'] for row in selected_periods)
            if not end_date:end_date=max(row['period_end'] for row in selected_periods)
        clauses=[];params=[]
        if source_file and source_file in statements:
            clauses.append('source_file=?');params.append(source_file)
            snapshot=db.execute('SELECT * FROM bank_snapshots WHERE source_file=? ORDER BY as_of_date DESC,id DESC LIMIT 1',(source_file,)).fetchone()
        else:
            source_file=''
            snapshot=db.execute('SELECT * FROM bank_snapshots ORDER BY as_of_date DESC,id DESC LIMIT 1').fetchone()
        if start_date:clauses.append('txn_date>=?');params.append(start_date)
        if end_date:clauses.append('txn_date<=?');params.append(end_date)
        if min_value is not None:clauses.append('amount>=?');params.append(min_value)
        if max_value is not None:clauses.append('amount<=?');params.append(max_value)
        if search:
            db.create_function('CASEFOLD',1,lambda value:str(value or '').casefold())
            clauses.append("CASEFOLD(COALESCE(counterparty,'') || ' ' || COALESCE(description,'')) LIKE ?")
            params.append('%'+search.casefold()+'%')
        where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
        transaction_count=db.execute(f'SELECT COUNT(*) n FROM transactions{where}',params).fetchone()['n']
        page_count=max(1,(transaction_count+page_size-1)//page_size)
        page_number=min(page_number,page_count)
        tx=[dict(row) for row in db.execute(f'SELECT * FROM transactions{where} ORDER BY txn_date DESC,id DESC LIMIT ? OFFSET ?',(*params,page_size,(page_number-1)*page_size)).fetchall()]
        amount_expr="CASE WHEN COALESCE(signed_amount,0)<>0 THEN signed_amount WHEN direction='incoming' THEN amount ELSE -amount END"
        totals_rows=db.execute(f"SELECT COALESCE(currency,'EUR') currency,SUM(CASE WHEN ({amount_expr})<0 THEN ABS({amount_expr}) ELSE 0 END) outgoing,SUM(CASE WHEN ({amount_expr})>=0 THEN ({amount_expr}) ELSE 0 END) incoming FROM transactions{where} GROUP BY currency",params).fetchall() if read_only else []
        page_params={key:value for key,value in {'statement':'__all__' if all_statements else source_file,'from':start_date,'to':end_date,'search':search,'min_amount':min_amount,'max_amount':max_amount,'tab':tab,'bill_id':focus_bill_id,'view':'overall' if read_only else 'matching'}.items() if value}
        page_url=lambda number:'/reconcile?'+urlencode({**page_params,'page':number})
        all_bills=[dict(row) for row in db.execute("SELECT id,supplier,invoice_number,invoice_date,due_date,scheduled_pay_date,amount,currency,status,payment_reference,payment_model,payment_purpose,payment_description,description,decision_reason,category,document_type,created_by FROM bills WHERE status IN (?,?,?) ORDER BY COALESCE(NULLIF(scheduled_pay_date,''),NULLIF(due_date,''),'9999-12-31'),id DESC",(READY,ORDER_ISSUE,PAID)).fetchall()]
        bills=[bill for bill in all_bills if submitter_id is None or bill['created_by']==submitter_id]
        allocations=[dict(row) for row in db.execute('SELECT a.*,b.supplier,b.invoice_number FROM payment_allocations a JOIN bills b ON b.id=a.bill_id ORDER BY a.id').fetchall()]
    snapshot_data=dict(snapshot) if snapshot else None
    if read_only:
        totals={row['currency']:{'currency':row['currency'],'outgoing':float(row['outgoing'] or 0),'incoming':float(row['incoming'] or 0),'approved':0.0} for row in totals_rows};outgoing=[];incoming=[];approved_orders=[];paid_by_bill={};refunds_by_bill={}
        if snapshot_data:
            totals.setdefault(snapshot_data['currency'] or 'EUR',{'currency':snapshot_data['currency'] or 'EUR','outgoing':0.0,'incoming':0.0,'approved':0.0})
        for allocation in allocations:
            if allocation['allocation_type']=='bill_payment':paid_by_bill[allocation['bill_id']]=paid_by_bill.get(allocation['bill_id'],0)+allocation['amount']
            elif allocation['allocation_type']=='supplier_refund':refunds_by_bill[allocation['bill_id']]=refunds_by_bill.get(allocation['bill_id'],0)+allocation['amount']
        for transaction in tx:
            amount=float(transaction['signed_amount'] or 0)
            if not amount:amount=float(transaction['amount'] or 0)*(1 if transaction['direction']=='incoming' else -1)
            currency=transaction['currency'] or 'EUR'
            item={**transaction,'display_amount':abs(amount)}
            totals.setdefault(currency,{'currency':currency,'outgoing':0.0,'incoming':0.0,'approved':0.0})
            if amount<0:outgoing.append(item)
            else:incoming.append(item)
        for bill in all_bills:
            if bill['status'] not in (READY,ORDER_ISSUE):continue
            remaining=max(0.0,float(bill['amount'])-paid_by_bill.get(bill['id'],0)+refunds_by_bill.get(bill['id'],0))
            if remaining<=0.009:continue
            payment_date=bill['scheduled_pay_date'] or bill['due_date'] or ''
            if (start_date or end_date) and not payment_date:continue
            if start_date and payment_date<start_date:continue
            if end_date and payment_date>end_date:continue
            if search and search.casefold() not in (str(bill.get('supplier') or '')+' '+str(bill.get('invoice_number') or '')).casefold():continue
            if min_value is not None and remaining<min_value:continue
            if max_value is not None and remaining>max_value:continue
            currency=bill['currency'] or 'EUR'
            totals.setdefault(currency,{'currency':currency,'outgoing':0.0,'incoming':0.0,'approved':0.0})['approved']+=remaining
            approved_orders.append({**bill,'payment_date':payment_date,'remaining':remaining})
        summaries=[]
        for currency,bucket in sorted(totals.items()):
            bucket['current_net']=bucket['incoming']-bucket['outgoing']
            bucket['projected_net']=bucket['current_net']-bucket['approved']
            bucket['account_balance']=snapshot_data['closing_balance'] if snapshot_data and snapshot_data['currency']==currency else None
            bucket['projected_balance']=bucket['account_balance']-bucket['approved'] if bucket['account_balance'] is not None else None
            summaries.append(bucket)
        return render_page('validator_reconcile.html',title='Bank reconciliation',
                           bank_situation=bank_situation,
                           outgoing=outgoing,incoming=incoming,approved_orders=approved_orders,
                           summaries=summaries,start_date=start_date,end_date=end_date,
                           search=search,min_amount=min_amount,max_amount=max_amount,
                           transaction_count=transaction_count,transaction_page=page_number,
                           transaction_pages=page_count,statement_filter='__all__' if all_statements else source_file,
                           statements=statements,statement_ranges=statement_ranges,selected_statement=source_file,
                           snapshot=snapshot_data,can_match=can_match,
                           matching_url='/reconcile?'+urlencode({**page_params,'view':'matching','page':1}),
                           previous_page=page_number-1 if page_number>1 else 0,
                           next_page=page_number+1 if page_number<page_count else 0,
                           previous_page_url=page_url(page_number-1) if page_number>1 else '',
                           next_page_url=page_url(page_number+1) if page_number<page_count else '')
    shared_params={key:value for key,value in {'statement':'__all__' if all_statements else source_file,'from':start_date,'to':end_date,'search':search,'min_amount':min_amount,'max_amount':max_amount}.items() if value}
    shared_params['page']=page_number
    shared_params['view']='matching'
    statement_tab_url='/reconcile?'+urlencode({**shared_params,'tab':'statement'})
    order_params={**shared_params,'tab':'orders'}
    if focus_bill_id:order_params['bill_id']=focus_bill_id
    orders_tab_url='/reconcile?'+urlencode(order_params)
    overall_params={**shared_params,'view':'overall','page':1}
    overall_url='/reconcile?'+urlencode(overall_params)
    allocations_by_tx={};paid_by_bill={};refunded_by_bill={}
    for allocation in allocations:
        allocations_by_tx.setdefault(allocation['transaction_id'],[]).append(allocation)
        if allocation['allocation_type']=='bill_payment':paid_by_bill[allocation['bill_id']]=paid_by_bill.get(allocation['bill_id'],0)+allocation['amount']
        elif allocation['allocation_type']=='supplier_refund':refunded_by_bill[allocation['bill_id']]=refunded_by_bill.get(allocation['bill_id'],0)+allocation['amount']
    entries=[]
    for transaction in tx:
        incoming=transaction['direction']=='incoming'
        prior=allocations_by_tx.get(transaction['id'],[])
        if prior:
            entries.append({'transaction':transaction,'incoming':incoming,'kind':'allocated','allocations':prior,'candidates':[]})
        elif transaction['kind'] in ('other_cost','other_income'):
            entries.append({'transaction':transaction,'incoming':incoming,'kind':'classified','allocations':[],'candidates':[]})
        else:
            candidates=[];tref=normalized_reference(transaction['reference'])
            for bill in bills:
                net=paid_by_bill.get(bill['id'],0)-refunded_by_bill.get(bill['id'],0)
                remaining=bill['amount']-net
                eligible=(net>0 and bill['currency']==transaction['currency']) if incoming else (remaining>0 and bill['currency']==transaction['currency'] and bill['status'] in (READY,ORDER_ISSUE))
                if eligible:
                    refs={normalized_reference(bill['payment_reference']),normalized_reference(bill['payment_model']+bill['payment_reference'])}
                    open_amount=net if incoming else remaining
                    day_gap=None
                    try:day_gap=abs((date.fromisoformat(transaction['txn_date'])-date.fromisoformat(bill['due_date'] or bill['invoice_date'])).days)
                    except (ValueError,TypeError):pass
                    name_score=counterparty_similarity(transaction.get('counterparty') or transaction['description'],bill['supplier'])
                    invoice_ref=normalized_reference(bill['invoice_number'])
                    invoice_match=bool(len(invoice_ref)>=4 and invoice_ref in normalized_reference(transaction['description']+' '+transaction['reference']))
                    candidates.append({'bill':bill,'remaining':open_amount,'reference_match':bool(tref and tref in refs),'invoice_match':invoice_match,'amount_difference':abs(open_amount-transaction['amount']),'day_gap':day_gap,'name_score':name_score})
            candidates.sort(key=lambda item:(not item['reference_match'],item['amount_difference']>=0.01,item['day_gap'] is None,item['day_gap'] if item['day_gap'] is not None else 999,-item['name_score']))
            entries.append({'transaction':transaction,'incoming':incoming,'kind':'unmatched','allocations':[],
                            'candidates':suggested_matches(candidates),'eligible_bills':candidates,
                            'other_path':'/other-income' if incoming else '/other-cost',
                            'other_label':'Other income' if incoming else 'Other cost'})
    payment_orders=[]
    for bill in bills:
        if focus_bill_id and bill['id']!=focus_bill_id:continue
        if bill['status']!=ORDER_ISSUE:continue
        paid=paid_by_bill.get(bill['id'],0);refunded=refunded_by_bill.get(bill['id'],0)
        remaining=max(0.0,float(bill['amount'])-paid+refunded)
        if remaining<=0.009:continue
        candidates=[]
        bill_day=bill['scheduled_pay_date'] or bill['due_date'] or bill['invoice_date']
        for transaction in tx:
            if transaction['direction']!='outgoing' or transaction['kind'] in ('other_cost','other_income'):
                continue
            if allocations_by_tx.get(transaction['id']) or transaction['currency']!=bill['currency'] or transaction['amount']>remaining+0.009:
                continue
            gap=None
            try:gap=abs((date.fromisoformat(transaction['txn_date'])-date.fromisoformat(bill_day)).days)
            except (ValueError,TypeError):pass
            name_score=counterparty_similarity(transaction.get('counterparty') or transaction['description'],bill['supplier'])
            tref=normalized_reference(transaction['reference'])
            refs={normalized_reference(bill['payment_reference']),normalized_reference(bill['payment_model']+bill['payment_reference'])}
            invoice_ref=normalized_reference(bill['invoice_number'])
            invoice_match=bool(len(invoice_ref)>=4 and invoice_ref in normalized_reference(transaction['description']+' '+transaction['reference']))
            candidates.append({'transaction':transaction,'allocation_amount':transaction['amount'],'invoice_match':invoice_match,
                               'amount_difference':abs(remaining-transaction['amount']),'day_gap':gap,
                               'name_score':name_score,'reference_match':bool(tref and tref in refs)})
        candidates.sort(key=lambda item:(not item['reference_match'],item['amount_difference']>=0.01,
                                         item['day_gap'] is None,item['day_gap'] if item['day_gap'] is not None else 999,
                                         -item['name_score']))
        payment_orders.append({'bill':bill,'remaining':remaining,'candidates':suggested_matches(candidates)})
    snapshot=dict(snapshot) if snapshot else None
    grouped={}
    for row in period_rows:
        grouped.setdefault((row['account_ref'],row['currency']),[]).append(dict(row))
    period_gaps=[]
    for (account_ref,currency),periods in grouped.items():
        periods.sort(key=lambda item:(item['period_start'],item['period_end']))
        covered_end=date.fromisoformat(periods[0]['period_end'])
        for period in periods[1:]:
            start=date.fromisoformat(period['period_start']);end=date.fromisoformat(period['period_end'])
            if start>covered_end+timedelta(days=1):
                period_gaps.append({'account_ref':account_ref,'currency':currency,
                    'start':(covered_end+timedelta(days=1)).isoformat(),'end':(start-timedelta(days=1)).isoformat()})
            covered_end=max(covered_end,end)
    return render_page('reconcile.html',title='Bank reconciliation',entries=entries,payment_orders=payment_orders,
                       bank_situation=bank_situation,
                       snapshot=snapshot,statements=statements,selected_statement=source_file,
                       start_date=start_date,end_date=end_date,tab=tab,search=search,
                       min_amount=min_amount,max_amount=max_amount,
                       statement_tab_url=statement_tab_url,orders_tab_url=orders_tab_url,
                       focus_bill_id=focus_bill_id,period_gaps=period_gaps,
                       untracked_statement_count=untracked_statement_count,
                       transaction_count=transaction_count,transaction_page=page_number,
                       transaction_pages=page_count,
                       previous_page_url=page_url(page_number-1) if page_number>1 else '',
                       next_page_url=page_url(page_number+1) if page_number<page_count else '',
                       statement_filter='__all__' if all_statements else source_file,
                       statement_ranges=statement_ranges,
                       overall_url=overall_url,can_classify=submitter_id is None,
                       focus_message=(f'Bill #{focus_bill_id} is not waiting for bank confirmation. It may already be fully matched.'
                                      if focus_bill_id and not payment_orders else ''))

