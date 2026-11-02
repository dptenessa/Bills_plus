# EFIZ expense pilot

A small Flask application for bill capture, validation, approvals, payment preparation, supplier records, bank reconciliation, and reporting. It uses a conventional top-level `app.py`, `templates/`, and `static/` structure. Route handlers are grouped under `expense_pilot/routes/`. The `expense_pilot/` package separates database access, document reading, bills, payments, comments, suppliers, bank services, page data, configuration, and security. `expense_pilot/core.py` is a small compatibility facade for existing route imports.

## Run locally

1. Stop the old app with **Ctrl+C**.
2. If you want a clean start, remove the old app files and its `data/` and `uploads/` folders. The new ZIP contains no database or uploaded documents.
3. Extract the ZIP into an empty app folder. Install Python 3.10 or newer and Tesseract OCR, then run `python -m pip install -r requirements.txt`. Poppler (`pdftotext` and `pdftoppm`) is optional.
4. Run `python app.py` and open `http://127.0.0.1:8000`.
5. On first launch, create the administrator account. The admin can invite submitters and validators from **Users & invites**.

The clean ZIP contains `app.py`, top-level Jinja templates and CSS, the supporting Python modules, requirements, and `expense_config.json`. It does not contain a database or uploaded documents. On first run, the app creates an empty `data/` folder and SQLite database; bill uploads are saved under `uploads/`.

For camera upload over a private Wi-Fi network, run `python app.py --lan` and open the printed address on the phone. This development server uses HTTP; keep it on a trusted network.

## Bill workflow

- A submitter can upload scans or PDFs, take a phone photo, or enter a bill manually.
- Images are resized to at most 2200 × 2200 pixels and stored as optimized JPEGs. PDFs are cleaned and deflated when that makes them smaller; text and vector content are preserved. Original files are not duplicated in a second archive.
- During review, the submitter selects one required route: **Validate and send for approval**, **Validate as Minimax and send for approval**, or **Just record**. The two approval routes send EUR bills over €150 to the validator inbox; bills at or below €150 are validated immediately. Non-EUR bills on either approval route wait for a validator because no currency conversion is applied.
- A validator can reject a bill, request more details, approve payment now, approve for its due date, or choose another payment date. Submitters can answer detail requests and return them to the inbox. Rejected bills remain open until marked solved. Each bill has an always-visible discussion thread shared by its submitter and validator. Unread notes are marked in the relevant bill row and inbox/dashboard; pending actions also appear as a badge.
- Before approval, the submitter can reopen and edit a bill, then choose its route and send it through review again. The submitter can permanently delete an eligible bill flow. Archive bills can be erased with their comments, read receipts, approval and bill audit history. If a deleted bill was part of a bank reconciliation, the associated transaction remains in the statement as unmatched, and other bills sharing that transaction are reopened as needed.
- The submitter / payer dashboard filters bills by payment date or shows all, including future bills. Opening a payable bill displays a HUB-3 PDF417 code. After paying in the banking app, mark it **Paid**; canceling the modal leaves it open. Paid and solved bills appear under the top-level **Archive** page, where the entire bill track can be permanently deleted.
- The **Bill wall** collects discussion messages across accessible bills. Unread messages are marked individually. Opening a linked bill marks messages from other users as read for the current user.
- Payment purpose (`Šifra namjene`) and payment description (`Opis plaćanja`) are transfer fields included in the Croatian HUB-3 code, not internal comments. The separate bill `Description` can be used as fallback text if payment description is blank. These fields are part of the code format; whether a bank requires a value depends on the transfer. Payment codes require an EUR amount, a Croatian recipient IBAN, and a valid payment model. Set the payer name, address, and postal city in `expense_config.json` if you want these included. The user should verify the recipient and amount in the bank app before authorizing payment.

The package includes `expense_config.json`, with default categories, cost centers, and blank payer details. Edit it before creating payment codes if you want payer details embedded in the HUB-3 code. The app creates its database tables at startup.

## Storage and database

By default, the app stores its SQLite database at `data/expense_pilot.db` and uploaded bills and bank statements under `uploads/`. Back up both folders together. Set `DATABASE_URL` and `UPLOAD_DIR` to use other persistent paths.

SQLite is a separate database file and does not conflict with a MySQL/MariaDB database used by another Flask app on PythonAnywhere. It is suitable for this small pilot and a few users. Keep the database on the same persistent filesystem as the app; do not put it on a network share. SQLite permits concurrent reads and serializes writes, which is appropriate for this low-volume use.

