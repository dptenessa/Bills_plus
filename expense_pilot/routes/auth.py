import hashlib
import hmac
import secrets
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from flask import Blueprint, g, make_response, redirect, request, url_for
from ..web import render_page
from ..core import (REQUEST_CONTEXT, audit, connect, invite_accept_page, login_page,
                    page, password_record, setup_page, users_page)
from ..security import login_required, roles_required

bp=Blueprint('auth',__name__)


def _set_session(response,token):
    response.set_cookie('expense_session',token,max_age=12*60*60,httponly=True,samesite='Strict',path='/')
    return response


@bp.get('/setup')
def setup_get():
    if g.user_count:
        return redirect('/login')
    response=make_response(setup_page())
    # Do not bind first-run setup to a browser cookie: local applications are
    # commonly opened through different hostnames (localhost, 127.0.0.1, LAN IP).
    # Same-origin validation on POST protects the one-time admin creation.
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']='no-cache'
    response.headers['Expires']='0'
    return response


@bp.post('/setup')
def setup_post():
    if g.user_count:
        return redirect('/login')
    source=request.headers.get('Origin') or request.headers.get('Referer')
    parsed=urlsplit(source) if source else None
    if not parsed or parsed.scheme not in ('http','https') or parsed.netloc.lower()!=request.host.lower():
        return page('<h1>Setup request rejected</h1><p class="notice">For security, create the administrator from the same address used to open this setup page. Reload it and try again.</p><p><a href="/setup">Reload setup</a></p>'),403
    username=request.form.get('username','').strip()
    password=request.form.get('password','')
    if len(username)<2 or len(username)>80 or len(password)<12 or password!=request.form.get('confirm',''):
        return setup_page('Use a username with 2 to 80 characters and matching passwords of at least 12 characters.',username),400
    digest,salt=password_record(password)
    try:
        with connect() as db:
            if db.execute('SELECT COUNT(*) n FROM users').fetchone()['n']:
                return redirect('/login')
            cur=db.execute('INSERT INTO users(username,password_hash,salt,role,created_at) VALUES(?,?,?,\'admin\',?)',(username,digest,salt,datetime.now().isoformat(timespec='seconds')))
            REQUEST_CONTEXT.user={'id':cur.lastrowid,'username':username,'role':'admin'}
            audit(db,'created','initial administrator',username)
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return setup_page('That username is already in use. Choose another one.',username),400
        raise
    response=make_response(redirect('/login'))
    return response


@bp.get('/login')
def login_get():
    if not g.user_count:
        return login_page('No administrator exists yet. Create the first account to begin.',setup_available=True)
    return login_page('Password updated. Sign in with your new password.' if request.args.get('password_changed') == '1' else '')


@bp.post('/login')
def login_post():
    if not g.user_count:
        return login_page('No administrator exists yet. Create the first account to begin.',setup_available=True),400
    username=request.form.get('username','').strip()
    with connect() as db:
        user=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1',(username,)).fetchone()
    if not user:
        return login_page('Username or password was not recognized.'),401
    digest,_=password_record(request.form.get('password',''),user['salt'])
    if not hmac.compare_digest(digest,user['password_hash']):
        return login_page('Username or password was not recognized.'),401
    token=secrets.token_urlsafe(32)
    csrf=secrets.token_urlsafe(24)
    expires=(datetime.now()+timedelta(hours=12)).isoformat(timespec='seconds')
    with connect() as db:
        db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,csrf_token) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],expires,csrf))
        REQUEST_CONTEXT.user=dict(user)
        audit(db,'signed in','session',user['id'])
    response=make_response(redirect('/'))
    response.set_cookie('expense_session',token,max_age=12*60*60,httponly=True,samesite='Strict',path='/')
    return response


@bp.post('/logout')
@login_required
def logout():
    token=request.cookies.get('expense_session','')
    with connect() as db:
        db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
        audit(db,'signed out','session',g.user['id'])
    response=make_response(redirect('/login'))
    response.set_cookie('expense_session','',max_age=0,httponly=True,samesite='Strict',path='/')
    return response


@bp.get('/accept-invite')
def accept_invite_get():
    if g.user:
        return page('<h1>Already signed in</h1><p>Sign out first, then open the invitation link again.</p>')
    invite_token=request.args.get('token','')
    with connect() as db:
        invite=db.execute("SELECT role FROM invitations WHERE token_hash=? AND accepted_at='' AND revoked_at='' AND expires_at>?",(hashlib.sha256(invite_token.encode()).hexdigest(),datetime.now().isoformat(timespec='seconds'))).fetchone() if invite_token else None
    if not invite:
        return page('<h1>Invitation unavailable</h1><p class="sub">This link has expired, was already accepted, or was revoked. Ask the administrator for a new invitation.</p>'),404
    csrf=secrets.token_urlsafe(24)
    REQUEST_CONTEXT.csrf=csrf
    response=make_response(invite_accept_page(invite_token,invite['role']))
    response.set_cookie('expense_invite',csrf,max_age=600,httponly=True,samesite='Strict',path='/')
    return response


