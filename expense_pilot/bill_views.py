"""Bill Views for the expense pilot."""
from __future__ import annotations

import json
import mimetypes
from datetime import date, timedelta
from urllib.parse import urlencode
from .config import EXPENSE_CONFIG
from .database import connect
from .request_context import REQUEST_CONTEXT
from .web import render_page, page, parse_european_number
from .comments import unread_bill_comment_count, bill_discussion_data
from .payment_codes import hub3_svg
from .workflow import DOCUMENT_TYPES, status_history, offer_matches
from .workflow import PENDING, READY, ORDER_ISSUE, PAID, REJECTED

RECENT_BILL_DAYS = 14

def dashboard():
    user=getattr(REQUEST_CONTEXT,'user',None)
    owner_filter=''
    owner_args=()
    with connect() as db:
        bills=[dict(row) for row in db.execute("SELECT b.* FROM bills b WHERE b.status NOT IN ('Paid at bank','Paid','Solved','Deleted','Void')"+owner_filter+" ORDER BY b.id DESC",owner_args).fetchall()]
        for bill in bills:bill['unread']=unread_bill_comment_count(db,user['id'],bill['id']) if user else 0
        openb=sum(bill['status']=='Needs review' for bill in bills)
        unmatched=db.execute("SELECT COUNT(*) n FROM transactions WHERE matched_bill_id IS NULL").fetchone()['n']
        spend=db.execute("SELECT COALESCE(SUM(amount),0) n FROM bills WHERE invoice_date>=? AND status NOT IN ('Deleted','Void')",((date.today()-timedelta(days=7)).isoformat(),)).fetchone()['n']
    return render_page('dashboard.html',title='Expense overview',bills=bills,openb=openb,unmatched=unmatched,spend=spend,categories=EXPENSE_CONFIG['categories'])


def manual_bill_page(message=''):
    with connect() as db:suppliers=[dict(row) for row in db.execute('SELECT * FROM supplier_profiles ORDER BY name COLLATE NOCASE').fetchall()]
    fields=[
        {'label':'Supplier name','name':'supplier','id':'supplier','required':True},
        {'label':'Invoice number','name':'invoice_number','required':True},
        {'label':'Issue date','name':'invoice_date','type':'date'},
        {'label':'Due date','name':'due_date','type':'date'},
        {'label':'Total amount','name':'amount','type':'number','required':True,'step':'0.01','min':'0.01'},
        {'label':'VAT amount','name':'vat_amount','type':'number','step':'0.01','min':'0'},
        {'label':'Currency','name':'currency','value':'EUR'},
        {'label':'Supplier address','name':'supplier_address','id':'supplier_address'},
        {'label':'Supplier OIB','name':'supplier_oib','id':'supplier_oib'},
        {'label':'Supplier VAT ID','name':'supplier_vat_id','id':'supplier_vat_id'},
        {'label':'Supplier IBAN','name':'supplier_iban','id':'supplier_iban'},
        {'label':'Payment model','name':'payment_model','value':'HR00'},
        {'label':'Poziv na broj primatelja','name':'payment_reference'},
        {'label':'Payment purpose','name':'payment_purpose','value':'OTHR'},
        {'label':'Payment description','name':'payment_description'},
        {'label':'Bill description','name':'description'},
    ]
    return render_page('manual_bill.html',title='Enter bill manually',message=message,fields=fields,
                       suppliers=suppliers,categories=EXPENSE_CONFIG['categories'],cost_centers=EXPENSE_CONFIG['cost_centers'],
                       document_types=DOCUMENT_TYPES)