The app currently owns its login, roles, and session tables. Its route blueprints provide a clean integration boundary, though the existing school's login and URL layout would need to be connected when mounting it inside that app. No existing SQL database or tables need to be changed for the pilot. A migration to the hosted SQL server can be done if usage or deployment needs change.

For a PythonAnywhere WSGI file, use an absolute path to this project and create the application with `from expense_pilot import create_app; application = create_app()`. Configure `DATABASE_URL` and `UPLOAD_DIR` to persistent paths in the WSGI environment. Install the packages in `requirements.txt` in the web app's virtual environment. The local `app.py` runner is for development; PythonAnywhere serves the WSGI `application` object.

## OCR and bank imports

Digital PDFs are read with PyMuPDF or Poppler. Scanned PDFs are rendered and read with Tesseract; images use Tesseract directly; `.txt` files are read directly. Croatian OCR uses `hrv+eng` when installed and falls back to English. Extracted details are suggestions and require review. Set `TESSERACT_CMD`, `PDFTOTEXT_CMD`, `PDFTOPPM_CMD`, or `OCR_LANG` when needed.

CSV and XLSX bank imports support signed amounts or separate debit and credit columns. The reconciliation screen supports matching a transaction across multiple bills, partial payments, refunds, and other income or costs. Bank reconciliation remains a separate workflow from approval and payment preparation.

## Configuration and limits

All signed-in roles can open **Archive** and download **Situation of all bills** as Excel. This shared export includes every user's archived and ongoing bills, their current status, invoice and payment dates, category, original currency, and net bank allocations. It is independent of archive filters and pagination. Permanently deleted bills cannot be exported.

**Budget alignment** is shared by all signed-in users. Plans are stored by **accounting year (January–December)** and **version**, such as Budget, FCQ1, FCQ2, FCQ3, or FCQ4. Additional version names are accepted. Choose the year and version explicitly in the upload form, download the matching Excel template, fill the `Budget plan` sheet with `month` (`YYYY-MM`), `category`, `currency`, and `planned_amount`, then upload it using the same year and version. The template includes instructions and year/version metadata. Mismatched metadata or months outside the chosen accounting year are rejected. Existing plans from before versioning are retained as the Budget version.

Uploads validate the complete file before saving and replace only the month/category/currency entries provided **within the chosen year and version**; omitted entries and other versions stay unchanged. Upload zero to clear a planned amount. Amounts must be non-negative numeric values with at most two decimal places; formulas and duplicate entries are rejected. Select a version to inspect its category and monthly detail, or use the version comparison table to compare all uploaded versions against the same actual payments.

Filter the comparison by year, month (or whole year), and currency to see planned spending, paid spending, remaining budget, and overspending by category and month. Actuals use bank allocation dates and amounts (including partial payments and subtracting supplier refunds received). Employee reimbursements count as positive expenses, like invoices. Paid bills without bank allocations use their full amount and recorded payment date, falling back to paid status history; they are never counted again alongside allocations. Undated paid records are flagged and excluded. Categories use the bill's current category; currencies are kept separate. Whole-year comparisons use the full twelve-month plan.

Click a category, monthly planned amount, or paid amount to see the underlying bill payments for that category and period. The breakdown shows each payment/refund contribution and its source, and totals match the comparison. Click a bill for shared read-only invoice details. The selected year, month, currency, and plan version are retained when returning to the comparison. Existing workflow editing and document permissions still apply. A planned amount with no paid bills opens an empty breakdown.

### Password changes and recovery

Signed-in users can choose **Change password** beside their account name. Their current password is required, and the new password must be 12–1024 characters with matching confirmation. Saving revokes every session and unused reset link for that user, then asks them to sign in again.

The sign-in page includes **Forgot your password?**. This app has no email delivery, so recovery is assisted by an administrator: from **Users & invites → Create a password reset link**, select an active user, confirm their identity, and share the link privately. The link expires after 30 minutes, can be used once, and lets the user choose their own password. Creating another link invalidates previous links. Only a hash of the reset token is stored. Completing recovery revokes all user sessions; disabling an account prevents link use. Existing administrator-set temporary passwords also invalidate reset links. A sole administrator who has lost every signed-in session needs assistance from the application operator; public recovery never grants access based on a username alone.

Edit bill categories and cost centers in `expense_config.json`. OCR and field extraction may need correction for unfamiliar document layouts. Reports retain source currencies and do not convert amounts. The pilot does not send email invitations or initiate payments; it displays a payment code for the submitter to scan and confirm in their bank.
