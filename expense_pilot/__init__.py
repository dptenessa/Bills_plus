import hashlib
import hmac
from datetime import datetime
from flask import Flask, g, redirect, request
from .core import REQUEST_CONTEXT, connect, init_db, page


def create_app():
    app=Flask(__name__,template_folder='../templates',static_folder='../static')
    app.config['SECRET_KEY']='local-pilot-session-cookie-not-used-for-auth'
    app.config['MAX_CONTENT_LENGTH']=100*1024*1024
    init_db()

    @app.before_request
    def load_user():
        token=request.cookies.get('expense_session','')
        user=None
        count=0
        csrf=''
        with connect() as db:
            count=db.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
            if token:
                row=db.execute('SELECT s.csrf_token,u.id,u.username,u.role,u.active FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',(hashlib.sha256(token.encode()).hexdigest(),datetime.now().isoformat(timespec='seconds'))).fetchone()
                if row and row['active']:
                    user=dict(row)
                    csrf=row['csrf_token']
        g.user=user
        g.user_count=count
        REQUEST_CONTEXT.user=user
        REQUEST_CONTEXT.csrf=csrf
        if request.method=='POST' and user and request.path not in ('/accept-invite','/setup','/login','/reset-password'):
            submitted=request.form.get('csrf_token','')
            if not csrf or not hmac.compare_digest(csrf,submitted):
                return page('<h1>Request expired</h1><p>Please refresh the page and try again.</p>'),403
        # Flask serves /static/... through this same before_request hook. Let
        # those requests through before redirecting a first-time user to setup.
        if count==0 and request.path not in ('/setup','/login','/accept-invite') and request.endpoint != 'static':
            return redirect('/setup')
        return None

    @app.errorhandler(413)
    def too_large(_error):
        return page('<h1>Upload is too large</h1><p>Please upload bills in smaller batches.</p>'),413

    from .routes.auth import bp as auth_bp
    from .routes.bills import bp as bills_bp
    from .routes.approvals import bp as approvals_bp
    from .routes.bank import bp as bank_bp
    from .routes.reports import bp as reports_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(bills_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(bank_bp)
    app.register_blueprint(reports_bp)
    return app
