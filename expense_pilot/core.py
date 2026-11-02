"""Compatibility exports for route modules. New logic belongs in focused modules."""
from .config import (ROOT, DB_URL, UPLOAD_DIR, CONFIG_PATH, DEFAULT_CONFIG,
    EXPENSE_CONFIG, PAYER_CONFIG, SESSION_HOURS, load_expense_config, load_payer_config)
from .request_context import REQUEST_CONTEXT
from .database import connect, init_db
from .web import (esc, password_record, audit, approval_threshold, csrf_input,
    render_page, page, setup_page, login_page, users_page, invite_accept_page)
from .comments import (add_bill_comment, unread_bill_comment_count,
    mark_bill_comments_read, notification_counts, bill_discussion_data)
from .approval_views import approvals_page, audit_page
from .document_reader import (guess_supplier, parse_doc, fingerprint, find_executable,
    read_pdf417_image, pdf417_decoder_available, read_pdf417_file, hub3_fields,
    append_barcode, local_ocr)
from .identifiers import (normalize_oib, normalize_invoice_number, supplier_identity,
    find_duplicate_bill, normalized_reference)
from .supplier_services import get_or_create_supplier
from .payment_codes import HUB3_ALLOWED, hub3_text, hub3_payload, hub3_svg
from .bill_ingestion import compress_uploaded_document, upload_one_bill
from .bill_views import (dashboard, manual_bill_page, payer_page, bills_page,
    suppliers_page, bill_page, uploaded_page, archive_page)
from .bank_services import (parse_bank_amount, read_bank_rows, normalize_bank_date,
    refresh_bill_payment_status, auto_match_exact_reference)
from .bank_views import reconcile_page
from .report_views import report_page
from .wall_views import bill_wall_page
