"""Bill Ingestion for the expense pilot."""
from __future__ import annotations

import hashlib
import re
import tempfile
from datetime import datetime
from pathlib import Path
from .config import UPLOAD_DIR
from .database import connect
from .document_reader import local_ocr, parse_doc, fingerprint
from .identifiers import normalize_oib, find_duplicate_bill
from .supplier_services import get_or_create_supplier
from .web import audit
from .workflow import record_initial_status

def compress_uploaded_document(filename,data):
    """Reduce stored upload size while keeping scans usable for OCR and payment codes."""
    clean_name=filename.replace('\\','/').rsplit('/',1)[-1]
    suffix=Path(clean_name).suffix.lower()
    if suffix in ('.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp'):
        try:
            from PIL import Image,ImageOps
            from io import BytesIO
            with Image.open(BytesIO(data)) as source:
                image=ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((2200,2200),Image.Resampling.LANCZOS)
                output=BytesIO()
                image.save(output,format='JPEG',quality=82,optimize=True,progressive=True)
                data=output.getvalue()
                clean_name=Path(clean_name).stem+'.jpg'
        except (ImportError,OSError,ValueError):
            pass
    elif suffix=='.pdf':
        try:
            import fitz
            from io import BytesIO
            with fitz.open(stream=data,filetype='pdf') as document:
                output=BytesIO()
                document.save(output,garbage=4,deflate=True,clean=True)
                compressed=output.getvalue()
                if compressed and len(compressed)<len(data):data=compressed
        except (ImportError,RuntimeError,ValueError):
            pass
    return clean_name,data


def upload_one_bill(filename,data,category='',actor_id=None):
    clean_name=filename.replace('\\','/').rsplit('/',1)[-1]
    clean_name,data=compress_uploaded_document(clean_name,data)
    safe=re.sub(r'[^A-Za-z0-9_.-]','_',clean_name)[:100] or 'document'
    # OCR reads from a disposable temp directory. Only a confirmed new bill
    # is copied into the persistent uploads folder below.
    with tempfile.TemporaryDirectory(prefix='expense-ingest-') as temp_dir:
        reading_copy=Path(temp_dir)/safe
        reading_copy.write_bytes(data)
        text,method=local_ocr(reading_copy)
    parsed=parse_doc(text); fp=fingerprint(text,parsed['supplier']); memory_reused=False
    target=None
    try:
        with connect() as db:
            memory=db.execute('SELECT * FROM suppliers WHERE fingerprint=?',(fp,)).fetchone()
            if memory:
                memory_reused=True
                parsed['supplier']=memory['name'] or parsed['supplier']
                parsed['supplier_oib']=memory['supplier_oib'] or parsed['supplier_oib']
                parsed['supplier_iban']=memory['supplier_iban'] or parsed['supplier_iban']
                parsed['supplier_address']=memory['supplier_address'] or parsed['supplier_address']
                if memory['supplier_profile_id']:
                    remembered=db.execute('SELECT * FROM supplier_profiles WHERE id=?',(memory['supplier_profile_id'],)).fetchone()
                    if remembered:
                        parsed['supplier']=remembered['name']
                        parsed['supplier_oib']=remembered['oib'] or parsed['supplier_oib']
                        parsed['supplier_iban']=remembered['iban'] or parsed['supplier_iban']
                        parsed['supplier_address']=remembered['address'] or parsed['supplier_address']
                        parsed['supplier_vat_id']=remembered['vat_id'] or parsed['supplier_vat_id']
                category=memory['category'] or category
                cc=memory['cost_center']; currency=memory['default_currency'] or parsed['currency']
            else:cc=''; currency=parsed['currency']
            registered=db.execute('SELECT * FROM supplier_profiles WHERE oib=?',(normalize_oib(parsed['supplier_oib']),)).fetchone() if normalize_oib(parsed['supplier_oib']) else None
            if registered:
                parsed['supplier']=registered['name']
                parsed['supplier_address']=registered['address'] or parsed['supplier_address']
                parsed['supplier_vat_id']=registered['vat_id'] or parsed['supplier_vat_id']
                parsed['supplier_iban']=registered['iban'] or parsed['supplier_iban']
            duplicate=find_duplicate_bill(db,parsed['invoice_number'],parsed['supplier'],parsed['supplier_oib'])
            if duplicate:
                return {'id':duplicate['id'],'duplicate':True,'existing_supplier':duplicate['supplier'],'existing_invoice':duplicate['invoice_number']}
            supplier_id=get_or_create_supplier(db,parsed['supplier'],parsed['supplier_address'],parsed['supplier_oib'],parsed['supplier_vat_id'],parsed['supplier_iban'])
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            target=UPLOAD_DIR/f'{stamp}_{safe}'
            target.write_bytes(data)
            extraction_note=method+(' · previous supplier profile matched' if memory_reused else '')
            cur=db.execute("INSERT INTO bills(supplier,invoice_number,invoice_date,due_date,amount,currency,category,cost_center,fingerprint,original_name,stored_path,raw_text,status,created_at,extraction_method,supplier_address,supplier_oib,supplier_vat_id,supplier_iban,vat_amount,payment_model,payment_reference,payment_purpose,payment_description,supplier_profile_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(parsed['supplier'],parsed['invoice_number'],parsed['invoice_date'],parsed['due_date'],parsed['amount'],currency,category,cc,fp,clean_name,str(target),text,'Needs review',datetime.now().isoformat(timespec='seconds'),extraction_note,parsed['supplier_address'],parsed['supplier_oib'],parsed['supplier_vat_id'],parsed['supplier_iban'],parsed['vat_amount'],parsed['payment_model'],parsed['payment_reference'],parsed['payment_purpose'],parsed['payment_description'],supplier_id,actor_id))
            bid=cur.lastrowid
            record_initial_status(db,bid,'Needs review',actor_id,'Document uploaded; processing type not selected')
            audit(db,'uploaded','bill',bid,clean_name)
            for ix,item in enumerate(parsed['line_items']):
                db.execute('INSERT INTO bill_items(bill_id,item_index,description,quantity,unit,unit_price,discount,vat_rate,amount,category) VALUES(?,?,?,?,?,?,?,?,?,?)',(bid,ix,item['description'],item['quantity'],item['unit'],item['unit_price'],item['discount'],item['vat_rate'],item['amount'],item['category']))
    except Exception:
        if target is not None:target.unlink(missing_ok=True)
        raise
    return {'id':bid,'duplicate':False}

