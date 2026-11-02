"""Supplier Services for the expense pilot."""
from __future__ import annotations

import re
from datetime import datetime
from .identifiers import normalize_oib

def get_or_create_supplier(db,name,address='',oib='',vat_id='',iban=''):
    name=str(name or '').strip() or 'Supplier not identified'
    normalized=re.sub(r'\W+','',name.casefold())
    oib=normalize_oib(oib)
    vat_id=str(vat_id or '').strip().upper()
    iban=re.sub(r'\s+','',str(iban or '')).upper()
    existing=None
    for column,value in [('oib',oib),('vat_id',vat_id),('iban',iban)]:
        if value:
            existing=db.execute(f'SELECT * FROM supplier_profiles WHERE {column}=? LIMIT 1',(value,)).fetchone()
            if existing:break
    if not existing:existing=db.execute('SELECT * FROM supplier_profiles WHERE normalized_name=?',(normalized,)).fetchone()
    if existing:
        db.execute('UPDATE supplier_profiles SET name=CASE WHEN ?<>"" THEN ? ELSE name END,normalized_name=CASE WHEN ?<>"" THEN ? ELSE normalized_name END,address=CASE WHEN address="" THEN ? ELSE address END,oib=CASE WHEN ?<>"" THEN ? ELSE oib END,vat_id=CASE WHEN vat_id="" THEN ? ELSE vat_id END,iban=CASE WHEN iban="" THEN ? ELSE iban END WHERE id=?',(name,name,name,normalized,address,oib,oib,vat_id,iban,existing['id']))
        return existing['id']
    cur=db.execute('INSERT INTO supplier_profiles(name,normalized_name,address,oib,vat_id,iban,created_at) VALUES(?,?,?,?,?,?,?)',(name,normalized,address,oib,vat_id,iban,datetime.now().isoformat(timespec='seconds')))
    return cur.lastrowid

