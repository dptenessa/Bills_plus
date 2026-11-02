"""Comments for the expense pilot."""
from __future__ import annotations

from datetime import datetime
from .request_context import REQUEST_CONTEXT
from .database import connect

def add_bill_comment(db,bill_id,user,message):
    message=str(message or '').strip()[:4000]
    if not message:return False
    db.execute('INSERT INTO bill_comments(bill_id,author_id,author_name,message,created_at) VALUES(?,?,?,?,?)',
        (bill_id,user['id'] if user else None,user['username'] if user else 'System',message,datetime.now().isoformat(timespec='seconds')))
    return True


def unread_bill_comment_count(db,user_id,bill_id=None):
    sql='''SELECT COUNT(*) n FROM bill_comments c
        LEFT JOIN bill_comment_reads r ON r.comment_id=c.id AND r.user_id=?
        WHERE c.author_id IS NOT NULL AND c.author_id<>? AND r.comment_id IS NULL'''
    params=[user_id,user_id]
    if bill_id is not None:
        sql+=' AND c.bill_id=?';params.append(bill_id)
    return db.execute(sql,params).fetchone()['n']


def mark_bill_comments_read(db,bill_id,user):
    if not user:return
    now=datetime.now().isoformat(timespec='seconds')
    db.execute('''INSERT OR IGNORE INTO bill_comment_reads(comment_id,user_id,read_at)
        SELECT id,?,? FROM bill_comments WHERE bill_id=? AND author_id IS NOT NULL AND author_id<>?''',
        (user['id'],now,bill_id,user['id']))


def notification_counts(user):
    if not user:return {'actions':0,'notes':0}
    with connect() as db:
        # The validator inbox badge mirrors the shared inbox view exactly:
        # every pending approval is counted, independent of which user submitted it.
        actions=db.execute("SELECT COUNT(*) n FROM bills WHERE status='Pending for approval'").fetchone()['n']
        notes=unread_bill_comment_count(db,user['id'])
    return {'actions':actions,'notes':notes}


def bill_discussion_data(bill_id,return_to,user=None,mark_read=True):
    with connect() as db:
        comments=[dict(row) for row in db.execute('SELECT id,author_id,author_name,message,created_at FROM bill_comments WHERE bill_id=? ORDER BY id DESC',(bill_id,)).fetchall()]
        bill=db.execute('SELECT decision_reason,submitter_response,decided_at FROM bills WHERE id=?',(bill_id,)).fetchone()
        if mark_read:
            mark_bill_comments_read(db,bill_id,user)
    known={item['message'] for item in comments}
    legacy=[]
    if bill:
        for label,message in (('Previous approver note',bill['decision_reason']),('Previous submitter response',bill['submitter_response'])):
            if message and message not in known:
                legacy.append({'author_name':label,'message':message,'created_at':bill['decided_at']})
    return {'comments':legacy+comments,'bill_id':bill_id,'return_to':return_to}

