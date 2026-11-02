"""Identifiers for the expense pilot."""
from __future__ import annotations

import re

def normalize_oib(value):
    return re.sub(r"\D", "", str(value or ""))


def normalize_invoice_number(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def supplier_identity(name, oib=""):
    normalized_oib=normalize_oib(oib)
    if normalized_oib:return "oib:"+normalized_oib
    return "name:"+re.sub(r"\W+","",str(name or "").casefold())


def find_duplicate_bill(db, invoice_number, supplier, oib="", exclude_id=None):
    normalized_number=normalize_invoice_number(invoice_number)
    if not normalized_number:return None
    # A cancelled/void row still represents a recorded invoice. It remains a
    # duplicate until the user permanently deletes that bill track.
    query="SELECT * FROM bills WHERE invoice_number<>''"
    params=[]
    if exclude_id is not None:
        query+=" AND id<>?";params.append(exclude_id)
    for bill in db.execute(query,params).fetchall():
        if normalize_invoice_number(bill['invoice_number'])!=normalized_number:continue
        identity_match=supplier_identity(bill['supplier'],bill['supplier_oib'])==supplier_identity(supplier,oib)
        same_name=(not normalize_oib(bill['supplier_oib']) or not normalize_oib(oib)) and re.sub(r"\W+","",str(bill['supplier'] or '').casefold())==re.sub(r"\W+","",str(supplier or '').casefold())
        if identity_match or same_name:
            return bill
    return None



def normalized_reference(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())
