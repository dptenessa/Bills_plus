"""Cross-bill discussion wall for submitters, validators, and administrators."""
from __future__ import annotations

from flask import make_response
from .database import connect
from .web import render_page


def bill_wall_page(user):
    where=''
    params=[]
    with connect() as db:
        rows=db.execute(f'''SELECT c.id,c.bill_id,c.author_id,c.author_name,c.message,c.created_at,
                b.supplier,b.invoice_number,b.status,
                CASE WHEN r.comment_id IS NULL AND c.author_id IS NOT NULL AND c.author_id<>? THEN 1 ELSE 0 END AS unread
            FROM bill_comments c
            JOIN bills b ON b.id=c.bill_id
            LEFT JOIN bill_comment_reads r ON r.comment_id=c.id AND r.user_id=?
            {where}
            ORDER BY c.created_at DESC,c.id DESC''',(user['id'],user['id'],*params)).fetchall()
    messages=[dict(row) for row in rows]
    response=make_response(render_page('wall.html',title='Wall',messages=messages))
    # The wall is an unread/read inbox. Avoid browser back-forward caching so
    # returning from a bill detail refreshes the per-user read state.
    response.headers['Cache-Control']='no-store, max-age=0'
    return response