@bp.post('/accept-invite')
def accept_invite_post():
    token_cookie=request.cookies.get('expense_invite','')
    invite_token=request.form.get('invite_token','')
    if not token_cookie or not hmac.compare_digest(token_cookie,request.form.get('csrf_token','')):
        return page('<h1>Invitation session expired</h1><p>Open the invitation link again and retry.</p>'),403
    username=request.form.get('username','').strip()
    password=request.form.get('password','')
    with connect() as db:
        invite=db.execute("SELECT * FROM invitations WHERE token_hash=? AND accepted_at='' AND revoked_at='' AND expires_at>?",(hashlib.sha256(invite_token.encode()).hexdigest(),datetime.now().isoformat(timespec='seconds'))).fetchone()
    if not invite:
        return page('<h1>Invitation unavailable</h1><p>It has expired, was accepted, or was revoked.</p>'),404
    if len(username)<2 or len(username)>80 or len(password)<12 or password!=request.form.get('confirm',''):
        REQUEST_CONTEXT.csrf=token_cookie
        return invite_accept_page(invite_token,invite['role'],'Choose a username of 2–80 characters and matching passwords of at least 12 characters.'),400
    digest,salt=password_record(password)
    session_token=secrets.token_urlsafe(32)
    csrf=secrets.token_urlsafe(24)
    expires=(datetime.now()+timedelta(hours=12)).isoformat(timespec='seconds')
    try:
        with connect() as db:
            invite=db.execute("SELECT * FROM invitations WHERE token_hash=? AND accepted_at='' AND revoked_at='' AND expires_at>?",(hashlib.sha256(invite_token.encode()).hexdigest(),datetime.now().isoformat(timespec='seconds'))).fetchone()
            if not invite:
                return page('<h1>Invitation unavailable</h1><p>It has expired, was accepted, or was revoked.</p>'),404
            created=db.execute('INSERT INTO users(username,password_hash,salt,role,created_at) VALUES(?,?,?,?,?)',(username,digest,salt,invite['role'],datetime.now().isoformat(timespec='seconds')))
            user_id=created.lastrowid
            changed=db.execute("UPDATE invitations SET accepted_at=?,accepted_user_id=? WHERE id=? AND accepted_at='' AND revoked_at=''",(datetime.now().isoformat(timespec='seconds'),user_id,invite['id']))
            if changed.rowcount!=1:raise ValueError('Invitation was already used')
            REQUEST_CONTEXT.user={'id':user_id,'username':username,'role':invite['role']}
            audit(db,'accepted invitation','user',user_id,f'{invite["role"]}; invited by user #{invite["invited_by"] or "unknown"}')
            db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,csrf_token) VALUES(?,?,?,?)',(hashlib.sha256(session_token.encode()).hexdigest(),user_id,expires,csrf))
    except Exception as exc:
        if isinstance(exc,ValueError):
            return page('<h1>Invitation already used</h1><p>Ask an administrator for a new invitation.</p>'),409
        if 'UNIQUE constraint failed: users.username' in str(exc):
            REQUEST_CONTEXT.csrf=token_cookie
            return invite_accept_page(invite_token,invite['role'],'That username is already in use.'),400
        raise
    response=make_response(redirect('/'))
    response.set_cookie('expense_session',session_token,max_age=12*60*60,httponly=True,samesite='Strict',path='/')
    response.set_cookie('expense_invite','',max_age=0,httponly=True,samesite='Strict',path='/')
    return response


@bp.get('/users')
@login_required
@roles_required('admin')
def users_get():
    return users_page()


@bp.post('/users')
@login_required
@roles_required('admin')
def users_post():
    username=request.form.get('username','').strip()
    password=request.form.get('password','')
    role=request.form.get('role','')
    if len(username)<2 or len(password)<12 or role not in ('submitter','validator','admin'):
        return page('<h1>User not added</h1><p class="notice">Enter a username with at least two characters, a password of 12 characters or more, and a valid role.</p><p><a href="/users">Return to user settings</a></p>'),400
    digest,salt=password_record(password)
    try:
        with connect() as db:
            cur=db.execute('INSERT INTO users(username,password_hash,salt,role,created_at) VALUES(?,?,?,?,?)',(username,digest,salt,role,datetime.now().isoformat(timespec='seconds')))
            audit(db,'created user','user',cur.lastrowid,f'{username} · {role}')
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return page('<h1>User not added</h1><p class="notice">That username is already in use.</p><p><a href="/users">Return to user settings</a></p>'),400
        raise
    return redirect('/users')


