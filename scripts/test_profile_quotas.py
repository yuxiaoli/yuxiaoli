import datetime as dt
import unittest
import io
import json
from unittest.mock import patch
import xml.etree.ElementTree as ET

from profile_quotas import UTC, cloudflare, github, paid_window, query, render, safe

NOW = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)
CF = {'plan':'paid','account':'f'*32,'start':'2026-11-15T00:00:00Z','reset':'2026-12-15T00:00:00Z'}
GH = {'start':'2026-11-02T00:00:00Z','reset':'2026-12-02T00:00:00Z','observed_at':'2026-11-23T00:00:00Z','allowance_percent':0,'runner_minutes':0}

class Quotas(unittest.TestCase):
    def test_zero_and_unknown_are_distinct(self):
        svg = render([('Actions', github(GH,NOW)),('Unknown',None)],NOW)
        ET.fromstring(svg)
        self.assertIn('0.0%',svg)
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
        self.assertIn('resets in 1 day',svg)
        self.assertNotIn('12h',svg)
        for value in ('f'*32,'private-token','2026-11-15','2026-12-15'):
            self.assertNotIn(value,svg)

    def test_invalid_fails_closed(self):
        self.assertIsNone(safe(github,{**GH,'allowance_percent':101},NOW))
        self.assertIsNone(safe(github,{**GH,'observed_at':'2026-11-01T00:00:00Z'},NOW))
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
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
                self.assertEqual(safe(query,'q',{},'token','dataset','requests'),expected)

    def test_reset_is_always_in_days(self):
        self.assertIn('resets in 9 days',render([('Actions',github(GH,NOW))],NOW))

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
