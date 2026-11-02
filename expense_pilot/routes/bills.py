import json
import mimetypes
import re
from datetime import date, datetime
from pathlib import Path
from flask import Blueprint, g, redirect, request, send_file, jsonify
from ..core import (EXPENSE_CONFIG, UPLOAD_DIR, add_bill_comment, audit, bill_page, bills_page, connect,
                   archive_page, dashboard, find_duplicate_bill, get_or_create_supplier,
                   manual_bill_page, normalize_oib, normalize_invoice_number,
                   suppliers_page, upload_one_bill, uploaded_page, page)
from ..security import login_required, roles_required
from ..bank_services import refresh_bill_payment_status
from ..bill_ingestion import compress_uploaded_document
from ..workflow import (DOCUMENT_TYPES, ORDER_ISSUE, REJECTED, initial_status, record_initial_status,
                        set_bill_status, offer_matches, attach_invoice_to_paid_offer, local_timestamp)
from ..web import parse_european_number

bp=Blueprint('bills',__name__)


@bp.get('/notifications')
@login_required
def notifications():
    from ..comments import notification_counts
    response=jsonify(notification_counts(g.user))
    response.headers['Cache-Control']='no-store'
    return response


def _bill_upload_paths(db,bill_id,bill):
    raw_paths=[bill['stored_path']] if bill['stored_path'] else []
    raw_paths.extend(row['stored_path'] for row in db.execute(
        'SELECT stored_path FROM bill_documents WHERE bill_id=?',(bill_id,)).fetchall() if row['stored_path'])
    safe_paths=[]
    for raw_path in raw_paths:
        candidate=Path(raw_path)
        if not candidate.is_absolute():candidate=Path(__file__).resolve().parent.parent/candidate
        try:candidate=candidate.resolve();candidate.relative_to(UPLOAD_DIR.resolve())
        except (OSError,ValueError):continue
        if candidate not in safe_paths:safe_paths.append(candidate)
    return safe_paths


def _remove_unused_supplier_records(db,bill):
    fingerprint=bill['fingerprint'];profile_id=bill['supplier_profile_id']
    if fingerprint and not db.execute('SELECT 1 FROM bills WHERE fingerprint=? LIMIT 1',(fingerprint,)).fetchone():
        db.execute('DELETE FROM suppliers WHERE fingerprint=?',(fingerprint,))
    if profile_id and not db.execute('SELECT 1 FROM bills WHERE supplier_profile_id=? LIMIT 1',(profile_id,)).fetchone() and not db.execute('SELECT 1 FROM suppliers WHERE supplier_profile_id=? LIMIT 1',(profile_id,)).fetchone():
        db.execute('DELETE FROM supplier_profiles WHERE id=?',(profile_id,))


def _existing_bill_url(bill):
    if bill['status'] in ('Deleted','Void'):
        return '/archive?status='+bill['status']
    return '/bill/'+str(bill['id'])


def _route_status(document_type,amount,currency):
    status=initial_status(document_type,amount,currency)
    waiting=status=='Pending for approval'
    plan='due_date' if status=='Ready for payment' else ''
    return status,waiting,plan,''


@bp.get('/')
@login_required
def overview():
    return dashboard()


@bp.get('/bills')
@login_required
@roles_required('submitter','validator','admin')
def bills_list():
    return bills_page(request.args.get('supplier',''))


@bp.get('/suppliers')
@login_required
@roles_required('submitter','validator','admin')
def suppliers_list():
    return suppliers_page()


@bp.get('/archive')
@login_required
def archive_list():
    message='Bill and its complete workflow permanently deleted.' if request.args.get('deleted')=='1' else ''
    return archive_page(request.args,message)


@bp.get('/manual-bill')
@login_required
@roles_required('submitter','validator','admin')
def manual_get():
    return manual_bill_page()


