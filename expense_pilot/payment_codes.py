"""Payment Codes for the expense pilot."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .config import PAYER_CONFIG

HUB3_ALLOWED = re.compile(r"[^A-Za-z0-9ČĆĐŠŽčćđšž .,/+()\-]")

def hub3_text(value,limit):
    text=re.sub(r'\s+',' ',str(value or '')).strip()
    text=HUB3_ALLOWED.sub(' ',text)
    return re.sub(r'\s+',' ',text).strip()[:limit]


def hub3_payload(bill):
    if str(bill['currency'] or '').upper()!='EUR':
        raise ValueError('HUB-3 payment codes are set up for EUR bills only.')
    iban=re.sub(r'\s+','',str(bill['supplier_iban'] or '')).upper()
    if not re.fullmatch(r'HR\d{19}',iban):
        raise ValueError('A Croatian recipient IBAN is required. Check the supplier details before creating a payment code.')
    try:
        cents=int((Decimal(str(bill['amount'])).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)*100))
    except (InvalidOperation,ValueError,TypeError):
        cents=0
    if cents<=0 or cents>999999999999999:
        raise ValueError('Enter a valid positive bill amount before creating a payment code.')
    model=re.sub(r'\s+','',str(bill['payment_model'] or '')).upper()
    if not model:model='HR00'
    elif re.fullmatch(r'\d{2}',model):model='HR'+model
    if not re.fullmatch(r'HR\d{2}',model):
        raise ValueError('The payment model must be HR followed by two digits, such as HR00.')
    supplier_name=hub3_text(bill['supplier'],25)
    if not supplier_name:raise ValueError('Enter the supplier name before creating a payment code.')
    supplier_address=[line.strip() for line in str(bill['supplier_address'] or '').splitlines() if line.strip()]
    recipient_street=hub3_text(supplier_address[0] if supplier_address else '',25)
    recipient_city=hub3_text(supplier_address[1] if len(supplier_address)>1 else '',27)
    description=bill['payment_description'] or bill['description'] or f'Invoice {bill["invoice_number"]}'
    fields=[
        'HRVHUB30','EUR',f'{cents:015d}',hub3_text(PAYER_CONFIG['name'],30),
        hub3_text(PAYER_CONFIG['address'],27),hub3_text(PAYER_CONFIG['postal_city'],27),
        supplier_name,recipient_street,recipient_city,iban,model,
        hub3_text(bill['payment_reference'],22),
        hub3_text(bill['payment_purpose'] or 'OTHR',4).upper(),hub3_text(description,35),
    ]
    return '\n'.join(fields)+'\n'


def hub3_svg(bill):
    try:
        import zxingcpp
    except ImportError as exc:
        raise ValueError('PDF417 generation requires the updated zxing-cpp package. Run python -m pip install -r requirements.txt, then restart the app.') from exc
    if not hasattr(zxingcpp,'create_barcode'):
        raise ValueError('PDF417 generation needs a newer zxing-cpp. Run python -m pip install --upgrade zxing-cpp, then restart the app.')
    barcode=zxingcpp.create_barcode(hub3_payload(bill),zxingcpp.BarcodeFormat.PDF417,columns=9,ec_level=4,eci='UTF-8')
    return barcode.to_svg(scale=3,add_hrt=False,add_quiet_zones=True)

