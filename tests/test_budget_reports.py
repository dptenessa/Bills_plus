"""Exercise shared exports and budget accounting against an isolated database."""
import hashlib
import gc
import re
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from expense_pilot import create_app
from expense_pilot.budget import PLAN_HEADERS, budget_data, import_plan
from expense_pilot.database import connect, init_db
from expense_pilot.web import password_record


class BudgetReportsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter('ignore', ResourceWarning)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch('expense_pilot.database.DB_URL',
                              'sqlite:///' + str(Path(self.temp.name) / 'test.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        # Existing SQLite context managers commit but leave closing to GC.
        # Collect connections before removing the temporary DB on Windows.
        self.addCleanup(gc.collect)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with connect() as db:
            for number, role in enumerate(('admin', 'submitter', 'validator'), 1):
                db.execute('''INSERT INTO users(id,username,password_hash,salt,role,created_at)
                    VALUES(?,?, 'unused','unused',?,'2026-01-01')''', (number, role, role))
                token = hashlib.sha256(role.encode()).hexdigest()
                db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token, number, '2099-01-01', 'test-csrf'))

    def login(self, role='admin'):
        self.client.set_cookie('expense_session', role)

    def plan_file(self, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Budget plan'
        sheet.append(PLAN_HEADERS)
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def import_rows(self, rows, year=2026, version='Budget'):
        return import_plan(self.plan_file(rows), {'id': 1}, year, version)

    def bill(self, amount, status='Paid at bank', paid_at='2026-02-10',
             category='Services', currency='EUR', document_type='invoice', created_by=2):
        with connect() as db:
            return db.execute('''INSERT INTO bills(supplier,amount,status,paid_at,category,currency,
                document_type,created_by,created_at) VALUES('Supplier',?,?,?,?,?,?,?,'2026-01-01')''',
                (amount, status, paid_at, category, currency, document_type, created_by)).lastrowid

    def allocation(self, bill_id, amount, txn_date, kind='bill_payment', currency='EUR'):
        with connect() as db:
            txn_id = db.execute('''INSERT INTO transactions(txn_date,description,amount,currency)
                VALUES(?,'Payment',?,?)''', (txn_date, amount, currency)).lastrowid
            db.execute('INSERT INTO payment_allocations(transaction_id,bill_id,amount,allocation_type) VALUES(?,?,?,?)',
                       (txn_id, bill_id, amount, kind))

    def test_payment_month_partial_payments_refunds_and_no_double_count(self):
        self.import_rows([('2026-02', 'Services', 'EUR', 200)])
        full = self.bill(100)
        self.allocation(full, 100, '2026-02-10')
        self.allocation(full, 20, '2026-03-10', 'supplier_refund')
        partial = self.bill(300, status='Order of payment issued')
        self.allocation(partial, 50, '2026-02-11')
        self.bill(25)
        self.bill(10, document_type='refund')
        self.bill(900, status='Ready for payment')
        self.bill(1000, status='Deleted')
        self.bill(700, currency='USD')
        feb = budget_data(2026, 2)
        self.assertEqual(feb['paid'], 165)
        self.assertEqual(feb['remaining'], 35)
        self.assertEqual(budget_data(2026, 3)['paid'], -20)
        self.assertEqual(budget_data(2026)['paid'], 145)
        self.assertEqual(budget_data(2026, 2, 'USD')['paid'], 700)

    def test_history_date_and_missing_date(self):
        history_bill = self.bill(42, paid_at='')
        self.bill(99, paid_at='')
        with connect() as db:
            db.execute('''INSERT INTO bill_status_history(bill_id,status,occurred_at)
                VALUES(?,'Paid at bank','2026-04-02')''', (history_bill,))
        data = budget_data(2026, 4)
        self.assertEqual(data['paid'], 42)
        self.assertEqual(data['undated'], 1)
        self.assertFalse(data['categories'][0]['has_plan'])

    def test_upload_upserts_and_preserves_omitted_months(self):
        self.import_rows([('2026-01', 'Services', 'EUR', 100), ('2026-02', 'Services', 'EUR', 200)])
        self.import_rows([('2026-01', 'services', 'eur', '1.234,56')])
        self.assertEqual(str(budget_data(2026)['planned']), '1434.56')
        self.import_rows([('2026-01', 'Services', 'EUR', 0)])
        self.assertEqual(budget_data(2026)['planned'], 200)

    def test_invalid_upload_is_atomic(self):
        self.import_rows([('2026-01', 'Services', 'EUR', 100)])
        for bad in (-1, 'NaN', '=1+2', 1.234, ''):
            with self.subTest(amount=bad), self.assertRaises(ValueError):
                self.import_rows([('2026-01', 'Services', 'EUR', 999), ('2026-02', 'Services', 'EUR', bad)])
            self.assertEqual(budget_data(2026)['planned'], 100)
        for rows in (
            [('2026-13', 'Services', 'EUR', 5)],
            [('2026-01', '', 'EUR', 5)],
            [('2026-01', 'Services', '', 5)],
            [('2026-01', 'Services', 'EUR', 5), ('2026-01', 'services', 'eur', 6)],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.import_rows(rows)

    def test_all_roles_can_export_all_users_and_statuses(self):
        self.bill(30, created_by=1)
        self.bill(50, status='Ready for payment', created_by=2)
        self.bill(70, status='Rejected', created_by=3)
        for role in ('admin', 'submitter', 'validator'):
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.client.get('/archive').status_code, 200)
                response = self.client.get('/bills-situation.xlsx?from=2099-01-01&page=99')
                self.assertEqual(response.status_code, 200)
                workbook = load_workbook(BytesIO(response.data))
                self.assertEqual(workbook.active.max_row, 4)
                workbook.close()
                page = self.client.get('/budget?year=2026')
                self.assertEqual(page.status_code, 200)
                self.assertIn(b'Budget alignment', page.data)

    def test_export_formula_like_text_stays_text(self):
        bill_id = self.bill(1)
        with connect() as db:
            db.execute('UPDATE bills SET supplier=? WHERE id=?', ('=1+1', bill_id))
        self.login()
        response = self.client.get('/bills-situation.xlsx')
        workbook = load_workbook(BytesIO(response.data))
        self.assertEqual(workbook.active['B2'].value, '=1+1')
        self.assertEqual(workbook.active['B2'].data_type, 's')
        workbook.close()

    def test_template_and_upload_for_each_role_with_csrf(self):
        for role in ('admin', 'submitter', 'validator'):
            with self.subTest(role=role):
                self.login(role)
                template = self.client.get('/budget-template.xlsx?year=2026&version=Budget')
                self.assertEqual(template.status_code, 200)
                workbook = load_workbook(BytesIO(template.data))
                self.assertIn('Budget plan', workbook.sheetnames)
                workbook.close()
                response = self.client.post('/budget-upload', data={
                    'year': '2026', 'version': 'Budget', 'csrf_token': 'test-csrf',
                    'budget_file': (BytesIO(self.plan_file([('2026-02', 'Services', 'EUR', 120)])), 'plan.xlsx')},
                    follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Saved 1 monthly budget values.', response.data)
        self.assertEqual(self.client.post('/budget-upload', data={}).status_code, 403)
        bad = self.client.post('/budget-upload', data={'csrf_token': 'test-csrf', 'year': '2026',
            'budget_file': (BytesIO(b'broken'), 'bad.xlsx')})
        self.assertEqual(bad.status_code, 400)

    def test_anonymous_access_requires_login(self):
        for path in ('/archive', '/budget', '/budget-template.xlsx', '/bills-situation.xlsx'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith('/login'))

    def test_year_and_version_isolation_and_comparisons(self):
        self.import_rows([('2026-02', 'Services', 'EUR', 100)])
        self.import_rows([('2026-02', 'Services', 'EUR', 200)], version='fcq1')
        self.import_rows([('2026-02', 'Services', 'EUR', 300)], version='FCQ2')
        self.import_rows([('2027-02', 'Services', 'EUR', 700)], year=2027)
        self.import_rows([('2026-02', 'Services', 'EUR', 220)], version='FCQ1')
        self.bill(50)
        self.assertEqual(budget_data(2026, 2)['planned'], 100)
        fcq = budget_data(2026, 2, version='FCQ1')
        self.assertEqual(fcq['planned'], 220)
        self.assertEqual(fcq['paid'], 50)
        self.assertEqual(budget_data(2026, 2, version='FCQ2')['planned'], 300)
        self.assertEqual(budget_data(2027, 2)['planned'], 700)
        comparisons = {row['version']: row for row in fcq['version_comparisons']}
        self.assertEqual(comparisons['Budget']['remaining'], 50)
        self.assertEqual(comparisons['FCQ2']['remaining'], 250)
        with self.assertRaises(ValueError):
            self.import_rows([('2027-01', 'Services', 'EUR', 999)])
        self.login()
        for missing in ({'year': '2026'}, {'version': 'FCQ1'}, {'year': 'invalid', 'version': 'FCQ1'}):
            response = self.client.post('/budget-upload', data={**missing, 'csrf_token': 'test-csrf',
                'budget_file': (BytesIO(self.plan_file([('2026-02', 'Services', 'EUR', 5)])), 'plan.xlsx')})
            self.assertEqual(response.status_code, 400)

    def test_budget_payment_breakdown_matches_total_and_filters(self):
        full = self.bill(100)
        self.allocation(full, 80, '2026-02-10')
        self.allocation(full, 20, '2026-03-10')
        self.allocation(full, 5, '2026-02-12', 'supplier_refund')
        manual = self.bill(25)
        self.bill(400, category='Travel')
        self.bill(500, currency='USD')
        self.bill(600, paid_at='2027-02-10')
        self.bill(700, status='Ready for payment')
        self.import_rows([('2026-02', 'Services', 'EUR', 200)], version='FCQ1')
        data = budget_data(2026, 2, version='FCQ1')
        services = [p for p in data['payment_details'] if p['category'] == 'Services']
        self.assertEqual(sum(p['amount'] for p in services), 100)
        self.assertEqual({p['bill_id'] for p in services}, {full, manual})
        self.assertEqual(len(services), 3)
        self.assertEqual(sum(p['amount'] for p in data['payment_details']), data['paid'])
        self.login()
        path = '/budget/bills?year=2026&month=2&currency=EUR&version=FCQ1&category=services'
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Bank refund', response.data)
        self.assertIn(b'100,00 EUR', response.data)
        self.assertIn(b'200,00 EUR', response.data)
        self.assertNotIn(b'Travel', response.data)
        self.assertIn(f'/budget/bills/{full}?'.encode(), response.data)
        page = self.client.get('/budget?year=2026&version=FCQ1')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'/budget/bills?year=2026&amp;month=2&amp;currency=EUR&amp;version=FCQ1&amp;category=Services', page.data)

    def test_budget_bill_read_only_detail_is_shared_and_empty_period_works(self):
        bill_id = self.bill(30, created_by=1)
        with connect() as db:
            db.execute('UPDATE bills SET description=? WHERE id=?', ('Visible invoice detail', bill_id))
        for role in ('admin', 'submitter', 'validator'):
            self.login(role)
            detail = self.client.get(f'/budget/bills/{bill_id}?year=2026&month=2&version=FCQ2')
            self.assertEqual(detail.status_code, 200)
            self.assertIn(b'Visible invoice detail', detail.data)
            self.assertIn(b'30,00 EUR', detail.data)
            if role == 'submitter':
                self.assertNotIn(b'Open bill workflow', detail.data)
                self.assertEqual(self.client.get(f'/bill/{bill_id}').status_code, 403)
            empty = self.client.get('/budget/bills?year=2026&month=1&category=Services')
            self.assertEqual(empty.status_code, 200)
            self.assertIn(b'No bill payments', empty.data)
        self.assertEqual(self.client.get('/budget/bills/99999?year=2026').status_code, 404)
        deleted = self.bill(10, status='Deleted')
        self.assertEqual(self.client.get(f'/budget/bills/{deleted}?year=2026').status_code, 404)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/budget/bills?year=2026').status_code, 302)
        self.assertEqual(anonymous.get(f'/budget/bills/{bill_id}?year=2026').status_code, 302)

    def test_template_metadata_rejects_wrong_year_or_version(self):
        self.login()
        response = self.client.get('/budget-template.xlsx?year=2026&version=FCQ2')
        workbook = load_workbook(BytesIO(response.data))
        self.assertEqual(workbook['Instructions']['B1'].value, 2026)
        self.assertEqual(workbook['Instructions']['B2'].value, 'FCQ2')
        self.assertTrue(all(row[0].startswith('2026-') for row in workbook['Budget plan'].iter_rows(min_row=2, values_only=True)))
        workbook.close()
        with self.assertRaises(ValueError):
            import_plan(response.data, {'id': 1}, 2026, 'Budget')
        self.assertGreater(import_plan(response.data, {'id': 1}, 2026, 'FCQ2'), 0)

    def test_migrates_existing_plan_to_budget_and_can_run_twice(self):
        with connect() as db:
            db.execute('DROP TABLE budget_plan')
            db.execute('''CREATE TABLE budget_plan(year INTEGER,month INTEGER,category TEXT,currency TEXT,
                amount REAL,updated_at TEXT,updated_by INTEGER,PRIMARY KEY(year,month,category,currency))''')
            db.execute("INSERT INTO budget_plan VALUES(2026,2,'Services','EUR',123,'2026-01-01',1)")
        init_db()
        init_db()
        self.assertEqual(budget_data(2026, 2)['planned'], 123)
        self.import_rows([('2026-02', 'Services', 'EUR', 456)], version='FCQ1')
        self.assertEqual(budget_data(2026, 2)['planned'], 123)

    def reset_link(self, user_id=2):
        self.login('admin')
        response = self.client.post('/users/password-reset-link', data={'user_id': user_id, 'csrf_token': 'test-csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return re.search(r'/reset-password\?token=([A-Za-z0-9_-]+)', response.text).group(1)

    def reset_form(self, client, token):
        response = client.get('/reset-password?token=' + token)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

    def test_password_reset_is_single_use_and_revokes_sessions(self):
        token = self.reset_link()
        with connect() as db:
            stored = db.execute('SELECT token_hash FROM password_resets').fetchone()[0]
            self.assertNotEqual(stored, token)
            self.assertEqual(stored, hashlib.sha256(token.encode()).hexdigest())
        client = self.app.test_client()
        csrf = self.reset_form(client, token)
        self.assertEqual(client.post('/reset-password', data={'reset_token': token}).status_code, 403)
        data = {'reset_token': token, 'csrf_token': csrf, 'password': 'new-password-123', 'confirm': 'new-password-123'}
        self.assertEqual(client.post('/reset-password', data={**data, 'confirm': 'mismatch'}).status_code, 400)
        response = client.post('/reset-password', data=data)
        self.assertEqual(response.status_code, 303)
        with connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=2').fetchone()[0], 0)
        self.assertEqual(client.get('/reset-password?token=' + token).status_code, 404)
        login = client.post('/login', data={'username': 'submitter', 'password': 'new-password-123'})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(client.get('/budget').status_code, 200)

    def test_reset_links_expire_and_are_replaced_or_disabled(self):
        old_token = self.reset_link()
        new_token = self.reset_link()
        client = self.app.test_client()
        self.assertEqual(client.get('/reset-password?token=' + old_token).status_code, 404)
        with connect() as db:
            db.execute("UPDATE password_resets SET expires_at='2000-01-01'")
        self.assertEqual(client.get('/reset-password?token=' + new_token).status_code, 404)
        token = self.reset_link()
        with connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=2')
        self.assertEqual(client.get('/reset-password?token=' + token).status_code, 404)
        self.login('validator')
        self.assertEqual(self.client.post('/users/password-reset-link', data={'user_id': 1, 'csrf_token': 'test-csrf'}).status_code, 403)

    def test_users_change_own_password_with_current_password(self):
        digest, salt = password_record('original-password')
        with connect() as db:
            db.execute('UPDATE users SET password_hash=?,salt=? WHERE id=2', (digest, salt))
        self.login('submitter')
        self.assertEqual(self.client.get('/change-password').status_code, 200)
        data = {'csrf_token': 'test-csrf', 'current_password': 'wrong', 'password': 'changed-password', 'confirm': 'changed-password'}
        self.assertEqual(self.client.post('/change-password', data=data).status_code, 400)
        data['current_password'] = 'original-password'
        self.assertEqual(self.client.post('/change-password', data=data).status_code, 303)
        self.assertEqual(self.client.post('/login', data={'username': 'submitter', 'password': 'original-password'}).status_code, 401)
        self.assertEqual(self.client.post('/login', data={'username': 'submitter', 'password': 'changed-password'}).status_code, 302)


if __name__ == '__main__':
    unittest.main()
