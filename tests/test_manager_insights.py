"""Growth comparisons use disposable synthetic history only."""
from datetime import date
from decimal import Decimal
import unittest
import test_regressions as fixtures
from growth_insights import growth_insights

class GrowthInsightsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BillingRegressionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def add(self, day, channel='Walk-in', customer=1, **kwargs):
        b=self.f.bill(orderType=channel,**kwargs)
        if customer!=1:
            self.f.sql("INSERT OR IGNORE INTO customers(id,name,phone) VALUES (?,'Another Synthetic',?)",(customer,str(9000000000+customer)))
        self.f.sql('UPDATE bills SET date=?, customer_id=? WHERE id=?',(day,customer,b))
        return b

    def data(self):
        with fixtures.application.app.test_request_context('/'):
            conn=fixtures.application.get_db_connection()
            return growth_insights(conn,date(2026,10,29))

    def test_exact_fifteen_day_windows_and_zero_days(self):
        for day in ['2026-09-29','2026-09-30','2026-10-14','2026-10-15','2026-10-29','2026-10-30']:self.add(day)
        d=self.data()
        self.assertEqual((d['current']['count'],d['previous']['count']),(2,2))
        self.assertEqual((d['current']['start'],d['previous']['start']),(date(2026,10,15),date(2026,9,30)))
        self.assertEqual(len(d['daily']),30)
        self.assertEqual(d['daily'][1]['Walk-in'],0)

    def test_channel_full_value_not_wallet_or_advance(self):
        self.f.wallet(200)
        self.add('2026-10-29','Online',advancePaid='100',discountType='Rs',discountValue='50')
        self.add('2026-10-29','Walk-in')
        self.add('2026-10-29','Unknown')
        c=self.data()['current']['channels']
        self.assertEqual([(x['count'],x['sales'],x['average']) for x in c],[(1,Decimal('450'),Decimal('450')),(1,Decimal('500'),Decimal('500')),(1,Decimal('500'),Decimal('500'))])

    def test_new_repeat_unique_and_sales_classification(self):
        self.add('2026-09-01',customer=1)
        self.add('2026-10-20',customer=1)
        self.add('2026-10-20',customer=2)
        self.add('2026-10-21',customer=2)
        c=self.data()['current']['cohorts']
        self.assertEqual([(x['customers'],x['sales']) for x in c],[(1,Decimal('1000')),(1,Decimal('500'))])
        self.assertAlmostEqual(c[0]['share'],50)
        self.assertAlmostEqual(c[0]['sales_share'],100*2/3)

    def test_customer_can_be_new_previous_and_repeat_current(self):
        self.add('2026-10-01');self.add('2026-10-20')
        d=self.data();self.assertEqual(d['previous']['cohorts'][0]['customers'],1)
        self.assertEqual(d['current']['cohorts'][1]['customers'],1)

    def test_cancelled_first_bill_not_used_as_history(self):
        b=self.add('2026-09-01');self.f.sql('UPDATE bills SET void=1 WHERE id=?',(b,))
        self.add('2026-10-20');d=self.data()
        self.assertEqual(d['current']['cohorts'][0]['customers'],1)
        self.assertEqual(d['current']['sales'],500)

    def test_service_allocation_reconciles_to_full_bill_value(self):
        b=self.add('2026-10-20',discountValue='50',express_service='on',surchargeValue='30')
        self.f.sql("INSERT INTO bill_items(bill_id,service_name,qty,rate,total,items) VALUES (?,'Extra',1,100,100,1)",(b,))
        d=self.data()['current'];self.assertEqual(sum(d['services'].values()),d['sales'])
        self.assertEqual(len(d['services']),2)

    def test_service_quantities_do_not_split_the_same_service(self):
        self.add('2026-10-20',**{'qty[]':['1']})
        self.add('2026-10-21',**{'qty[]':['2']})
        d=self.data()['current']
        self.assertEqual(list(d['services']),['Wash KG'])
        self.assertEqual(d['services']['Wash KG'],1500)

    def test_weekdays_average_by_occurrences(self):
        self.add('2026-10-15');self.add('2026-10-22');self.add('2026-10-29')
        thu=self.data()['current']['weekdays'][3]
        self.assertEqual((thu['count'],thu['average']),(3,1))

    def test_retention_matured_cohorts_boundary_and_recent_exclusion(self):
        self.add('2026-10-14',customer=1);self.add('2026-10-29',customer=1)
        self.add('2026-10-13',customer=2);self.add('2026-10-29',customer=2)
        self.add('2026-10-15',customer=3);self.add('2026-10-16',customer=3)
        d=self.data()['retention_current'];self.assertEqual((d['total'],d['returned'],d['rate']),(2,1,50))

    def test_empty_invalid_and_future_dates_no_division_by_zero(self):
        d=self.data();self.assertIsNone(d['sales_change']);self.assertIsNone(d['retention_current']['rate'])
        self.add('bad-date');self.add('2026-12-01')
        d=self.data();self.assertEqual((d['invalid_dates'],d['current']['count']),(1,0))

    def test_page_aggregate_private_and_authenticated(self):
        self.add('2026-10-29')
        r=self.f.client.get('/insights');self.assertEqual(r.status_code,200)
        html=r.get_data(as_text=True)
        for s in ['Daily sales','New vs repeat','Service mix','Previous 15']:self.assertIn(s,html)
        for s in ['Synthetic Customer','9000000000','Loading...</p>','Store pulse']:self.assertNotIn(s,html)
        self.assertEqual(fixtures.application.app.test_client().get('/insights').status_code,302)

    def test_home_links_to_insights_and_reports_do_not_load_analytics(self):
        from unittest.mock import patch
        home=self.f.client.get('/').get_data(as_text=True)
        self.assertIn('href="/insights"',home)
        with patch.object(fixtures.application,'growth_insights',side_effect=AssertionError('Reports must not calculate insights')):
            report=self.f.client.get('/reports')
        self.assertEqual(report.status_code,200)
        self.assertNotIn('Business growth overview',report.get_data(as_text=True))
        insights=self.f.client.get('/insights').get_data(as_text=True)
        self.assertIn('href="/insights"',insights)
        self.assertIn('chart-scroll',insights)
        self.assertIn('channel-cards',insights)
        self.assertNotIn('href="/reports"',insights)

    def test_old_negative_line_preserves_repeat_history_without_crashing(self):
        old=self.add('2026-09-01')
        self.f.sql('UPDATE bill_items SET total=-50 WHERE bill_id=?',(old,))
        self.add('2026-10-29')
        d=self.data()
        self.assertEqual(d['current']['sales'],500)
        self.assertEqual(d['current']['cohorts'][1]['customers'],1)
        self.assertEqual(d['invalid_amounts'],0)

    def test_invalid_period_amounts_are_flagged_but_counts_and_history_remain(self):
        bad=self.add('2026-10-20')
        self.f.sql('UPDATE bill_items SET total=-50 WHERE bill_id=?',(bad,))
        self.add('2026-10-21')
        d=self.data()
        self.assertEqual((d['invalid_amounts'],d['current']['count'],d['current']['sales']),(1,2,500))
        self.assertEqual(d['current']['channels'][1]['average'],500)
        self.assertEqual(d['current']['cohorts'][0]['customers'],1)
        self.assertEqual(sum(d['current']['services'].values()),500)
        html=self.f.client.get('/insights').get_data(as_text=True)
        self.assertIn('Sales data incomplete',html)
        self.assertEqual(self.f.sql('SELECT total FROM bill_items WHERE bill_id=?',(bad,))[0][0],-50)

    def test_nonfinite_nonnumeric_and_oversized_amounts_do_not_crash(self):
        for value in ['not-an-amount','inf',1000000001]:
            b=self.add('2026-10-20');self.f.sql('UPDATE bill_items SET total=? WHERE bill_id=?',(value,b))
        d=self.data()
        self.assertEqual(d['invalid_amounts'],3)
        self.assertEqual(d['current']['sales'],0)
        self.assertEqual(self.f.client.get('/insights').status_code,200)

    def test_reads_leave_business_records_unchanged(self):
        self.add('2026-10-29');before=[tuple(r) for r in self.f.sql('SELECT * FROM bills')]
        self.data();self.assertEqual(before,[tuple(r) for r in self.f.sql('SELECT * FROM bills')])

if __name__=='__main__':unittest.main()