@bp.post('/begin-payment')
@login_required
@roles_required('submitter','validator','admin')
def begin_payment():
    try:bill_id=int(request.form.get('bill_id','0'))
    except ValueError:bill_id=0
    with connect() as db:
        bill=db.execute('SELECT id,created_by,status FROM bills WHERE id=?',(bill_id,)).fetchone()
        if not bill:return page('<h1>Bill not found</h1><p><a href="/payer">Return to the payer dashboard</a></p>'),404
        if bill['status']=='Ready for payment':
            set_bill_status(db,bill_id,ORDER_ISSUE,g.user['id'],'Payment order issued; awaiting bank statement confirmation')
            audit(db,'issued payment order','bill',bill_id,'PDF417 payment code opened')
        elif bill['status']!=ORDER_ISSUE:
            return page('<h1>Payment is not ready</h1><p>This bill is not in a payable status.</p><p><a href="/bill/'+str(bill_id)+'">Return to bill</a></p>'),409
    return redirect('/payer?bill='+str(bill_id))


@bp.get('/bill/<int:bill_id>')
@login_required
def bill_detail(bill_id):
    return bill_page(bill_id)


@bp.get('/file/<int:bill_id>')
@login_required
def bill_file(bill_id):
    with connect() as db:bill=db.execute('SELECT * FROM bills WHERE id=?',(bill_id,)).fetchone()
    if not bill or not bill['stored_path'] or not Path(bill['stored_path']).is_file():
        return page('<h1>File not found</h1>'),404
    mimetype=mimetypes.guess_type(bill['original_name'])[0] or 'application/octet-stream'
    return send_file(bill['stored_path'],mimetype=mimetype,as_attachment=False,download_name=Path(bill['original_name']).name)


@bp.get('/offer-candidates')
@login_required
@roles_required('submitter','validator','admin')
def offer_candidates():
    try:amount=float(request.args.get('amount') or 0)
    except ValueError:amount=0
    with connect() as db:
        rows=offer_matches(db,request.args.get('supplier',''),request.args.get('oib',''),amount,request.args.get('currency','EUR'))
    return jsonify([{'id':row['id'],'supplier':row['supplier'],'invoice_number':row['invoice_number'],
                     'amount':row['amount'],'currency':row['currency']} for row in rows])


@bp.get('/related-file/<int:document_id>')
@login_required
def related_file(document_id):
    with connect() as db:
        row=db.execute('SELECT d.*,b.created_by FROM bill_documents d JOIN bills b ON b.id=d.bill_id WHERE d.id=?',(document_id,)).fetchone()
    if not row:
        return page('<h1>File not found</h1>'),404
    path=Path(row['stored_path'])
    if not path.is_file():return page('<h1>File not found</h1>'),404
    return send_file(path,as_attachment=False,download_name=Path(row['original_name']).name)


@bp.post('/upload')
@login_required
@roles_required('submitter','validator','admin')
def upload_bills():
    files=[]
    for key in ('documents','folder','photo','document'):
        for item in request.files.getlist(key):
            if item and item.filename:
                files.append((item.filename,item.read()))
    allowed={'.pdf','.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp','.txt'}
    files=[(name,data) for name,data in files if Path(name.replace('\\','/')).suffix.lower() in allowed and data]
    if not files:return page('<h1>No supported bills selected</h1><p class="sub">Choose a PDF, image, or text document and try again.</p>'),400
    category=request.form.get('category','')
    results=[upload_one_bill(name,data,category,g.user['id']) for name,data in files]
    ids=','.join(str(x['id']) for x in results)
    duplicates=','.join(str(x['id']) for x in results if x.get('duplicate'))
    return redirect('/uploaded?ids='+ids+'&duplicates='+duplicates)


@bp.get('/uploaded')
@login_required
def uploaded():
    ids=[int(x) for x in request.args.get('ids','').split(',') if x.isdigit()]
    duplicates={int(x) for x in request.args.get('duplicates','').split(',') if x.isdigit()}
    return uploaded_page([{'id':bill_id,'duplicate':bill_id in duplicates} for bill_id in ids])


