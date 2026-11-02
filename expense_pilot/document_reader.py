"""Document Reader for the expense pilot."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

def guess_supplier(text):
    """Find a Croatian supplier label or a company named in the payment block."""
    for label in (r"PRODAVATELJ",r"DOBAVLJAČ",r"DOBAVLJAC",r"IZDAVATELJ",r"PRUŽATELJ USLUGE",r"NAZIV PRIMATELJA"):
        match=re.search(rf"(?im)^\s*{label}\s*:?[ \t]*(?:\n[ \t]*)?([^\n]+)",text)
        if match:
            candidate=match.group(1).strip()
            if len(candidate)>2 and not re.search(r"^(?:OIB|IBAN|ADRESA|RAČUN|RACUN)\b",candidate,re.I):return candidate[:120]
    match=re.search(r"(?im)^\s*(Zagrebački\s+holding\s+d\.o\.o\.)",text)
    if match:return re.sub(r"\s+"," ",match.group(1)).strip()
    # Holding utility bills often place the issuer in a standalone text line
    # rather than under a supplier label. The OCR text may have wide columns,
    # so inspect only the part of the line before the next column.
    match=re.search(r"(?im)^\s*(Zagrebački\s+holding\s+d\.o\.o\.)",text)
    if match:return re.sub(r"\s+"," ",match.group(1)).strip()
    for line in text.splitlines():
        match=re.search(r"\b(Zagrebački\s+holding\s+d\.o\.o\.)",line,re.I)
        if match:return re.sub(r"\s+"," ",match.group(1)).strip()
    recipient=re.search(r"(?is)PRIMATELJ.{0,400}?\n\s*([^\n]{3,100}?\b(?:d\.o\.o\.|d\.d\.|j\.d\.o\.o\.))",text)
    if recipient:return re.sub(r"\s+"," ",recipient.group(1)).strip()
    return ""


def parse_doc(text):
    # Conservative starter extraction. OCR and locale-specific field parsing are
    # deliberately separated so they can be improved without changing storage.
    # Decode the structured Croatian payment payload first; its payment fields
    # override any overlapping text/OCR guesses below.
    hub=hub3_fields(text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    supplier = guess_supplier(text) or (lines[0][:120] if lines else "")
    def first(pattern):
        m = re.search(pattern, text, re.I | re.M)
        return m.group(1).strip() if m else ""
    def after_label(label):
        m = re.search(rf"(?im)^\s*(?:{label})\s*:?\s*(?:\n\s*)?([^\n]+)", text)
        return m.group(1).strip() if m else ""
    supplier_block = re.search(r"(?is)PRODAVATELJ\s*(.*?)(?:\bPONUDA\b|\bRAČUN\b|\bRACUN\b)", text)
    if supplier_block:
        seller_lines=[x.strip() for x in supplier_block.group(1).splitlines() if x.strip()]
        if seller_lines:
            supplier=seller_lines[0]
    inv = first(r"\b((?:INV|PRO)[-_/]\d{4}[-_/]\d{3,})")
    if not inv:
        inv = first(r"(?:invoice|bill|document)\s*(?:no\.?|number|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})")
    if not inv:
        inv=after_label(r"PONUDA|RAČUN|RACUN")
        inv=re.match(r"([A-Z]{2,5}[-_/][A-Z0-9/_-]+)",inv,re.I).group(1) if re.match(r"([A-Z]{2,5}[-_/][A-Z0-9/_-]+)",inv,re.I) else ""
    if not inv:
        inv=first(r"(?:ra(?:č|c)un\s*(?:br\.?|broj))\s*:?\s*([A-Z0-9][A-Z0-9/_-]+)")
    date_pattern=r"(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"
    due_dt=first(rf"(?:due\s+date|datum\s+dospije[cć][ae]|dospije[cć][ae]|datum\s+dospi[cj]e[cć]a|rok\s+pla[cć]anja)\s*[:#-]?\s*{date_pattern}")
    dt=first(rf"(?:invoice\s+date|issue\s+date|datum\s+(?:izdavanja|izdano|ra[cč]una)|izdano)\s*[:#-]?\s*{date_pattern}")
    if not dt:
        zagreb_date=first(rf"Zagreb\s*,\s*{date_pattern}")
        if zagreb_date:dt=zagreb_date
    total = first(r"(?:grand\s+total|total\s+due|amount\s+due|total)\s*[: ]\s*(?:€|EUR\s*)?([0-9][0-9., ]*)")
    if not total:
        total=first(r"SVEUKUPNO\s*:?\s*(?:€|EUR\s*)?([0-9][0-9., ]*)")
    if not total:
        total=after_label(r"UKUPNO")
        if not re.search(r"\d",total):
            # OCR frequently places the value on the following line.
            m=re.search(r"(?im)^\s*UKUPNO\s*$\s*([0-9][0-9., ]*)",text)
            total=m.group(1).strip() if m else ""
    cur = "EUR" if ("€" in text or "EUR" in text.upper()) else "EUR"
    try:
        if dt and not re.match(r"^\d{4}-", dt):
            date_only=re.match(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",dt).group(0)
            date_parts=date_only.replace(".", "/").replace("-", "/")
            dt = datetime.strptime(date_parts, "%d/%m/%Y" if len(date_parts.split("/")[-1]) == 4 else "%d/%m/%y").date().isoformat()
    except ValueError: dt = ""
    try:
        if due_dt and not re.match(r"^\d{4}-",due_dt):
            due_only=re.match(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",due_dt).group(0)
            due_parts=due_only.replace(".","/").replace("-","/")
            due_dt=datetime.strptime(due_parts,"%d/%m/%Y" if len(due_parts.split("/")[-1])==4 else "%d/%m/%y").date().isoformat()
    except ValueError: due_dt=""
    try:
        amount_text = re.sub(r"[^0-9,.-]", "", total).replace(" ", "")
        if "." in amount_text and "," in amount_text:
            # The final separator is treated as the decimal mark.
            decimal_mark = "." if amount_text.rfind(".") > amount_text.rfind(",") else ","
            grouping_mark = "," if decimal_mark == "." else "."
            amount_text = amount_text.replace(grouping_mark, "").replace(decimal_mark, ".")
        elif "," in amount_text:
            tail = amount_text.rsplit(",", 1)[1]
            amount_text = amount_text.replace(",", ".") if len(tail) in (1, 2) else amount_text.replace(",", "")
        elif "." in amount_text:
            tail = amount_text.rsplit(".", 1)[1]
            if len(tail) in (1, 2):
                pass
            elif len(tail) > 2 and len(tail) <= 5:
                # OCR often drops the grouping/decimal mark in values such as
                # "1.23456". Currency amounts conventionally use two decimals.
                digits = amount_text.replace(".", "")
                amount_text = digits[:-2] + "." + digits[-2:]
            else:
                amount_text = amount_text.replace(".", "")
        amount = float(amount_text) if total else 0
    except ValueError: amount = 0
    def to_number(value):
        value=re.sub(r"[^0-9,.-]", "", str(value or ""))
        if not value:return 0.0
        if "." in value and "," in value:
            dec="." if value.rfind(".")>value.rfind(",") else ","; group="," if dec=="." else "."
            value=value.replace(group,"").replace(dec,".")
        elif "," in value:
            tail=value.rsplit(",",1)[1]; value=value.replace(",",".") if len(tail) in (1,2) else value.replace(",","")
        elif "." in value and len(value.rsplit(".",1)[1]) not in (1,2):
            tail_len=len(value.rsplit(".",1)[1]); digits=value.replace(".","")
            value=digits[:-2]+"."+digits[-2:] if 2<len(digits)<=7 and 2<tail_len<=5 else digits
        try:return float(value)
        except ValueError:return 0.0
    vat=0.0
    recap=re.search(r"(?is)REKAPITULACIJA\s+PDV(?:-A)?(.*?)(?:UKUPNO|TOTAL)",text)
    if recap:
        recap_lines=[x.strip() for x in recap.group(1).splitlines() if x.strip()]
        for i in range(len(recap_lines)-1,-1,-1):
            line=recap_lines[i]
            if re.search(r"PDV",line,re.I) and "%" in line:
                after_percent=line.rsplit("%",1)[1].strip(" :")
                candidate=re.match(r"([0-9][0-9.,]*)",after_percent)
                if not candidate and i+1<len(recap_lines):candidate=re.match(r"([0-9][0-9.,]*)",recap_lines[i+1])
                if candidate:vat=to_number(candidate.group(1));break
            if re.fullmatch(r"PDV",line,re.I) and i+3<len(recap_lines) and "%" in recap_lines[i+1]:
                candidate=re.match(r"([0-9][0-9.,]*)",recap_lines[i+3])
                if candidate:vat=to_number(candidate.group(1));break
    if not vat:
        vat_match=first(r"PDV\s*\d{1,2}\s*%\s*:?\s*([0-9][0-9.,]*)\s*EUR")
        if vat_match:vat=to_number(vat_match)
    def next_label(label):
        return after_label(label)
    address=""
    if supplier_block:
        sl=[x.strip() for x in supplier_block.group(1).splitlines() if x.strip()]
        for si,line in enumerate(sl[1:],1):
            if re.search(r"\b(?:ulica|cesta|trg|avenija|put)\b.*\d",line,re.I):
                address=line
                if si+1<len(sl) and re.search(r"\b\d{5}\b",sl[si+1]):address+=", "+sl[si+1]
                break
    supplier_oib_match=re.search(re.escape(supplier)+r"(?:(?!\n\s*\S).){0,1000}?OIB\s*:?\s*(\d{11})",text,re.I|re.S) if supplier else None
    oib=supplier_oib_match.group(1) if supplier_oib_match else first(r"(?im)^\s*OIB\s*:\s*(\d{11})")
    if supplier.casefold().startswith('zagrebački holding'):
        for legal_line in text.splitlines():
            if re.search(r"Zagrebački\s+holding\s+d\.o\.o\.",legal_line,re.I):
                oib_match=re.search(r"OIB\s*:?\s*(\d{11})",legal_line,re.I)
                if oib_match:
                    oib=oib_match.group(1)
                    address_match=re.search(r"d\.o\.o\.,?\s*(.*?)\s*,?\s*OIB\s*:?",legal_line,re.I)
                    if address_match and not address:
                        address=re.sub(r"\s+"," ",re.split(r",\s*(?:MBS|OIB)\b",address_match.group(1),maxsplit=1,flags=re.I)[0]).strip(" ,")
                    break
    vat_id=first(r"PDV\s*ID\s*:\s*([A-Z]{2}\d{8,})")
    # Tie the account to the supplier/payment block. A bill can list bank
    # details for another utility company in its legal footer.
    iban=""
    supplier_iban_match=re.search(re.escape(supplier)+r".{0,700}?IBAN\s*:?\s*([A-Z]{2}\d{2}[A-Z0-9 ]{10,})",text,re.I|re.S) if supplier else None
    payment_iban_match=re.search(r"(?:PODACI\s+ZA\s+UPLATU|IBAN\s+primatelja).{0,400}?IBAN\s*:?\s*([A-Z]{2}\d{2}[A-Z0-9 ]{10,})",text,re.I|re.S)
    if supplier_iban_match:iban=supplier_iban_match.group(1)
    elif payment_iban_match:iban=payment_iban_match.group(1)
    items=[]
    item_section=re.search(r"(?is)STAVKE(.*?)(?:REKAPITULACIJA\s+PDV|UKUPNO)",text)
    if item_section:
        item_lines=[x.strip() for x in item_section.group(1).splitlines() if x.strip()]
        # OCR often emits a table as one value per line. Locate item number,
        # then collect the description and the six following table values.
        for i,line in enumerate(item_lines):
            if not re.fullmatch(r"\d{1,3}[.)]?",line):continue
            j=i+1
            desc=[]
            while j<len(item_lines) and not re.fullmatch(r"\d+[,.]\d{1,3}",item_lines[j]):
                if item_lines[j].upper() in {"OPIS","KOL.","JM","CIJENA","POPUST","PDV","IZNOS","#"}:
                    j+=1; continue
                desc.append(item_lines[j]); j+=1
            if not desc or j+5>=len(item_lines):continue
            qty=item_lines[j]; unit=item_lines[j+1]; price=item_lines[j+2]; discount=item_lines[j+3]; tax=item_lines[j+4]; line_total=item_lines[j+5]
            tax_match=re.search(r"(\d{1,2})\s*%",tax)
            if not tax_match and j+6<len(item_lines):
                tax_match=re.search(r"(\d{1,2})\s*%",item_lines[j+5]); line_total=item_lines[j+6]
            if not re.fullmatch(r"\d+[,.]?\d*",price) or not re.search(r"%",tax if tax_match else ""):
                continue
            items.append({"description":" ".join(desc),"quantity":to_number(qty),"unit":unit,"unit_price":to_number(price),"discount":discount,"vat_rate":float(tax_match.group(1)) if tax_match else 0,"amount":to_number(line_total),"category":""})
    if not items:
        # Some Croatian utility statements combine multiple sub-bills under
        # one payment barcode instead of using a single STAVKE table.
        fee=re.search(r"(?is)NAKNADE\s+za\s+prostor.*?(?:\n)(.*?)(?:\bUKUPNO\b)",text)
        if fee:
            for line in fee.group(1).splitlines():
                m=re.match(r"\s*([A-Za-zČĆĐŠŽčćđšž][^\n]*?)\s{2,}([0-9][0-9.,]*)\s*EUR\b",line,re.I)
                if m and not re.search(r"PDV|MEĐUZBROJ|OSNOVICA",m.group(1),re.I):
                    val=to_number(m.group(2)); items.append({"description":m.group(1).strip(),"quantity":1.0,"unit":"","unit_price":val,"discount":"","vat_rate":0,"amount":val,"category":""})
        service=re.search(r"(?is)(Javna usluga[^\n]*)(.*?)(?:\bSVEUKUPNO\b)",text)
        if service:
            service_total=re.search(r"(?im)^\s*UKUPNO\s+([0-9][0-9.,]*)\s*EUR\b",service.group(2))
            if service_total:
                val=to_number(service_total.group(1)); desc=re.sub(r"\s+"," ",service.group(1)).strip().rstrip(":")
                items.append({"description":desc,"quantity":1.0,"unit":"","unit_price":val,"discount":"","vat_rate":0,"amount":val,"category":""})
    # A HUB-3 PDF417 contains structured payment fields. Prefer these for the
    # payee, payment amount and IBAN; keep OCR/PDF text for invoice details.
    payment_model=hub.get("model",""); payment_reference=hub.get("reference","")
    payment_purpose=hub.get("purpose",""); payment_description=hub.get("description","")
    if hub:
        supplier=hub.get("payee") or supplier
        address=", ".join(x for x in [hub.get("payee_address"),hub.get("payee_city")] if x)
        iban=hub.get("iban") or iban
        amount=hub.get("amount") or amount
        cur=hub.get("currency") or cur
        supplier_oib_match=re.search(re.escape(supplier)+r"[^\n]{0,500}?OIB\s*:?\s*(\d{11})",text,re.I)
        if supplier_oib_match:oib=supplier_oib_match.group(1)
        if not due_dt:
            due_match=re.search(r"DOSPI(?:J|JE)ĆE\s*([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{4})",payment_description,re.I)
            if due_match:
                parts=due_match.group(1).replace(".","/").replace("-","/")
                try:due_dt=datetime.strptime(parts,"%d/%m/%Y").date().isoformat()
                except ValueError:pass
    if due_dt and dt==due_dt:dt=""
    return {"supplier":supplier,"invoice_number":inv,"invoice_date":dt,"due_date":due_dt,"amount":amount,"currency":cur,"supplier_address":address,"supplier_oib":oib,"supplier_vat_id":vat_id,"supplier_iban":re.sub(r"\s+","",iban),"vat_amount":vat,"line_items":items,"payment_model":payment_model,"payment_reference":payment_reference,"payment_purpose":payment_purpose,"payment_description":payment_description}


def fingerprint(text, supplier):
    # Stable document-layout clue, independent of OCR's initial supplier guess.
    normalized = re.sub(r"\d+", "#", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()[:1000]
    seed = normalized[:500].encode("utf-8", "ignore")
    return hashlib.sha256(seed).hexdigest()


def find_executable(env_name: str, command: str, windows_paths=()) -> str | None:
    """Resolve a tool from config/PATH and common Windows install locations."""
    configured = os.getenv(env_name)
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    found = shutil.which(command)
    if found:
        return found
    for candidate in windows_paths:
        if Path(candidate).is_file():
            return candidate
    return None


def read_pdf417_image(image):
    """Return a decoded HUB-3 PDF417 payload from a PIL image, if present."""
    try:
        import zxingcpp
    except ImportError:
        return ""
    try:
        results=zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.PDF417, try_rotate=True)
    except Exception:
        return ""
    for result in results:
        payload=(getattr(result,"text","") or "").replace("\r\n","\n").replace("\r","\n").replace("\x00","").strip("\n")
        if payload[:8].upper()=="HRVHUB30" and (len(payload)>=273 or len(payload.splitlines())>=13):
            return payload
    return ""


def pdf417_decoder_available():
    try:
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def read_pdf417_file(path: Path) -> str:
    """Scan likely HUB-3 barcode areas first, then full first pages."""
    try:
        import fitz
        from PIL import Image
        with fitz.open(path) as pdf:
            for page in list(pdf)[:4]:
                height=page.rect.height; width=page.rect.width
                clips=[fitz.Rect(0,max(0,height-170),width*.72,height),page.rect]
                for clip in clips:
                    pix=page.get_pixmap(matrix=fitz.Matrix(3.5,3.5),clip=clip,alpha=False)
                    img=Image.frombytes("RGB",(pix.width,pix.height),pix.samples)
                    payload=read_pdf417_image(img)
                    if payload:return payload
    except (ImportError,RuntimeError,ValueError):
        pass
    return ""


def hub3_fields(text):
    """Parse Croatian HUB-3 as 14 positional fields, preserving empty fields."""
    normalized=text.replace("\r\n","\n").replace("\r","\n")
    markers=list(re.finditer(r"HRVHUB30",normalized,re.I))
    if not markers:return {}
    # Decoded barcode text is appended after PDF/OCR text, so prefer the last
    # complete marker if OCR happened to recognize part of the barcode too.
    marker=markers[-1]
    candidate=normalized[marker.start():]
    lengths=(8,3,15,30,27,27,25,25,27,21,4,22,4,35)
    first_line=candidate.split("\n",1)[0]
    if len(first_line)>=sum(lengths):
        # Some PDF417 readers return the Croatia-standard 273 fixed-width
        # characters as one string instead of separating the 14 fields.
        compact=candidate[:sum(lengths)]
        fields=[];offset=0
        for size in lengths:fields.append(compact[offset:offset+size].strip());offset+=size
    else:
        lines=candidate.split("\n")
        payload_lines=lines[:14]
        # Some decoders omit the final blank description field.
        if len(payload_lines)==13:payload_lines.append("")
        if len(payload_lines)<14:return {}
        fields=[value.strip() for value in payload_lines]
    if len(fields[0])!=8:return {}
    if not re.fullmatch(r"[A-Z]{3}",fields[1].upper()):return {}
    amount_digits=re.sub(r"\D","",fields[2])
    amount=(int(amount_digits)/100) if amount_digits else 0
    if not amount:return {}
    return {"currency":fields[1],"amount":amount,"payer":fields[3],"payer_address":fields[4],
        "payer_city":fields[5],"payee":fields[6],"payee_address":fields[7],"payee_city":fields[8],
        "iban":re.sub(r"\s+","",fields[9]),"model":fields[10],"reference":fields[11],
        "purpose":fields[12],"description":fields[13]}


def append_barcode(text,payload):
    return text+"\n\n--- DECODED HUB-3 PDF417 ---\n"+payload if payload else text


def local_ocr(path: Path) -> tuple[str, str]:
    """Extract text locally from common image/PDF uploads; never calls a service."""
    suffix = path.suffix.lower()
    tess = find_executable("TESSERACT_CMD", "tesseract", (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ))
    pdftext = find_executable("PDFTOTEXT_CMD", "pdftotext")
    pdfpages = find_executable("PDFTOPPM_CMD", "pdftoppm")
    barcode_note="" if pdf417_decoder_available() else " · PDF417 decoder unavailable; install updated requirements"
    def run_tesseract(image):
        language=os.getenv("OCR_LANG", "hrv+eng")
        langs=[language]+(["eng"] if language!="eng" else [])
        last=None
        for lang in langs:
            last=subprocess.run([tess, str(image), "stdout", "-l", lang, "--psm", "3"],capture_output=True,text=True,timeout=90)
            if last.returncode==0:return last
        return last
    try:
        if suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="replace"), "Text file"
        if suffix == ".pdf":
            # Digital PDFs retain better text through pdftotext. Scanned PDFs
            # fall back to rendered pages and local Tesseract OCR.
            barcode=read_pdf417_file(path)
            extracted = ""
            if pdftext:
                proc = subprocess.run([pdftext, "-layout", str(path), "-"], capture_output=True, text=True, timeout=45)
                extracted = proc.stdout.strip()
            else:
                try:
                    import fitz
                    with fitz.open(path) as pdf:
                        extracted = "\n\n".join(page.get_text("text") for page in pdf).strip()
                except ImportError:
                    pass
            if len(extracted) >= 20:
                method="HUB-3 PDF417 + PDF text extraction" if barcode else "PDF text extraction"+barcode_note
                return append_barcode(extracted,barcode), method
            if not tess:
                if barcode:return append_barcode("",barcode),"HUB-3 PDF417 (Tesseract unavailable)"
                note="; PDF417 decoder is also unavailable, install updated requirements.txt" if not pdf417_decoder_available() else ""
                return "", "Tesseract is not installed or not on PATH; set TESSERACT_CMD to its executable path"+note
            with tempfile.TemporaryDirectory(prefix="expense-ocr-") as tmp:
                images=[]
                if pdfpages:
                    prefix = str(Path(tmp) / "page")
                    subprocess.run([pdfpages, "-jpeg", "-r", "200", "-scale-to", "2400", str(path), prefix], check=True, capture_output=True, timeout=90)
                    images = sorted(Path(tmp).glob("page-*.jpg"))
                else:
                    try:
                        import fitz
                        with fitz.open(path) as pdf:
                            for i, page in enumerate(pdf.pages()):
                                if i >= 20: break
                                pix=page.get_pixmap(matrix=fitz.Matrix(2.5,2.5), alpha=False)
                                image=Path(tmp)/f"page-{i+1:03d}.png"; pix.save(image); images.append(image)
                    except ImportError:
                        return "", "Scanned PDF needs pdftoppm or the Python package PyMuPDF (fitz) to render pages"
                parts = []
                for img in images[:20]:
                    ocr = run_tesseract(img)
                    if ocr.returncode == 0 and ocr.stdout.strip(): parts.append(ocr.stdout.strip())
                extracted="\n\n".join(parts)
                method="HUB-3 PDF417 + Tesseract OCR" if barcode else "Scanned PDF · Tesseract OCR"+barcode_note
                return append_barcode(extracted,barcode), method
        elif suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
            barcode=""
            try:
                from PIL import Image
                with Image.open(path) as image:barcode=read_pdf417_image(image.convert("RGB"))
            except ImportError:pass
            if not tess:
                if barcode:return append_barcode("",barcode),"HUB-3 PDF417 (Tesseract unavailable)"
                note="; PDF417 decoder is also unavailable, install updated requirements.txt" if not pdf417_decoder_available() else ""
                return "", "Tesseract is not installed or not on PATH; set TESSERACT_CMD to its executable path"+note
            proc = run_tesseract(path)
            if proc.returncode == 0:
                method="HUB-3 PDF417 + Tesseract OCR" if barcode else "Tesseract OCR"+barcode_note
                return append_barcode(proc.stdout.strip(),barcode), method
            raise RuntimeError(proc.stderr.strip() or "Tesseract could not read this image")
        return "", "Unsupported file type for OCR"
    except FileNotFoundError as e:
        missing = e.filename or "required OCR command"
        return "", f"OCR tool unavailable: could not start {missing}; check the command path/PATH and its dependencies"
    except subprocess.TimeoutExpired:
        return "", "OCR timed out; try a smaller document or paste the text manually"
    except (subprocess.CalledProcessError, RuntimeError) as e:
        return "", f"OCR failed: {e}"