def payer_page(query):
    today=date.today().isoformat()
    recent_cutoff=(date.today()-timedelta(days=RECENT_BILL_DAYS)).isoformat()
    until=query.get('until',today)
    try:until=date.fromisoformat(until).isoformat()
    except ValueError:until=today
    show_all=query.get('all','')=='1'
    chosen=query.get('bill','')
    chosen_id=int(chosen) if str(chosen).isdigit() else 0
    user=getattr(REQUEST_CONTEXT,'user',None)
    owner_filter=''
    owner_args=()
    with connect() as db:
        active=[dict(row) for row in db.execute(
            "SELECT * FROM bills WHERE (status IN (?,?,?) OR (status=? AND substr(COALESCE(NULLIF(decided_at,''),created_at),1,10)>=?))"+owner_filter+
            " ORDER BY COALESCE(scheduled_pay_date,due_date,'9999-12-31'),due_date,id",
            (PENDING,READY,ORDER_ISSUE,REJECTED,recent_cutoff,*owner_args)).fetchall()]
        archive=[dict(row) for row in db.execute(
            "SELECT b.* FROM bills b WHERE b.status IN (?, 'Solved') AND substr(COALESCE(NULLIF(paid_at,''),NULLIF(decided_at,''),created_at),1,10)>=?"+owner_filter+
            " ORDER BY COALESCE(paid_at,decided_at,created_at) DESC,id DESC LIMIT 100",
            (PAID,recent_cutoff,*owner_args)).fetchall()]
    due_rows=[]
    for bill in active:
        if bill['status'] in (PENDING,REJECTED,ORDER_ISSUE) or show_all or (bill['scheduled_pay_date'] or bill['due_date'] or today)<=until:
            schedule=bill['scheduled_pay_date'] or bill['due_date'] or 'Not scheduled'
            action='Awaiting approval' if bill['status']==PENDING else ('Resolve' if bill['status']==REJECTED else ('Open payment code' if bill['status']==ORDER_ISSUE else 'Open payment'))
            with connect() as db:unread=unread_bill_comment_count(db,user['id'],bill['id']) if user else 0
            url=(f'/bill/{bill["id"]}' if bill['status']==READY else
                 f'/payer?until={until}&all={1 if show_all else 0}&bill={bill["id"]}')
            if bill['status']==READY:action='Review next step'
            due_rows.append({'bill':bill,'schedule':schedule,'action':action,'url':url,'unread':unread})
    selected=next((bill for bill in active if bill['id']==chosen_id),None)
    close_url=f'/payer?until={until}&all={1 if show_all else 0}'
    mode='';payment_svg='';payment_error='';discussion={};thread_return=''
    if selected:
        if selected['status'] in (READY,ORDER_ISSUE):
            mode='payment'
            try:payment_svg=hub3_svg(selected)
            except Exception as exc:payment_error=str(exc)
        elif selected['status']==PENDING:mode='awaiting'
        else:mode='rejected'
        thread_return=f'/payer?until={until}&all={1 if show_all else 0}&bill={selected["id"]}'
        discussion=bill_discussion_data(selected['id'],thread_return,user,mark_read=True)
    return render_page('payer.html',title='Submitter / payer dashboard',until=until,show_all=show_all,
                       due_rows=due_rows,archive=archive,selected=selected,modal_mode=mode,
                       recent_bill_days=RECENT_BILL_DAYS,
                       payment_svg=payment_svg,payment_error=payment_error,discussion=discussion,
                       thread_return=thread_return,close_url=close_url)