@bp.post('/bill-action')
@login_required
def bill_action():
    try:bill_id=int(request.form.get('id','0'))
    except ValueError:bill_id=0
    action=request.form.get('action','')
    delete_paths=[]
    permanently_deleted=False
    with connect() as db:
        bill=db.execute('SELECT * FROM bills WHERE id=?',(bill_id,)).fetchone()
        matches=db.execute('SELECT COUNT(*) n FROM payment_allocations WHERE bill_id=?',(bill_id,)).fetchone()['n']
        linked_tx=db.execute('SELECT 1 FROM transactions WHERE matched_bill_id=? LIMIT 1',(bill_id,)).fetchone()
        owner=bool(bill)
        unprocessed_manual=False
        if bill:
            audit_actions={row['action'] for row in db.execute("SELECT action FROM audit_log WHERE entity_type='bill' AND entity_id=?",(str(bill_id),)).fetchall()}
            has_comments=db.execute('SELECT 1 FROM bill_comments WHERE bill_id=? LIMIT 1',(bill_id,)).fetchone()
            unprocessed_manual=(bill['status']=='Recorded' and bill['workflow_route']=='record' and
                          bill['original_name']=='Manual entry' and not bill['stored_path'] and
                          not matches and not linked_tx and not bill['transaction_id'] and
                          not bill['paid_at'] and not bill['decided_at'] and not bill['validated_by'] and
                          not bill['requires_approval'] and not has_comments and audit_actions <= {'recorded'})
        archive_bill=bill and bill['status'] in ('Paid at bank','Paid','Solved',REJECTED,'Deleted','Void')
        if bill and owner and action=='permanent_delete' and (archive_bill or unprocessed_manual):
            delete_paths=_bill_upload_paths(db,bill_id,bill)
            # A transaction may contain allocations to multiple bills. Deleting
            # this bill cancels that whole reconciliation and leaves its bank
            # statement row intact and unmatched.
            affected_tx={row['transaction_id'] for row in db.execute('SELECT DISTINCT transaction_id FROM payment_allocations WHERE bill_id=?',(bill_id,)).fetchall()}
            affected_tx.update(row['id'] for row in db.execute('SELECT id FROM transactions WHERE matched_bill_id=?',(bill_id,)).fetchall())
            if bill['transaction_id']:
                affected_tx.add(bill['transaction_id'])
            affected_bills=set()
            for txid in affected_tx:
                affected_bills.update(row['bill_id'] for row in db.execute('SELECT DISTINCT bill_id FROM payment_allocations WHERE transaction_id=? AND bill_id<>?',(txid,bill_id)).fetchall())
                db.execute('DELETE FROM payment_allocations WHERE transaction_id=?',(txid,))
                db.execute("UPDATE transactions SET matched_bill_id=NULL,kind='unclassified' WHERE id=?",(txid,))
                db.execute("DELETE FROM audit_log WHERE entity_type='transaction' AND entity_id=? AND action IN ('matched payment','matched refund')",(str(txid),))
            db.execute('DELETE FROM payment_allocations WHERE bill_id=?',(bill_id,))
            db.execute("DELETE FROM audit_log WHERE entity_type='bill' AND entity_id=?",(str(bill_id),))
            db.execute('UPDATE transactions SET matched_bill_id=NULL WHERE matched_bill_id=?',(bill_id,))
            if affected_tx:
                placeholders=','.join('?' for _ in affected_tx)
                db.execute(f'UPDATE bills SET transaction_id=NULL WHERE transaction_id IN ({placeholders})',tuple(affected_tx))
            db.execute('DELETE FROM bills WHERE id=?',(bill_id,))
            for other_id in affected_bills:
                refresh_bill_payment_status(db,other_id)
            _remove_unused_supplier_records(db,bill)
            permanently_deleted=True
        elif bill and owner and action=='delete' and bill['status'] not in ('Paid at bank','Paid','Deleted','Void','Solved') and not matches and not linked_tx and not bill['transaction_id']:
            delete_paths=_bill_upload_paths(db,bill_id,bill)
            # Delete removes the bill row and cascades its thread and workflow
            # history. Keep the connected supplier profile only if another bill
            # or supplier memory still uses it.
            db.execute("DELETE FROM audit_log WHERE entity_type='bill' AND entity_id=?",(str(bill_id),))
            db.execute('DELETE FROM bills WHERE id=?',(bill_id,))
            _remove_unused_supplier_records(db,bill)
            permanently_deleted=True
    for delete_path in delete_paths:
        try:delete_path.unlink(missing_ok=True)
        except OSError:pass
    if permanently_deleted:return redirect('/archive?deleted=1')
    return redirect('/archive' if action=='permanent_delete' else '/bill/'+str(bill_id))


