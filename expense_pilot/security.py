from functools import wraps
from flask import g, redirect


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, 'user', None):
            return redirect('/login')
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user=getattr(g, 'user', None)
            if not user:
                return redirect('/login')
            if user['role'] not in roles:
                from .core import page
                return page('<h1>Access denied</h1>'),403
            return view(*args, **kwargs)
        return wrapped
    return decorate
