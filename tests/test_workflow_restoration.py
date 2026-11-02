"""Regression coverage for pipeline, bank overview, matching, and unread notes."""
import unittest
from unittest.mock import patch

import test_budget_reports as fixtures
from expense_pilot.bank_views import bank_forecast, suggested_matches
from expense_pilot.comments import add_bill_comment, mark_bill_comments_read, notification_counts
from expense_pilot.database import connect
from expense_pilot.web import render_page


class WorkflowRestorationTest(unittest.TestCase):
    setUp = fixtures.BudgetReportsTest.setUp
    login = fixtures.BudgetReportsTest.login
    bill = fixtures.BudgetReportsTest.bill
    allocation = fixtures.BudgetReportsTest.allocation

    def test_dashboard_keeps_pending_partial_and_more_than_eight_bills(self):
        for number in range(12):
            bill_id = self.bill(100, status='Pending for approval')
            with connect() as db:
                db.execute('UPDATE bills SET invoice_number=? WHERE id=?', (f'PIPE-{number}', bill_id))
        partial = self.bill(100, status='Order of payment issued')
        self.allocation(partial, 40, '2026-10-01')
        other = self.bill(100, status='Needs review', created_by=1)
        with connect() as db:
            db.execute("UPDATE bills SET invoice_number='OTHER-OWNER' WHERE id=?", (other,))
            db.execute("UPDATE bills SET invoice_number='PARTIAL-ORDER' WHERE id=?", (partial,))
        self.login('submitter')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Bills in the pipeline', response.data)
        for number in range(12):
            self.assertIn(f'PIPE-{number}'.encode(), response.data)
        self.assertIn(b'PARTIAL-ORDER', response.data)
        self.assertNotIn(b'OTHER-OWNER', response.data)

    def test_default_bank_overview_and_global_pipeline_forecast(self):
        self.bill(100, status='Pending for approval')
        self.bill(200, status='Ready for payment')
        order = self.bill(300, status='Order of payment issued')
        self.allocation(order, 50, '2026-10-01')
        self.bill(999, status='Paid at bank')
        self.bill(999, status='Rejected')
        with connect() as db:
            db.execute("INSERT INTO bank_snapshots(account_name,as_of_date,closing_balance,currency,created_at) VALUES('Main','2026-10-01',2000,'EUR','2026-10-01')")
            db.execute("INSERT INTO bank_snapshots(account_name,as_of_date,closing_balance,currency,created_at) VALUES('Main','2026-09-01',9000,'EUR','2026-09-01')")
            summary = bank_forecast(db)[0]
        self.assertEqual(summary['pipeline'], 550)
        self.assertEqual(summary['balance'], 2000)
        self.assertEqual(summary['forecast'], 1450)
        self.login()
        response = self.client.get('/reconcile?from=2020-01-01&to=2020-01-02')
        self.assertEqual(response.status_code, 200)
        for text in (b'Money in', b'Money out', b'validator-ledger-columns', b'Forecast after all pipeline payments', b'1.450,00'):
            self.assertIn(text, response.data)
        matching = self.client.get('/reconcile?view=matching')
        self.assertIn(b'Current bank situation', matching.data)
        self.assertIn(b'Match from statement', matching.data)

    def test_only_one_or_two_supported_matches(self):
        unrelated = dict(reference_match=False, invoice_match=False, amount_difference=0, name_score=0.1, day_gap=0)
        good = dict(unrelated, name_score=0.9)
        reference = dict(unrelated, reference_match=True, amount_difference=20, day_gap=100)
        self.assertEqual(suggested_matches([unrelated]), [])
        self.assertEqual(suggested_matches([unrelated, good]), [good])
        self.assertEqual(suggested_matches([good, reference]), [reference])
        self.assertEqual(len(suggested_matches([dict(good, day_gap=i) for i in range(25)])), 2)

    def test_matching_screen_has_simple_suggestions_and_optional_split(self):
        bill_id = self.bill(100, status='Order of payment issued')
        with connect() as db:
            db.execute("UPDATE bills SET payment_reference='12345678',payment_model='HR00' WHERE id=?", (bill_id,))
            db.execute("INSERT INTO transactions(txn_date,description,amount,reference,currency,direction) VALUES('2026-10-01','Supplier',100,'HR00 12345678','EUR','outgoing')")
        self.login()
        with patch('expense_pilot.bank_views.render_page', wraps=render_page) as renderer:
            response = self.client.get('/reconcile?view=matching')
            entry = renderer.call_args.kwargs['entries'][0]
            self.assertEqual(len(entry['candidates']), 1)
            self.assertEqual(entry['candidates'][0]['bill']['id'], bill_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Confirm payment match', response.data)
        self.assertIn(b'Split payment or choose another bill', response.data)

    def test_wall_badge_counts_every_visible_unread_note_until_bill_opened(self):
        bill_id = self.bill(50, status='Ready for payment', created_by=2)
        with connect() as db:
            add_bill_comment(db, bill_id, {'id': 1, 'username': 'admin'}, 'New note from administrator')
            add_bill_comment(db, bill_id, {'id': 3, 'username': 'validator'}, 'New note from validator')
        # Validators previously missed notes from administrators.
        self.assertEqual(notification_counts({'id': 3, 'role': 'validator'})['notes'], 1)
        self.login('submitter')
        response = self.client.get('/notifications')
        self.assertEqual(response.json['notes'], 2)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn(b'data-unread-notes', self.client.get('/').data)
        self.assertEqual(self.client.get('/wall').status_code, 200)
        self.assertEqual(self.client.get('/notifications').json['notes'], 2)
        with connect() as db:
            mark_bill_comments_read(db, bill_id, {'id': 2})
        self.assertEqual(self.client.get('/notifications').json['notes'], 0)
        self.assertEqual(notification_counts({'id': 3, 'role': 'validator'})['notes'], 1)


if __name__ == '__main__':
    unittest.main()