@bp.post('/save-bill')
@login_required
@roles_required('submitter','validator','admin')
def save_reviewed_bill():
    try:bill_id=int(request.form.get('id','0'))
    except ValueError:bill_id=0
    route=request.form.get('document_type','').strip()
    if route not in DOCUMENT_TYPES:
        return page('<h1>Choose a document type</h1><p class="notice">Select the type before proceeding.</p><p><a href="/bill/'+str(bill_id)+'">Return to bill review</a></p>'),400
    names=['supplier','invoice_number','invoice_date','due_date','currency','category','cost_center','description','supplier_address','supplier_oib','supplier_vat_id','supplier_iban','payment_model','payment_reference','payment_purpose','payment_description']
    fields={key:request.form.get(key,'').strip() for key in names}
    if fields['category'] not in EXPENSE_CONFIG['categories']:
        return page('<h1>Choose a category</h1><p class="notice">Select a bill category before saving.</p><p><a href="/bill/'+str(bill_id)+'">Return to bill review</a></p>'),400
    try:amount=parse_european_number(request.form.get('amount'))
    except ValueError:amount=0
    try:vat_amount=parse_european_number(request.form.get('vat_amount'))
    except ValueError:vat_amount=0
    if amount<=0:return page('<h1>Enter a valid bill amount</h1><p><a href="/bill/'+str(bill_id)+'">Return to bill review</a></p>'),400
    with connect() as db:
        existing=db.execute('SELECT * FROM bills WHERE id=?',(bill_id,)).fetchone()
        if not existing or existing['status'] not in ('Needs review','Pending for approval'):
            return page('<h1>Bill cannot be edited</h1><p>This bill has already been approved, paid, or closed.</p><p><a href="/bills">Return to bills</a></p>'),409
        if db.execute('SELECT 1 FROM payment_allocations WHERE bill_id=? LIMIT 1',(bill_id,)).fetchone() or db.execute('SELECT 1 FROM transactions WHERE matched_bill_id=? LIMIT 1',(bill_id,)).fetchone() or existing['transaction_id']:
            return page('<h1>Bill cannot be edited</h1><p>This bill already has bank payment activity. Resolve the match in reconciliation first.</p><p><a href="/bill/'+str(bill_id)+'">Return to bill</a></p>'),409
        choice=request.form.get('supplier_profile_choice','')
        profile=db.execute('SELECT * FROM supplier_profiles WHERE id=?',(int(choice),)).fetchone() if choice.isdigit() else None
        if profile:
            fields.update(supplier=profile['name'],supplier_oib=profile['oib'],supplier_vat_id=profile['vat_id'],supplier_iban=profile['iban'],supplier_address=profile['address'])
        fields['supplier_oib']=normalize_oib(fields['supplier_oib'])
        offer_id=request.form.get('attached_offer_id','').strip()
        if offer_id and (route!='invoice' or not offer_id.isdigit() or int(offer_id) not in {
                row['id'] for row in offer_matches(db,fields['supplier'],fields['supplier_oib'],amount,currency,bill_id)}):
            return page('<h1>Offer link is not available</h1><p>Select a paid offer for the same supplier with a total within ±10%.</p><p><a href="/bill/'+str(bill_id)+'">Return to bill review</a></p>'),400
        duplicate=find_duplicate_bill(db,fields['invoice_number'],fields['supplier'],fields['supplier_oib'],exclude_id=bill_id)
        if duplicate:
            target=_existing_bill_url(duplicate)
            return page(f'<h1>Bill already recorded</h1><p class="notice">The same supplier and invoice number are already on bill #{duplicate["id"]}.</p><p><a href="{target}">Open the existing bill record</a></p>'),409
        supplier_id=get_or_create_supplier(db,fields['supplier'],fields['supplier_address'],fields['supplier_oib'],fields['supplier_vat_id'],fields['supplier_iban'])
        currency=(fields['currency'] or 'EUR').upper()
        status,waiting,plan,_=_route_status(route,amount,currency)
        scheduled=(fields['due_date'] or date.today().isoformat()) if status=='Ready for payment' and plan=='due_date' else ''
        db.execute("UPDATE bills SET supplier=?,invoice_number=?,invoice_date=?,due_date=?,amount=?,currency=?,category=?,cost_center=?,description=?,supplier_address=?,supplier_oib=?,supplier_vat_id=?,supplier_iban=?,vat_amount=?,payment_model=?,payment_reference=?,payment_purpose=?,payment_description=?,supplier_profile_id=?,workflow_route=?,document_type=?,requires_approval=?,validated_by=?,payment_plan=?,scheduled_pay_date=?,decision_reason='',decided_at='',submitter_response='' WHERE id=?",(*[fields[k] for k in ('supplier','invoice_number','invoice_date','due_date')],amount,currency,fields['category'],fields['cost_center'],fields['description'],fields['supplier_address'],fields['supplier_oib'],fields['supplier_vat_id'],fields['supplier_iban'],vat_amount,fields['payment_model'],fields['payment_reference'],fields['payment_purpose'],fields['payment_description'],supplier_id,route,route,1 if waiting else 0,g.user['id'] if waiting else None,plan,scheduled,bill_id))
        if existing['status']!='Needs review' and waiting:
            add_bill_comment(db,bill_id,g.user,'Bill details were edited and resubmitted for approval.')
        action='submitted for approval' if waiting else ('recorded as paid credit card charge' if route=='credit_card_charge' else 'made ready for payment')
        label=DOCUMENT_TYPES[route]
        audit(db,action,'bill',bill_id,f'{label} · {fields["supplier"]} · {amount:.2f} {currency}'+(' · approval required' if waiting else ''))
        set_bill_status(db,bill_id,status,g.user['id'],f'Document type: {label}')
        db.execute('DELETE FROM bill_items WHERE bill_id=?',(bill_id,))
        for index in range(min(int(request.form.get('item_count') or 0),100)):
            description=request.form.get(f'item_description_{index}','').strip()
            category=request.form.get(f'item_category_{index}','').strip()
            try:data=json.loads(request.form.get(f'item_payload_{index}','{}'))
            except (ValueError,TypeError):data={}
            if description:
                db.execute('INSERT INTO bill_items(bill_id,item_index,description,quantity,unit,unit_price,discount,vat_rate,amount,category) VALUES(?,?,?,?,?,?,?,?,?,?)',(bill_id,index,description,float(data.get('quantity') or 0),str(data.get('unit') or ''),float(data.get('unit_price') or 0),str(data.get('discount') or ''),float(data.get('vat_rate') or 0),float(data.get('amount') or 0),category))
        if request.form.get('remember'):
            db.execute("INSERT INTO suppliers(fingerprint,name,category,cost_center,default_currency,updated_at,supplier_profile_id,supplier_oib,supplier_iban,supplier_address) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET name=excluded.name,category=excluded.category,cost_center=excluded.cost_center,default_currency=excluded.default_currency,updated_at=excluded.updated_at,supplier_profile_id=excluded.supplier_profile_id,supplier_oib=excluded.supplier_oib,supplier_iban=excluded.supplier_iban,supplier_address=excluded.supplier_address",(existing['fingerprint'],fields['supplier'],fields['category'],fields['cost_center'],currency,datetime.now().isoformat(timespec='seconds'),supplier_id,fields['supplier_oib'],fields['supplier_iban'],fields['supplier_address']))
        if offer_id:
            attach_invoice_to_paid_offer(db,bill_id,int(offer_id),g.user)
            bill_id=int(offer_id)
    return redirect('/bill/'+str(bill_id))


