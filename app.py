"""Flask entry point for the local EFIZ expense pilot."""
from expense_pilot import create_app

app=create_app()


if __name__=='__main__':
    app.run(host='0.0.0.0' if '--lan' in __import__('sys').argv else '127.0.0.1',port=8000,debug=False)
