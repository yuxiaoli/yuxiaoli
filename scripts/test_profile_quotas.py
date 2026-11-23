import datetime as dt
import unittest
import io
import json
from unittest.mock import Mock, patch
import profile_quotas as quotas
import xml.etree.ElementTree as ET

from profile_quotas import UTC, cloudflare, countdown_label, github_snapshot, paid_window, profile_rows, publish, query, render, safe

NOW = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)
CF = {'plan':'paid','account':'f'*32,'start':'2026-11-15T00:00:00Z','reset':'2026-12-15T00:00:00Z'}
GH = {'start':'2026-11-01T00:00:00Z','reset':'2026-12-01T00:00:00Z','observed_at':'2026-11-23T00:00:00Z','allowance_percent':0,'runner_minutes':0}

class Quotas(unittest.TestCase):
    def test_zero_and_unknown_are_distinct(self):
        svg = render([('Actions', github_snapshot(GH,NOW)),('Unknown',None)],NOW)
        ET.fromstring(svg)
        self.assertIn('0.00%',svg)
        self.assertIn('Unavailable',svg)
        self.assertIn('width="0.00"',svg)

    def test_over_cap_clamps_width(self):
        row = cloudflare(CF,NOW,'secret','cpu',get=lambda *_: 30_000)
        svg = render([('Container',row)],NOW)
        self.assertIn('500.0 / 375',svg)
        self.assertIn('width="452.00"',svg)

    def test_private_input_never_rendered(self):
        row = cloudflare({**CF,'plan':'free'},NOW,'private-token','kv',get=lambda *_:25)
        svg = render([('KV',row)],NOW)
        self.assertIn('25 / 1,000',svg)
        self.assertIn('resets in 12 hours',svg)
        for value in ('f'*32,'private-token','2026-11-15','2026-12-15'):
            self.assertNotIn(value,svg)

    def test_invalid_fails_closed(self):
        self.assertIsNone(safe(github_snapshot,{**GH,'allowance_percent':101},NOW))
        self.assertIsNone(safe(github_snapshot,{**GH,'observed_at':'2026-10-31T00:00:00Z'},NOW))
        self.assertIsNone(safe(cloudflare,CF,NOW,'','cpu'))
        self.assertIsNone(safe(cloudflare,{**CF,'plan':'unknown'},NOW,'secret','kv'))

    def test_provider_failure_is_independent(self):
        def blocked(*_):
            raise ValueError('private provider payload')
        self.assertIsNone(safe(cloudflare,CF,NOW,'secret','cpu',blocked))
        self.assertEqual(cloudflare(CF,NOW,'secret','kv',lambda *_:0)[1],1_000_000)

    def test_empty_analytics_is_unknown_but_explicit_zero_is_valid(self):
        for rows, expected in (([],None),([{'sum':{'requests':0}}],0)):
            payload = {'data':{'viewer':{'accounts':[{'dataset':rows}]}}}
            with patch('profile_quotas.OPEN',return_value=io.BytesIO(json.dumps(payload).encode())):
                self.assertEqual(safe(query,'q',{},'token','dataset','requests'),expected)

    def test_long_reset_is_in_days(self):
        self.assertIn('resets in 8 days',render([('Cloudflare', (0, 1, 'fixture', NOW + dt.timedelta(days=8)))],NOW))

    def test_paid_anchor_handles_year_rollover(self):
        anchor = {'billing_day': 15}
        for month, year, expect in ((12,2026,(12,2026,1,2027)),(1,2027,(1,2027,2,2027))):
            start, end = paid_window(anchor,dt.datetime(year,month,15,tzinfo=UTC))
            self.assertEqual((start.month,start.year,end.month,end.year),expect)
        start, end = paid_window(anchor,dt.datetime(2027,1,1,tzinfo=UTC))
        self.assertEqual((start.month,start.year,end.month,end.year),(12,2026,1,2027))
        self.assertEqual(paid_window(CF,NOW)[0].day,15)
        self.assertIsNone(safe(paid_window,{'billing_day':29},NOW))
        self.assertIsNone(safe(paid_window,{'billing_day':15,'start':CF['start']},NOW))

    def test_quota_layout_preserves_track_width_and_avoids_clipping(self):
        reads = (51, 100_000, '51 / 100,000 reads (analytics)', NOW + dt.timedelta(hours=8))
        writes = (25, 1_000, '25 / 1,000 writes (analytics)', NOW + dt.timedelta(hours=8))
        browser = (1.2, 4, '1.2 / 4 hours', NOW + dt.timedelta(hours=8))
        rows = profile_rows(github_snapshot(GH,NOW),None,writes,kv_reads=reads,browser_run=browser)
        root = ET.fromstring(render(rows,NOW))
        self.assertEqual(root.attrib['viewBox'],'0 0 480 371')
        self.assertEqual(root.attrib['width'],'480')
        tracks = [rect for rect in root.findall('{*}rect') if rect.get('opacity') == '.30']
        self.assertEqual([(int(r.get('x')),int(r.get('y')),int(r.get('width')),int(r.get('height'))) for r in tracks],
                         [(14,80,452,11),(14,145,452,11),(14,210,452,11),(14,275,452,11),(14,340,452,11)])
        self.assertEqual(int(root.get('height'))-int(tracks[-1].get('y'))-int(tracks[-1].get('height')),20)
        details = [int(t.get('y')) for t in root.findall('{*}text') if t.get('class')=='muted']
        self.assertEqual(details,[72,137,202,267,332])

    def test_countdown_uses_rounded_hours_through_24(self):
        for delta, expected in ((dt.timedelta(seconds=1),'1 hour'),
                                (dt.timedelta(hours=23,seconds=1),'24 hours'),
                                (dt.timedelta(hours=24),'24 hours'),
                                (dt.timedelta(hours=24,seconds=1),'2 days')):
            self.assertEqual(countdown_label(NOW+delta,NOW),expected)

    def test_provider_colors_and_independent_kv_labels(self):
        reset = NOW+dt.timedelta(hours=6)
        rows = [('GitHub Actions',(10,100,'10%',reset)),
                ('Cloudflare KV reads',(25,100_000,'25 / 100,000 reads',reset)),
                ('Cloudflare KV writes',(10,1_000,'10 / 1,000 writes',reset))]
        svg = render(rows,NOW)
        self.assertEqual(svg.count('fill="#40c463"'),1)
        self.assertEqual(svg.count('fill="#056dff"'),2)
        self.assertIn('25 / 100,000 reads',svg)
        self.assertIn('10 / 1,000 writes',svg)

    def test_browser_run_is_independent_and_defaults_unavailable(self):
        unknown = render(profile_rows(None,None,None),NOW)
        self.assertIn('Cloudflare Browser Run',unknown)
        self.assertEqual(unknown.count('Unavailable'),5)
        reset = NOW+dt.timedelta(hours=9)
        live = render(profile_rows(None,None,None,browser_run=(1,4,'1 / 4 hours',reset)),NOW)
        self.assertIn('1 / 4 hours | resets in 9 hours',live)
        self.assertIn('fill="#056dff"',live)
        self.assertEqual(live.count('Unavailable'),4)

    def test_kv_requests_and_caps_are_independent(self):
        def stub(query_text,args,token,key,field):
            self.assertEqual((key,field),('kvOperationsAdaptiveGroups','requests'))
            return 120 if 'actionType:"read"' in query_text else 15

        for plan, caps in (('free',(100_000,1_000)),('paid',(10_000_000,1_000_000))):
            cf = {**CF,'plan':plan}
            r = cloudflare(cf,NOW,'secret','kv_reads',get=stub)
            w = cloudflare(cf,NOW,'secret','kv',get=stub)
            self.assertEqual((r[0],w[0]),(120,15))
            self.assertEqual((r[1],w[1]),caps)

    def test_incomplete_card_never_writes(self):
        p = Mock()
        self.assertFalse(publish(profile_rows(None,None,None),NOW,p))
        p.write_text.assert_not_called()
        r = (1,4,'Synthetic',NOW+dt.timedelta(hours=6))
        self.assertTrue(publish(profile_rows(r,r,r,kv_reads=r,browser_run=r),NOW,p))
        self.assertNotIn('Unavailable',p.write_text.call_args.args[0])

    def test_browser_settings_and_exact_range(self):
        b = {'enabled':True,'maxDuration':32*86400,'notOlderThan':32*86400,
             'maxPageSize':1000,'availableFields':['sum_totalSessionDurationMs']}
        with patch('profile_quotas.graphql_account',return_value={'settings':{'browserRenderingBrowserTimeUsageAdaptiveGroups':b}}):
            quotas.browser_entitled('t','f'*32,31*86400)
            self.assertIsNone(safe(quotas.browser_entitled,'t','f'*32,33*86400))
        def stub(q,args,token,key,field):
            self.assertEqual((key,field),('browserRenderingBrowserTimeUsageAdaptiveGroups','totalSessionDurationMs'))
            self.assertIn('datetime_lt:$end',q)
            self.assertEqual(args['start'],'2026-11-15T00:00:00Z')
            self.assertEqual(args['end'],'2026-11-23T12:00:00Z')
            return 750000
        result = cloudflare(CF,NOW,'t','browser',stub,Mock())
        self.assertEqual(result[:3],(12.5,600,'12.5 / 600 browser min (analytics)'))
        self.assertIsNone(safe(cloudflare,{**CF,'plan':'free'},NOW,'t','browser',stub,Mock()))

    def test_github_snapshot_is_labeled_before_and_after_reset(self):
        snapshot = quotas.GITHUB_BILLING_SNAPSHOT
        for now in [NOW, dt.datetime(2026, 12, 5, tzinfo=UTC)]:
            row = github_snapshot(snapshot, now)
            svg = render(profile_rows(row, None, None), now)
            self.assertIn('71.39% used; 3,139 metered min', svg)
            self.assertIn('Nov 2026 as of Nov 23, 04:18 UTC', svg)
            self.assertNotIn('resets in', svg)
        self.assertIsNone(safe(github_snapshot, snapshot, dt.datetime(2026, 11, 1, tzinfo=UTC)))

    def test_rollover_unknown_keeps_last_dated_card(self):
        old = dt.datetime(2026, 11, 23, 23, 55, tzinfo=UTC)
        new = dt.datetime(2026, 11, 24, 0, 5, tzinfo=UTC)
        for now, expected_start in [(old, 10), (new, 11)]:
            self.assertEqual(paid_window({'billing_day':24}, now)[0].month, expected_start)
        cf_row = (1, 10, 'fixture', new)
        old_rows = profile_rows(github_snapshot(quotas.GITHUB_BILLING_SNAPSHOT, old),
                                cf_row, cf_row, kv_reads=cf_row, browser_run=cf_row)
        path = Mock()
        self.assertTrue(publish(old_rows, old, path))
        self.assertIn('Cloudflare as of Nov 23, 23:55 UTC', path.write_text.call_args.args[0])
        unknown = [(name, None if name == 'Cloudflare Containers' else row) for name, row in old_rows]
        self.assertFalse(publish(unknown, new, path))
        self.assertEqual(path.write_text.call_count, 1)

    def test_cpu_query_groups_and_sorts_by_utc_date(self):
        self.assertIn('dimensions { date }',quotas.CPU)
        self.assertIn('orderBy:[date_ASC]',quotas.CPU)
        self.assertIn('date_geq:$start,date_leq:$end',quotas.CPU)