@bp.post('/manual-bill')
@login_required
@roles_required('submitter','validator','admin')
def manual_create():
    route=request.form.get('document_type','').strip()
    if route not in DOCUMENT_TYPES:
        return manual_bill_page('Choose a document type before you proceed.'),400
    fields={key:request.form.get(key,'').strip() for key in ('supplier','invoice_number','invoice_date','due_date','currency','category','cost_center','supplier_address','supplier_oib','supplier_vat_id','supplier_iban','payment_model','payment_reference','payment_purpose','payment_description','description')}
    try:amount=parse_european_number(request.form.get('amount'))
    except ValueError:amount=0
    item=request.files.get('document')
    allowed={'.pdf','.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp','.txt'}
    if not item or not item.filename or Path(item.filename.replace('\\','/')).suffix.lower() not in allowed:
        return manual_bill_page('Upload a supporting document before proceeding.'),400
    if not fields['supplier'] or not fields['invoice_number'] or amount<=0:
        return manual_bill_page('Enter supplier, invoice number, and a positive amount.'),400
    if fields['category'] not in EXPENSE_CONFIG['categories']:
        return manual_bill_page('Choose a category before proceeding.'),400
    fields['supplier_oib']=normalize_oib(fields['supplier_oib'])
    fields['currency']=(fields['currency'] or 'EUR').upper()
    clean_name,document_bytes=compress_uploaded_document(item.filename,item.read())
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    stored=UPLOAD_DIR/f'{stamp}_{re.sub(r"[^A-Za-z0-9_.-]", "_", clean_name)[:100]}'
    offer_id=request.form.get('attached_offer_id','').strip()
    with connect() as db:
        if offer_id and (route!='invoice' or not offer_id.isdigit() or int(offer_id) not in {
                row['id'] for row in offer_matches(db,fields['supplier'],fields['supplier_oib'],amount,fields['currency'])}):
            stored.unlink(missing_ok=True)
            return manual_bill_page('That offer is not a paid offer for this supplier within the ±10% amount range.'),400
        duplicate=find_duplicate_bill(db,fields['invoice_number'],fields['supplier'],fields['supplier_oib'])
        if duplicate:
            target=_existing_bill_url(duplicate)
            return page(f'<h1>Bill already recorded</h1><p class="notice">The same supplier and invoice number are already on bill #{duplicate["id"]}.</p><p><a href="{target}">Open the existing bill record</a></p>'),409
        stored.write_bytes(document_bytes)
        supplier_id=get_or_create_supplier(db,fields['supplier'],fields['supplier_address'],fields['supplier_oib'],'',fields['supplier_iban'])
        status,waiting,plan,_=_route_status(route,amount,fields['currency'])
        scheduled=(fields['due_date'] or date.today().isoformat()) if status=='Ready for payment' and plan=='due_date' else ''
        now=local_timestamp()
        try:vat_amount=parse_european_number(request.form.get('vat_amount'))
        except ValueError:vat_amount=0
        cursor=db.execute("INSERT INTO bills(supplier,invoice_number,invoice_date,due_date,amount,currency,category,cost_center,description,original_name,stored_path,status,created_at,extraction_method,supplier_address,supplier_oib,supplier_vat_id,supplier_iban,vat_amount,payment_model,payment_reference,payment_purpose,payment_description,supplier_profile_id,created_by,workflow_route,document_type,requires_approval,payment_plan,scheduled_pay_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(fields['supplier'],fields['invoice_number'],fields['invoice_date'],fields['due_date'],amount,fields['currency'],fields['category'],fields['cost_center'],fields['description'],clean_name,str(stored),status,now,'Manual entry',fields['supplier_address'],fields['supplier_oib'],fields['supplier_vat_id'],fields['supplier_iban'],vat_amount,fields['payment_model'],fields['payment_reference'],fields['payment_purpose'],fields['payment_description'],supplier_id,g.user['id'],route,route,1 if waiting else 0,plan,scheduled))
        bill_id=cursor.lastrowid
        record_initial_status(db,bill_id,status,g.user['id'],f'Document type: {DOCUMENT_TYPES[route]}')
        audit(db,'submitted for approval' if waiting else ('recorded as paid credit card charge' if route=='credit_card_charge' else 'made ready for payment'),'bill',bill_id,f'Manual entry · {DOCUMENT_TYPES[route]} · {fields["supplier"]} · {amount:.2f} {fields["currency"]}')
        if offer_id:
            attach_invoice_to_paid_offer(db,bill_id,int(offer_id),g.user)
            bill_id=int(offer_id)
    return redirect('/bill/'+str(bill_id))
