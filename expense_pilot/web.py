"""Web for the expense pilot."""
from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, date
from flask import render_template
from markupsafe import Markup
from .request_context import REQUEST_CONTEXT
from .database import connect

def european_amount(value):
    """Format a number using Croatian/European separators."""
    try:
        formatted = f'{float(value):,.2f}'
    except (TypeError, ValueError):
        return str(value or '')
    return formatted.replace(',', '\0').replace('.', ',').replace('\0', '.')

def european_date(value):
    """Display ISO dates in the Croatian day/month/year order."""
    try:
        return date.fromisoformat(str(value)[:10]).strftime('%d.%m.%Y.') if value else ''
    except (TypeError, ValueError):
        return str(value or '')

def parse_european_number(value):
    """Parse numeric form values accepting European grouping and decimals."""
    text = str(value or '').strip().replace(' ', '')
    if not text:
        return 0.0
    if ',' in text:
        text = text.replace('.', '').replace(',', '.')
    return float(text)

# Page rendering and shared web helpers. Notification data is loaded lazily so
# comments can use the database without creating an import cycle.

def esc(s): return html.escape(str(s or ""), quote=True)


def password_record(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()
    return digest, salt


def audit(db, action, entity_type, entity_id='', details=''):
    user = getattr(REQUEST_CONTEXT, 'user', None)
    db.execute('INSERT INTO audit_log(occurred_at,user_id,username,action,entity_type,entity_id,details) VALUES(?,?,?,?,?,?,?)',
        (datetime.now().isoformat(timespec='seconds'), user['id'] if user else None,
         user['username'] if user else 'system', action, entity_type, str(entity_id), str(details)[:500]))


def approval_threshold(db):
    return 150.0


def csrf_input():
    token = getattr(REQUEST_CONTEXT, 'csrf', '')
    return Markup(f'<input type="hidden" name="csrf_token" value="{esc(token)}">') if token else Markup('')


def render_page(template_name, title="Expense pilot", **context):
    user=getattr(REQUEST_CONTEXT,'user',None)
    if user:
        from .comments import notification_counts
        counts=notification_counts(user)
    else:
        counts={'actions':0,'notes':0}
    nav_items=[]
    if user:
        role=user['role']
        # Operational views are shared by role; only user administration and
        # the audit log are restricted to administrators.
        nav_items=[
            {'href':'/','label':'Overview'},
            {'href':'/manual-bill','label':'Enter bill'},
            {'href':'/approvals','label':'Validator inbox','badge':counts['actions']},
            {'href':'/payer','label':'Payer dashboard'},
            {'href':'/reconcile','label':'Bank reconciliation'},
            {'href':'/bills','label':'Bills'},
            {'href':'/suppliers','label':'Suppliers'},
            {'href':'/budget','label':'Budget alignment'},
            {'href':'/report','label':'Reports'},
            {'href':'/archive','label':'Archive'},
            {'href':'/wall','label':'Wall','badge':counts['notes']},
        ]
        if role=='admin':
            nav_items.extend([{'separator':True},{'href':'/users','label':'Users & invites'},
                              {'href':'/audit','label':'Audit log'}])
    return render_template(template_name,title=title,current_user=user,nav_items=nav_items,
                           european_amount=european_amount,european_date=european_date,
                           csrf_field=csrf_input(),**context)


def page(content, title="Expense pilot"):
    return render_page('raw_page.html',title=title,content=Markup(content))


def setup_page(message='', username=''):
    return render_page('setup.html',title='Set up the expense pilot',message=message,username=username)


def login_page(message='', setup_available=False):
    return render_page('login.html',title='Sign in',message=message,setup_available=setup_available)


def users_page(invite_url='', message='', reset_url=''):
    with connect() as db:
        users=db.execute('SELECT id,username,role,active,created_at FROM users ORDER BY username COLLATE NOCASE').fetchall()
        invitations=db.execute("SELECT i.id,i.role,i.created_at,i.expires_at,u.username inviter FROM invitations i LEFT JOIN users u ON u.id=i.invited_by WHERE i.accepted_at='' AND i.revoked_at='' AND i.expires_at>? ORDER BY i.id DESC",(datetime.now().isoformat(timespec='seconds'),)).fetchall()
    return render_page('users.html',title='Users and approval settings',users=users,
                       invitations=invitations,invite_url=invite_url,message=message,reset_url=reset_url)


def invite_accept_page(token,role,message=''):
    return render_page('invite_accept.html',title='Join the expense pilot',
                       invite_token=token,role=role,message=message)