@bp.post('/users/manage')
@login_required
@roles_required('admin')
def users_manage():
    try:
        user_id=int(request.form.get('user_id','0'))
    except ValueError:
        user_id=0
    role=request.form.get('role','')
    active=request.form.get('active')
    if role not in ('submitter','validator','admin') or active not in (None,'0','1'):
        return users_page(message='Choose a valid role and user status.'),400
    with connect() as db:
        target=db.execute('SELECT id,username,role,active FROM users WHERE id=?',(user_id,)).fetchone()
        if not target:
            return users_page(message='That user could not be found.'),404
        next_active=target['active'] if active is None else int(active)
        if user_id==g.user['id'] and (next_active==0 or role!='admin'):
            return users_page(message='You cannot disable your own account or remove your own administrator role.'),400
        if target['active'] and target['role']=='admin' and (next_active==0 or role!='admin'):
            other_admins=db.execute("SELECT COUNT(*) n FROM users WHERE active=1 AND role='admin' AND id<>?",(user_id,)).fetchone()['n']
            if not other_admins:
                return users_page(message='The last active administrator cannot be disabled or demoted.'),400
        db.execute('UPDATE users SET role=?,active=? WHERE id=?',(role,next_active,user_id))
        if not next_active or role!=target['role']:
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
            db.execute('DELETE FROM password_resets WHERE user_id=?',(user_id,))
        details=f'{target["username"]}: {target["role"]} → {role}; {"active" if next_active else "disabled"}'
        audit(db,'updated user','user',user_id,details)
    return users_page(message=f'Updated {target["username"]}.')


@bp.post('/users/reset-password')
@login_required
@roles_required('admin')
def users_reset_password():
    try:
        user_id=int(request.form.get('user_id','0'))
    except ValueError:
        user_id=0
    password=request.form.get('password','')
    if len(password)<12:
        return users_page(message='Temporary passwords must be at least 12 characters.'),400
    if user_id==g.user['id']:
        return users_page(message='Ask another administrator to reset your password so you do not lock yourself out.'),400
    digest,salt=password_record(password)
    with connect() as db:
        target=db.execute('SELECT id,username FROM users WHERE id=?',(user_id,)).fetchone()
        if not target:
            return users_page(message='That user could not be found.'),404
        db.execute('UPDATE users SET password_hash=?,salt=? WHERE id=?',(digest,salt,user_id))
        db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        db.execute('DELETE FROM password_resets WHERE user_id=?',(user_id,))
        audit(db,'reset password','user',user_id,'Admin-set temporary password; active sessions revoked')
    return users_page(message=f'Password updated for {target["username"]}; they must sign in again.')


def _password_page(mode, message='', reset_token=''):
    titles = {'forgot': 'Forgot password', 'change': 'Change password', 'reset': 'Reset password'}
    response = make_response(render_page('password.html', title=titles[mode], mode=mode,
                                         message=message, reset_token=reset_token))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@bp.get('/forgot-password')
def forgot_password():
    return _password_page('forgot')


@bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'GET':
        return _password_page('change')
    password = request.form.get('password', '')
    if not 12 <= len(password) <= 1024 or password != request.form.get('confirm', ''):
        return _password_page('change', 'Enter matching passwords of 12–1024 characters.'), 400
    with connect() as db:
        user = db.execute('SELECT * FROM users WHERE id=? AND active=1', (g.user['id'],)).fetchone()
        if not user:
            return redirect('/login')
        current, _ = password_record(request.form.get('current_password', ''), user['salt'])
        if not hmac.compare_digest(current, user['password_hash']):
            return _password_page('change', 'Current password is incorrect.'), 400
        digest, salt = password_record(password)
        # Only replace the password that was just verified, even if another
        # session changes it between verification and this update.
        changed = db.execute('UPDATE users SET password_hash=?,salt=? WHERE id=? AND password_hash=?',
                             (digest, salt, user['id'], user['password_hash']))
        if changed.rowcount != 1:
            return _password_page('change', 'Your password changed in another session. Sign in again.'), 409
        db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        db.execute('DELETE FROM password_resets WHERE user_id=?', (user['id'],))
        audit(db, 'changed own password', 'user', user['id'], 'All sessions revoked')
    response = make_response(redirect('/login?password_changed=1', code=303))
    response.delete_cookie('expense_session', path='/')
    return response


