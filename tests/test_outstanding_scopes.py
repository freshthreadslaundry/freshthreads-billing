"""Date boundaries and money totals use temporary synthetic databases only."""
from datetime import datetime
import unittest
from unittest.mock import patch
import test_regressions as fixtures


class OutstandingScopesTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BillingRegressionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        fixtures.application.app.config['OUTSTANDING_PASSWORD_REQUIRED']=False

    def bill(self,due,amount='500',paid=False,advance='0'):
        return self.f.bill(deliveryDate=due,paid=paid,advancePaid=advance,**{'rate[]':[amount]})

    def rows(self,scope=None):
        path='/api/outstanding-report'+('' if scope is None else '?scope='+scope)
        response=self.f.client.get(path)
        self.assertEqual(response.status_code,200)
        return response.get_json()

    def summary(self,scope=None):
        path='/outstanding_summary'+('' if scope is None else '?scope='+scope)
        response=self.f.client.get(path)
        self.assertEqual(response.status_code,200)
        return response.get_json()

    def test_default_is_strictly_after_may_31(self):
        old=self.bill('2026-05-30');boundary=self.bill('2026-05-31')
        june=self.bill('2026-06-01');future=self.bill('2027-01-01')
        self.assertEqual({r['bill_id'] for r in self.rows()},{june,future})
        self.assertEqual({r['bill_id'] for r in self.rows('arrears')},{old,boundary})

    def test_filter_uses_delivery_date_not_bill_creation(self):
        current=self.bill('2026-06-01');old=self.bill('2026-05-31')
        self.f.sql("UPDATE bills SET date='2020-01-01 10:00:00' WHERE id=?",(current,))
        self.f.sql("UPDATE bills SET date='2030-01-01 10:00:00' WHERE id=?",(old,))
        self.assertEqual([r['bill_id'] for r in self.rows()],[current])
        self.assertEqual([r['bill_id'] for r in self.rows('arrears')],[old])

    def test_summary_counts_and_amounts_match_each_scope(self):
        self.bill('2026-05-30','100',advance='25')
        self.bill('2026-05-31','200')
        self.bill('2026-05-20','50',paid=True)
        self.bill('2026-06-01','300',advance='50')
        self.bill('2026-06-02','400',paid=True)
        current=self.summary();arrears=self.summary('arrears')
        self.assertEqual((current['outstanding_bill_count'],current['pending_payment_count'],current['pending_amount']),(2,1,'250.00'))
        self.assertEqual((arrears['outstanding_bill_count'],arrears['pending_payment_count'],arrears['pending_amount']),(3,2,'275.00'))
        self.assertEqual(current['scope'],'current');self.assertEqual(arrears['scope'],'arrears')
        self.assertEqual(current['cutoff'],'2026-05-31')
        for scope in ('current','arrears'):
            self.assertEqual(float(self.summary(scope)['pending_amount']),sum(row['balance_amount'] for row in self.rows(scope)))

    def test_paid_undelivered_bills_remain_with_zero_due(self):
        old=self.bill('2026-05-31',paid=True)
        current=self.bill('2026-06-01',paid=True)
        self.assertEqual([(r['bill_id'],r['balance_amount']) for r in self.rows('arrears')],[(old,0)])
        self.assertEqual([(r['bill_id'],r['balance_amount']) for r in self.rows()],[(current,0)])
        self.assertEqual(self.summary('arrears')['pending_payment_count'],0)

    def test_cancelled_and_fully_settled_bills_excluded(self):
        for due in ('2026-05-31','2026-06-01'):
            cancelled=self.bill(due)
            self.f.post('/api/cancel-bill',json={'bill_id':cancelled})
            settled=self.bill(due,paid=True)
            self.f.post('/api/mark-delivered',json={'bill_id':settled})
        self.assertEqual(self.rows(),[]);self.assertEqual(self.rows('arrears'),[])
        self.assertEqual(self.summary()['pending_amount'],'0.00')
        self.assertEqual(self.summary('arrears')['pending_amount'],'0.00')

    def test_missing_dates_remain_accessible_separately(self):
        ids=[self.bill(value) for value in (None,'','not-a-date')]
        self.assertEqual(self.rows(),[]);self.assertEqual(self.rows('arrears'),[])
        self.assertEqual({r['bill_id'] for r in self.rows('undated')},set(ids))
        self.assertEqual(self.summary()['undated_bill_count'],3)
        self.assertEqual(self.summary('arrears')['undated_bill_count'],3)
        self.assertEqual(self.summary('undated')['pending_amount'],'1500.00')

    def test_boundary_includes_entire_may_31(self):
        boundary=self.bill('2026-05-31 23:59:59')
        june=self.bill('2026-06-01 00:00:00')
        self.assertEqual([r['bill_id'] for r in self.rows('arrears')],[boundary])
        self.assertEqual([r['bill_id'] for r in self.rows()],[june])

    def test_invalid_scope_cannot_return_unfiltered_data(self):
        self.bill('2026-06-01')
        for scope in ('all','bad',''):
            for route in ('/api/outstanding-report','/outstanding_summary'):
                self.assertEqual(self.f.client.get(route+'?scope='+scope).status_code,400)

    def test_all_scopes_require_staff_login(self):
        public=fixtures.application.app.test_client()
        for scope in ('current','arrears','undated'):
            for route in ('/api/outstanding-report','/outstanding_summary'):
                self.assertEqual(public.get(route+'?scope='+scope).status_code,401)

    def test_optional_report_password_still_covers_arrears_and_undated(self):
        fixtures.application.app.config['OUTSTANDING_PASSWORD_REQUIRED']=True
        for scope in ('current','arrears','undated'):
            self.assertEqual(self.f.client.get('/api/outstanding-report?scope='+scope).status_code,403)

    def test_paid_and_delivered_actions_update_arrears_not_current(self):
        bid=self.bill('2026-05-31','100')
        self.assertEqual(self.summary('arrears')['pending_amount'],'100.00')
        self.f.post('/api/mark-paid',json={'bill_id':bid})
        self.assertEqual(self.summary('arrears')['pending_amount'],'0.00')
        self.assertEqual(self.summary('arrears')['outstanding_bill_count'],1)
        self.assertEqual(self.rows(),[])
        self.f.post('/api/mark-delivered',json={'bill_id':bid})
        self.assertEqual(self.rows('arrears'),[])

    def test_multiple_line_items_do_not_duplicate_bills_or_money(self):
        bid=self.bill('2026-05-31','100')
        self.f.sql("INSERT INTO bill_items(bill_id,service_id,service_name,qty,rate,total,items) VALUES (?,1,'Extra item',2,0,0,2)",(bid,))
        rows=self.rows('arrears')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['items'],5)
        self.assertEqual(self.summary('arrears')['pending_amount'],'100.00')
        self.assertEqual(self.summary('arrears')['outstanding_bill_count'],1)

    def test_today_and_tomorrow_delivery_counts_are_scope_specific(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,6,10,12,0,0)
        with patch.object(fixtures.application,'datetime',Clock):
            self.bill('2026-06-10');self.bill('2026-06-11');self.bill('2026-05-31')
            current=self.summary();arrears=self.summary('arrears')
        self.assertEqual((current['pending_today'],current['pending_tomorrow']),(1,1))
        self.assertEqual((arrears['pending_today'],arrears['pending_tomorrow']),(0,0))

    def test_reading_reports_does_not_change_old_bills(self):
        self.bill('2026-05-31');self.bill('2026-06-01');self.bill(None)
        def snapshot():
            return [[tuple(row) for row in self.f.sql('SELECT * FROM '+table+' ORDER BY 1')] for table in ('bills','bill_items','bill_status','subscription_ledger')]
        before=snapshot()
        for scope in ('current','arrears','undated'):self.rows(scope);self.summary(scope)
        self.assertEqual(snapshot(),before)


if __name__=='__main__':unittest.main()
