from datetime import date, datetime
import re
from flask import Blueprint, g, redirect, request
from ..core import add_bill_comment, approvals_page, audit, bill_wall_page, connect, page, payer_page
from ..security import login_required, roles_required
from ..workflow import PENDING, READY, ORDER_ISSUE, REJECTED, set_bill_status, local_timestamp

bp=Blueprint('approvals',__name__)


@bp.get('/wall')
@login_required
def bill_wall():
    return bill_wall_page(g.user)


@bp.get('/payer')
@login_required
@roles_required('submitter','validator','admin')
def payer_dashboard():
    return payer_page(request.args)


@bp.get('/approvals')
@login_required
@roles_required('submitter','validator','admin')
def inbox():
    return approvals_page(g.user)


@bp.post('/approval')
@login_required
@roles_required('submitter','validator','admin')
def decide():
    try:bill_id=int(request.form.get('bill_id','0'))
    except ValueError:bill_id=0
    decision=request.form.get('decision','')
    reason=request.form.get('reason','').strip()
    return_to=request.form.get('return_to','')
    if return_to!=f'/bill/{bill_id}':return_to='/approvals'
    return_label='the bill' if return_to.startswith('/bill/') else 'the Validator inbox'
    today=date.today().isoformat()
    if decision=='reject' and not reason:
        return page('<h1>A note is required</h1><p>Enter the reason for rejection.</p><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),400
    scheduled=''
    plan=''
    if decision=='pay_now':
        plan='now';scheduled=today;status=READY
    elif decision=='pay_due':
        plan='due_date';status=READY
    elif decision=='pay_date':
        raw_date=request.form.get('pay_date','').strip()
        try:scheduled=date.fromisoformat(raw_date).isoformat()
        except ValueError:return page('<h1>Choose a valid payment date</h1><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),400
        if scheduled<today:return page('<h1>Payment date is in the past</h1><p>Choose a future date or use Pay now.</p><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),400
        plan='date';status=READY
    elif decision=='reject':
        status='Rejected'
    else:
        return page('<h1>Unknown approval decision</h1><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),400
    now=local_timestamp()
    with connect() as db:
        bill=db.execute('SELECT * FROM bills WHERE id=? AND status=?',(bill_id,PENDING)).fetchone()
        if not bill:
            return page('<h1>This bill is no longer waiting for approval</h1><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),409
        if bill['created_by']==g.user['id']:
            return page('<h1>You cannot approve your own bill</h1><p>A different validator must decide.</p><p><a href="'+return_to+'">Return to '+return_label+'</a></p>'),403
        if decision=='pay_due':
            scheduled=bill['due_date']
            if not scheduled:
                return page('<h1>Due date is missing</h1><p>Enter the due date in the bill details or select a specific payment date.</p><p><a href="/bill/'+str(bill_id)+'">Open bill details</a></p>'),400
        db.execute('UPDATE bills SET decision_reason=?,decided_at=?,validated_by=?,payment_plan=?,scheduled_pay_date=? WHERE id=?',(reason,now,g.user['id'],plan,scheduled,bill_id))
        set_bill_status(db,bill_id,status,g.user['id'],reason or f'Approved · {plan}')
        if reason:add_bill_comment(db,bill_id,g.user,reason)
        action={'reject':'rejected','pay_now':'approved for payment now','pay_due':'approved for payment at due date','pay_date':'approved for scheduled payment'}[decision]
        audit(db,action,'bill',bill_id,(reason+' · ' if reason else '')+(scheduled or ''))
    return redirect(return_to)


@bp.post('/bill-comment')
@login_required
def post_bill_comment():
    try:bill_id=int(request.form.get('bill_id','0'))
    except ValueError:bill_id=0
    message=request.form.get('message','').strip()
    target=request.form.get('return_to','').strip()
    if target!='/approvals' and not re.fullmatch(r'/approvals#bill-\d+',target) and not re.fullmatch(r'/bill/\d+',target) and not re.fullmatch(r'/payer\?until=\d{4}-\d{2}-\d{2}&all=[01]&bill=\d+',target):
        target=f'/bill/{bill_id}'
    if not message:
        return page('<h1>Enter a note</h1><p><a href="'+target+'">Return to the bill</a></p>'),400
    with connect() as db:
        bill=db.execute('SELECT id,created_by FROM bills WHERE id=?',(bill_id,)).fetchone()
        if not bill:return page('<h1>Bill not found</h1><p><a href="/payer">Return to bills</a></p>'),404
        add_bill_comment(db,bill_id,g.user,message)
        audit(db,'added bill discussion note','bill',bill_id,message)
    return redirect(target)


@bp.post('/submitter-action')
@login_required
@roles_required('submitter','validator','admin')
def payer_action():
    try:bill_id=int(request.form.get('bill_id','0'))
    except ValueError:bill_id=0
    action=request.form.get('action','')
    now=local_timestamp()
    with connect() as db:
        bill=db.execute('SELECT * FROM bills WHERE id=?',(bill_id,)).fetchone()
        if not bill:
            return page('<h1>Bill not found</h1><p><a href="/payer">Return to the payer dashboard</a></p>'),404
        if action=='paid' and bill['status'] in (READY,ORDER_ISSUE):
            if bill['status']==READY:
                set_bill_status(db,bill_id,ORDER_ISSUE,g.user['id'],'Payment reported; awaiting bank statement confirmation')
            audit(db,'payment reported by submitter','bill',bill_id,f'{bill["amount"]:.2f} {bill["currency"]}; awaiting bank statement confirmation')
        elif action=='payment_issue' and bill['status'] in (READY,ORDER_ISSUE):
            note=request.form.get('note','').strip()
            if not note:return page('<h1>Describe the payment issue</h1><p><a href="/payer?bill='+str(bill_id)+'">Return to the bill</a></p>'),400
            set_bill_status(db,bill_id,ORDER_ISSUE,g.user['id'],note)
            add_bill_comment(db,bill_id,g.user,note)
            audit(db,'reported payment order issue','bill',bill_id,note)
        elif action=='reply' and bill['status']==PENDING:
            note=request.form.get('note','').strip()
            if not note:return page('<h1>Please add an explanation</h1><p><a href="/payer?bill='+str(bill_id)+'">Return to the bill</a></p>'),400
            db.execute("UPDATE bills SET submitter_response=?,decided_at='',validated_by=NULL WHERE id=?",(note,bill_id))
            add_bill_comment(db,bill_id,g.user,note)
            audit(db,'resubmitted bill with explanation','bill',bill_id,note)
        elif action=='solved' and bill['status']=='Rejected':
            db.execute("UPDATE bills SET decided_at=? WHERE id=?",(now,bill_id))
            set_bill_status(db,bill_id,'Solved',g.user['id'],'Rejected issue marked solved by submitter')
            audit(db,'marked rejected bill solved','bill',bill_id,bill['decision_reason'])
    return redirect('/payer')