def archive_page(query=None,message=''):
    query=query or {}
    user=getattr(REQUEST_CONTEXT,'user',None)
    owner_filter=''
    owner_args=()
    def clean_date(value):
        try:return date.fromisoformat(str(value or '')).isoformat() if value else ''
        except (TypeError,ValueError):return ''
    start_date=clean_date(query.get('from',''))
    end_date=clean_date(query.get('to',''))
    if start_date and end_date and start_date>end_date:start_date,end_date=end_date,start_date
    supplier_search=str(query.get('supplier','') or '').strip()
    status_filter=query.get('status','') if query.get('status','') in ('Rejected','Solved',PAID,'Paid','Deleted','Void') else ''
    def amount_filter(value):
        if not str(value or '').strip():return None
        try:
            parsed=parse_european_number(value)
            return parsed if parsed>=0 else None
        except (TypeError,ValueError):return None
    min_amount=amount_filter(query.get('min_amount',''))
    max_amount=amount_filter(query.get('max_amount',''))
    min_text=str(query.get('min_amount','') or '').strip()
    max_text=str(query.get('max_amount','') or '').strip()
    if not message and ((min_text and min_amount is None) or (max_text and max_amount is None)):
        message='Enter valid non-negative amounts using European number format, for example 1.234,56.'
    try:page_number=max(1,int(query.get('page',1) or 1))
    except (TypeError,ValueError):page_number=1
    page_size=100
    archive_date_expr="COALESCE(NULLIF(substr(b.paid_at,1,10),''),NULLIF(substr(b.decided_at,1,10),''),NULLIF(b.invoice_date,''),substr(b.created_at,1,10))"
    with connect() as db:
        if status_filter in ('Deleted','Void'):
            conditions=['b.status=?']
            params=[status_filter]
        else:
            conditions=["b.status IN (?, ?, ?, 'Solved')"]
            params=[PAID,REJECTED,'Paid']
        if owner_filter:conditions.append('b.created_by=?');params.extend(owner_args)
        if status_filter and status_filter not in ('Deleted','Void'):
            conditions.append('b.status=?');params.append(status_filter)
        if start_date:conditions.append(f'{archive_date_expr}>=?');params.append(start_date)
        if end_date:conditions.append(f'{archive_date_expr}<=?');params.append(end_date)
        if supplier_search:
            db.create_function('CASEFOLD',1,lambda value:str(value or '').casefold())
            conditions.append('CASEFOLD(b.supplier) LIKE ?');params.append('%'+supplier_search.casefold()+'%')
        if min_amount is not None:conditions.append('b.amount>=?');params.append(min_amount)
        if max_amount is not None:conditions.append('b.amount<=?');params.append(max_amount)
        where=' WHERE '+' AND '.join(conditions)
        count=db.execute('SELECT COUNT(*) n FROM bills b'+where,params).fetchone()['n']
        pages=max(1,(count+page_size-1)//page_size)
        page_number=min(page_number,pages)
        completed=[dict(row) for row in db.execute(
            f"SELECT b.*, {archive_date_expr} archive_date FROM bills b{where} ORDER BY archive_date DESC,b.id DESC LIMIT ? OFFSET ?",
            (*params,page_size,(page_number-1)*page_size)).fetchall()]
        manual_records=[dict(row) for row in db.execute(
            "SELECT * FROM bills b WHERE status='Recorded' AND workflow_route='record' "
            "AND original_name='Manual entry' AND COALESCE(stored_path,'')=''"+owner_filter+
            " ORDER BY id DESC",owner_args).fetchall()]
        deletable=[]
        for bill in manual_records:
            has_allocations=db.execute('SELECT 1 FROM payment_allocations WHERE bill_id=? LIMIT 1',(bill['id'],)).fetchone()
            has_transaction=db.execute('SELECT 1 FROM transactions WHERE matched_bill_id=? LIMIT 1',(bill['id'],)).fetchone()
            has_comments=db.execute('SELECT 1 FROM bill_comments WHERE bill_id=? LIMIT 1',(bill['id'],)).fetchone()
            audit_actions=db.execute("SELECT action FROM audit_log WHERE entity_type='bill' AND entity_id=?",(str(bill['id']),)).fetchall()
            actions={row['action'] for row in audit_actions}
            eligible=(not has_allocations and not has_transaction and not bill['transaction_id'] and
                      not has_comments and not bill['paid_at'] and not bill['decided_at'] and
                      not bill['validated_by'] and not bill['requires_approval'] and actions <= {'recorded'})
            if eligible:deletable.append(bill)
    page_params={key:value for key,value in {'from':start_date,'to':end_date,'supplier':supplier_search,
        'status':status_filter,'min_amount':min_text,'max_amount':max_text}.items() if value}
    page_url=lambda number:'/archive?'+urlencode({**page_params,'page':number})
    return render_page('archive.html',title='Archive',completed=completed,deletable=deletable,message=message,
        start_date=start_date,end_date=end_date,supplier_search=supplier_search,status_filter=status_filter,
        min_amount=min_text,max_amount=max_text,total_count=count,page_number=page_number,total_pages=pages,
        previous_page_url=page_url(page_number-1) if page_number>1 else '',
        next_page_url=page_url(page_number+1) if page_number<pages else '')


def bills_page(query=''):
    user=getattr(REQUEST_CONTEXT,'user',None)
    with connect() as db:
        suppliers=[dict(row) for row in db.execute("SELECT id,name,oib FROM supplier_profiles ORDER BY name COLLATE NOCASE").fetchall()]
        selected=int(query) if str(query).isdigit() else 0
        if selected: rows=db.execute("SELECT * FROM bills WHERE supplier_profile_id=? AND status<>'Deleted' ORDER BY id DESC",(selected,)).fetchall()
        elif query: rows=db.execute("SELECT * FROM bills WHERE supplier=? AND status<>'Deleted' ORDER BY id DESC",(query,)).fetchall()
        else: rows=db.execute("SELECT * FROM bills WHERE status<>'Deleted' ORDER BY id DESC").fetchall()
        bills=[dict(row) for row in rows]
        for bill in bills:bill['unread']=unread_bill_comment_count(db,user['id'],bill['id']) if user else 0
    return render_page('bills.html',title='Bills',bills=bills,suppliers=suppliers,selected=selected)


def suppliers_page():
    with connect() as db:
        rows=db.execute("SELECT s.id,s.name,s.oib,s.iban,COUNT(b.id) bill_count,COALESCE(SUM(b.amount),0) total FROM supplier_profiles s LEFT JOIN bills b ON b.supplier_profile_id=s.id AND b.status<>'Deleted' GROUP BY s.id ORDER BY s.name COLLATE NOCASE").fetchall()
    return render_page('suppliers.html',title='Suppliers',suppliers=[dict(row) for row in rows])


def bill_page(bid):
    with connect() as db:
        bill_row=db.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
        if not bill_row:return page('<h1>Bill not found</h1>'),404
        bill=dict(bill_row)
        items=[dict(row) for row in db.execute("SELECT * FROM bill_items WHERE bill_id=? ORDER BY item_index",(bid,)).fetchall()]
        match_count=db.execute('SELECT COUNT(*) n FROM payment_allocations WHERE bill_id=?',(bid,)).fetchone()['n']
        linked_match=db.execute('SELECT COUNT(*) n FROM transactions WHERE matched_bill_id=?',(bid,)).fetchone()['n']
        suppliers=[dict(row) for row in db.execute('SELECT * FROM supplier_profiles ORDER BY oib,name COLLATE NOCASE').fetchall()]
        history=status_history(db,bid)
        related_documents=[dict(row) for row in db.execute('SELECT * FROM bill_documents WHERE bill_id=? ORDER BY id',(bid,)).fetchall()]
    for item in items:
        item['payload']=json.dumps({key:item[key] for key in ('quantity','unit','unit_price','discount','vat_rate','amount')},ensure_ascii=False)
    mime=mimetypes.guess_type(bill['original_name'])[0] or ''
    preview_kind='pdf' if mime=='application/pdf' else ('image' if mime.startswith('image/') else ('text' if mime.startswith('text/') else 'other'))
    has_match=bool(match_count or linked_match or bill['transaction_id'])
    user=getattr(REQUEST_CONTEXT,'user',None)
    can_manage=bool(user)
    can_pay=bool(can_manage and bill['status'] in (READY,ORDER_ISSUE))
    can_reconcile=bool(can_manage and bill['status']==ORDER_ISSUE)
    can_cancel=bool(can_manage and bill['status'] not in (PAID,'Paid','Deleted','Void','Solved'))
    can_permanently_delete=bool(can_manage and bill['status'] in (PAID,'Paid','Solved'))
    editable=bool(bill['status'] in ('Needs review',PENDING) and not has_match and can_manage)
    can_decide=bool(user and bill['created_by']!=user['id'])
    fields=[
        {'label':'Supplier name','name':'supplier','id':'supplier-name','value':bill['supplier']},
        {'label':'Invoice / offer number','name':'invoice_number','value':bill['invoice_number']},
        {'label':'Issue date','name':'invoice_date','type':'date','value':bill['invoice_date']},
        {'label':'Due date','name':'due_date','type':'date','value':bill['due_date']},
        {'label':'Total amount','name':'amount','type':'number','step':'0.01','min':'0.01','value':bill['amount']},
        {'label':'VAT amount','name':'vat_amount','type':'number','step':'0.01','value':bill['vat_amount']},
        {'label':'Currency','name':'currency','value':bill['currency']},
        {'label':'Category','name':'category','kind':'select','options':EXPENSE_CONFIG['categories'],'value':bill['category']},
        {'label':'Cost center','name':'cost_center','kind':'select','options':EXPENSE_CONFIG['cost_centers'],'value':bill['cost_center']},
        {'label':'Supplier address','name':'supplier_address','id':'supplier-address','value':bill['supplier_address']},
        {'label':'Supplier OIB','name':'supplier_oib','id':'supplier-oib','value':bill['supplier_oib']},
        {'label':'Supplier VAT ID','name':'supplier_vat_id','id':'supplier-vat-id','value':bill['supplier_vat_id']},
        {'label':'Supplier IBAN','name':'supplier_iban','id':'supplier-iban','value':bill['supplier_iban']},
        {'label':'Payment model','name':'payment_model','value':bill['payment_model']},
        {'label':'Payment reference','name':'payment_reference','value':bill['payment_reference']},
        {'label':'Payment purpose','name':'payment_purpose','value':bill['payment_purpose']},
        {'label':'Payment description','name':'payment_description','value':bill['payment_description']},
        {'label':'Description','name':'description','value':bill['description']},
    ]
    discussion=bill_discussion_data(bill['id'],'/bill/'+str(bill['id']),user,mark_read=True)
    candidates=[]
    if editable and (bill['status']=='Needs review' or not bill['document_type']) and bill['document_type'] in ('','invoice'):
        with connect() as db:candidates=offer_matches(db,bill['supplier'],bill['supplier_oib'],bill['amount'],bill['currency'],exclude_id=bill['id'])
    if bill['status'] in ('Needs review',PENDING):
        page_heading=f'Review bill #{bill["id"]}'
    elif bill['status'] in (PAID,'Paid','Solved'):
        page_heading=f'Archived bill #{bill["id"]}'
    else:
        page_heading=f'Bill details #{bill["id"]}'
    return render_page('bill_detail.html',title=page_heading,page_heading=page_heading,bill=bill,items=items,suppliers=suppliers,
                       fields=fields,categories=EXPENSE_CONFIG['categories'],preview_kind=preview_kind,
                       raw_text=bill['raw_text'][:12000],has_match=has_match,can_cancel=can_cancel,
                       can_permanently_delete=can_permanently_delete,editable=editable,
                       can_decide=can_decide,can_pay=can_pay,can_reconcile=can_reconcile,
                       discussion=discussion,history=history,
                       related_documents=related_documents,document_types=DOCUMENT_TYPES,
                       offer_candidates=candidates,today=date.today().isoformat())


def uploaded_page(results):
    with connect() as db:
        prepared=[]
        for result in results:
            row=db.execute('SELECT id,supplier,invoice_number,amount,currency,status FROM bills WHERE id=?',(result['id'],)).fetchone()
            if row:prepared.append({'bill':dict(row),'duplicate':bool(result.get('duplicate'))})
    duplicate_count=sum(1 for item in prepared if item['duplicate'])
    return render_page('uploaded.html',title='Upload complete',results=prepared,duplicate_count=duplicate_count)