@bp.post('/users/password-reset-link')
@login_required
@roles_required('admin')
def create_password_reset_link():
    try:
        user_id = int(request.form.get('user_id', '0'))
    except ValueError:
        user_id = 0
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        target = db.execute('SELECT id FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
        if not target:
            return users_page(message='Choose an active user.'), 400
        db.execute('DELETE FROM password_resets WHERE user_id=?', (user_id,))
        db.execute('INSERT INTO password_resets(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, now.isoformat(timespec='seconds'),
                    (now + timedelta(minutes=30)).isoformat(timespec='seconds')))
        audit(db, 'created password reset link', 'user', user_id, 'Single use; expires in 30 minutes')
    response = make_response(users_page(reset_url=url_for('auth.reset_password', token=token, _external=True)))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    token = request.args.get('token', '') if request.method == 'GET' else request.form.get('reset_token', '')
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now().isoformat(timespec='seconds')
    if request.method == 'POST':
        cookie = request.cookies.get('expense_reset', '')
        if not cookie or not hmac.compare_digest(cookie, request.form.get('csrf_token', '')):
            return page('<h1>Reset session expired</h1><p>Open your reset link again and retry.</p>'), 403
    with connect() as db:
        if request.method == 'POST':
            db.execute('BEGIN IMMEDIATE')
        reset = db.execute('''SELECT r.user_id FROM password_resets r JOIN users u ON u.id=r.user_id
            WHERE r.token_hash=? AND r.used_at='' AND r.expires_at>? AND u.active=1''', (token_hash, now)).fetchone()
        if not reset:
            return page('<h1>Reset link unavailable</h1><p>This link expired, was already used, or was replaced. Ask an administrator for a new link.</p>'), 404
        if request.method == 'GET':
            csrf = secrets.token_urlsafe(24)
            REQUEST_CONTEXT.csrf = csrf
            response = _password_page('reset', reset_token=token)
            response.set_cookie('expense_reset', csrf, max_age=1800, httponly=True,
                                secure=request.is_secure, samesite='Strict', path='/reset-password')
            return response
        password = request.form.get('password', '')
        if not 12 <= len(password) <= 1024 or password != request.form.get('confirm', ''):
            REQUEST_CONTEXT.csrf = cookie
            return _password_page('reset', 'Enter matching passwords of 12–1024 characters.', token), 400
        digest, salt = password_record(password)
        db.execute('UPDATE users SET password_hash=?,salt=? WHERE id=?', (digest, salt, reset['user_id']))
        db.execute('UPDATE password_resets SET used_at=? WHERE token_hash=?', (now, token_hash))
        db.execute('DELETE FROM sessions WHERE user_id=?', (reset['user_id'],))
        audit(db, 'completed password reset', 'user', reset['user_id'], 'All sessions revoked')
    response = make_response(redirect('/login?password_changed=1', code=303))
    response.delete_cookie('expense_reset', path='/reset-password')
    response.delete_cookie('expense_session', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.post('/invites')
@login_required
@roles_required('admin')
def invite_create():
    role=request.form.get('role','')
    if role not in ('submitter','validator','admin'):
        return page('<h1>Invite not created</h1><p>Choose a valid role.</p>'),400
    token=secrets.token_urlsafe(32)
    now=datetime.now()
    expires=now+timedelta(days=7)
    with connect() as db:
        cur=db.execute('INSERT INTO invitations(token_hash,role,invited_by,created_at,expires_at) VALUES(?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),role,g.user['id'],now.isoformat(timespec='seconds'),expires.isoformat(timespec='seconds')))
        audit(db,'created invitation','invitation',cur.lastrowid,role)
    host=request.host
    if not all(c.isalnum() or c in '.-:[]' for c in host):host='127.0.0.1:5000'
    invite_url=f'http://{host}/accept-invite?token={token}'
    return users_page(invite_url)


@bp.post('/invites/revoke')
@login_required
@roles_required('admin')
def invite_revoke():
    try:invite_id=int(request.form.get('invite_id','0'))
    except ValueError:invite_id=0
    with connect() as db:
        updated=db.execute("UPDATE invitations SET revoked_at=? WHERE id=? AND accepted_at='' AND revoked_at=''",(datetime.now().isoformat(timespec='seconds'),invite_id))
        if updated.rowcount:audit(db,'revoked invitation','invitation',invite_id,'')
    return redirect('/users')


@bp.get('/audit')
@login_required
@roles_required('admin')
def audit_get():
    from ..core import audit_page
    return audit_page()
