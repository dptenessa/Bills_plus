"""Approval inbox views."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .database import connect
from .web import render_page
from .comments import unread_bill_comment_count, bill_discussion_data
from .workflow import PENDING, DOCUMENT_TYPES


def approvals_page(user):
    with connect() as db:
        bills=db.execute("SELECT b.*,u.username creator FROM bills b LEFT JOIN users u ON u.id=b.created_by WHERE b.status=? ORDER BY b.due_date,b.created_at",(PENDING,)).fetchall()
    cards=[]
    for row in bills:
        bill=dict(row)
        suffix=Path(bill.get('original_name') or '').suffix.lower()
        preview_kind='pdf' if suffix=='.pdf' else 'image' if suffix in ('.png','.jpg','.jpeg','.gif','.webp','.bmp') else 'other'
        bill['has_document']=bool(bill.get('stored_path') and Path(bill['stored_path']).is_file())
        discussion=bill_discussion_data(bill['id'],f'/approvals#bill-{bill["id"]}',user,mark_read=True)
        discussion['comments'].sort(key=lambda item:item.get('created_at') or '')
        cards.append({'bill':bill,'reason':DOCUMENT_TYPES.get(bill['document_type'] or bill['workflow_route'],'Invoice'),
                      'can_decide':bill['created_by']!=user['id'],'preview_kind':preview_kind,
                      'unread':unread_bill_comment_count(db,user['id'],bill['id']),
                      'discussion':discussion})
    return render_page('approvals.html',title='Validator inbox',cards=cards,today=date.today().isoformat())


def audit_page():
    with connect() as db: rows=db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 300').fetchall()
    return render_page('audit.html',title='Audit log',rows=rows)
