import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import urllib.request

UTC = dt.timezone.utc
CF = 'https://api.cloudflare.com/client/v4/graphql'
# Owner's signed-in GitHub Billing Overview and Actions Usage reading.
# Both values are frozen at the same observation, not sampled by this workflow.
GITHUB_BILLING_SNAPSHOT = {
    'start': '2026-11-01T00:00:00Z',
    'reset': '2026-12-01T00:00:00Z',
    'observed_at': '2026-11-23T04:18:00Z',
    'allowance_percent': 71.39,
    'runner_minutes': 3139,
}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPEN = urllib.request.build_opener(NoRedirect()).open
CPU = '''query($account: String, $start: Time, $end: Time) {
  viewer { accounts(filter:{accountTag:$account}) {
    containersUsageAdaptiveGroups(limit:100,
      filter:{date_geq:$start,date_leq:$end},orderBy:[date_ASC]) { dimensions { date } sum { cpuTimeSec } }
  } }
}'''
KV = '''query($account: string!, $start: Date, $end: Date) {
  viewer { accounts(filter:{accountTag:$account}) {
    kvOperationsAdaptiveGroups(limit:100,
      filter:{date_geq:$start,date_leq:$end,actionType:"write"}) { sum { requests } }
  } }
}'''

BROWSER = '''query($account: string!, $start: Time, $end: Time) {
  viewer { accounts(filter:{accountTag:$account}) {
    browserRenderingBrowserTimeUsageAdaptiveGroups(limit:100,
      filter:{datetime_geq:$start,datetime_lt:$end}) {
        dimensions { date } sum { totalSessionDurationMs }
    }
  } }
}'''

def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0

def when(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError()
    return dt.datetime.fromisoformat(value[:-1] + '+00:00')

def window(item, now):
    start, reset = when(item['start']), when(item['reset'])
    if not start <= now < reset or reset - start > dt.timedelta(days=32):
        raise ValueError()
    return start, reset

def paid_window(item, now):
    if 'start' in item or 'reset' in item:
        return window(item, now)
    day = item['billing_day']
    if type(day) is not int or not 1 <= day <= 28:
        raise ValueError()
    month = now.year * 12 + now.month - 1

    def boundary(index):
        year, offset = divmod(index, 12)
        return dt.datetime(year, offset + 1, day, tzinfo=UTC)

    if now < boundary(month):
        month -= 1
    return boundary(month), boundary(month + 1)

def github_snapshot(item, now):
    start, reset, observed = (when(item[key]) for key in ('start', 'reset', 'observed_at'))
    first = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (first + dt.timedelta(days=32)).replace(day=1)
    pct, minutes = item['allowance_percent'], item['runner_minutes']
    if (start, reset) != (first, next_month) or not start <= observed < reset or observed > now:
        raise ValueError()
    if not number(pct) or pct > 100 or not number(minutes):
        raise ValueError()
    stamp = f'{observed:%b} {observed.day}, {observed:%H:%M} UTC'
    label = f'{pct:.2f}% used; {minutes:,.0f} metered min | {start:%b %Y} as of {stamp}'
    return pct, 100, label, None


def graphql_account(body, args, token):
    req = urllib.request.Request(CF, data=json.dumps({'query':body,'variables':args}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    with OPEN(req, timeout=12) as res:
        payload = json.loads(res.read())
    if payload.get('errors'):
        raise ValueError()
    accounts = payload['data']['viewer']['accounts']
    if not isinstance(accounts,list) or len(accounts) != 1:
        raise ValueError()
    return accounts[0]


def query(body, args, token, key, field):
    rows = graphql_account(body,args,token)[key]
    if not isinstance(rows,list) or not rows or len(rows) >= 100:
        raise ValueError()
    values = [r['sum'][field] for r in rows]
    if not all(map(number, values)):
        raise ValueError()
    result = sum(values)
    if not number(result):
        raise ValueError()
    return result

def browser_entitled(token, account, elapsed):
    settings = graphql_account('''query($account: string!) {
      viewer { accounts(filter:{accountTag:$account}) { settings {
        browserRenderingBrowserTimeUsageAdaptiveGroups {
          enabled maxDuration notOlderThan maxPageSize availableFields
        }
      } } }
    }''',{'account':account},token)['settings']['browserRenderingBrowserTimeUsageAdaptiveGroups']
    if (settings['enabled'] is not True or
        'sum_totalSessionDurationMs' not in settings['availableFields'] or
        not all(type(settings[k]) is int and settings[k]>=elapsed
                for k in ('maxDuration','notOlderThan')) or
        type(settings['maxPageSize']) is not int or settings['maxPageSize']<100):
        raise ValueError()


def cloudflare(item, now, token, kind, get=query, require_browser=browser_entitled):
    plan = item['plan']
    if not token or plan not in ('free', 'paid'):
        raise ValueError()
    if kind in ('kv','kv_reads') and plan == 'free':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        reset = start + dt.timedelta(days=1)
    elif plan == 'paid':
        start, reset = paid_window(item, now)
        if any((start.hour,start.minute,start.second,start.microsecond,reset.hour,reset.minute,reset.second,reset.microsecond)):
            raise ValueError()
    else:
        raise ValueError()
    account = item['account']
    if not isinstance(account,str) or len(account) != 32 or any(c not in '0123456789abcdef' for c in account):
        raise ValueError()
    args = {'account':account,'start':str(start.date()),'end':str(now.date())}
    if kind == 'cpu':
        used = get(CPU,args,token,'containersUsageAdaptiveGroups','cpuTimeSec')/60
        return used, 375, f'{used:,.1f} / 375 vCPU-min', reset
    if kind == 'browser':
        # Paid plan only. Full-cycle account GraphQL duration is telemetry,
        # not Cloudflare's invoice rounding to whole browser hours.
        if plan != 'paid':
            raise ValueError()
        require_browser(token,account,math.ceil((now-start).total_seconds()))
        args.update(start=start.isoformat().replace('+00:00','Z'),
                    end=now.isoformat(timespec='seconds').replace('+00:00','Z'))
        used = get(BROWSER,args,token,'browserRenderingBrowserTimeUsageAdaptiveGroups','totalSessionDurationMs')/60_000
        return used, 600, f'{used:,.1f} / 600 browser min (analytics)', reset
    if kind not in ('kv','kv_reads'):
        raise ValueError()
    reads = kind == 'kv_reads'
    request = KV.replace('actionType:"write"','actionType:"read"') if reads else KV
    used = get(request,args,token,'kvOperationsAdaptiveGroups','requests')
    cap = (100_000 if plan == 'free' else 10_000_000) if reads else (1_000 if plan == 'free' else 1_000_000)
    operation = 'reads' if reads else 'writes'
    return used, cap, f'{used:,.0f} / {cap:,} {operation} (analytics)', reset

def safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        # Never print exceptions: provider errors can include account or payload details.
        return None

def countdown_label(reset, now):
    hours = math.ceil((reset-now).total_seconds()/3600)
    if hours <= 24:
        return '1 hour' if hours == 1 else f'{hours} hours'
    days = math.ceil(hours/24)
    return f'{days} days'


def render(rows, now, cf_observed=None):
    first_row_y, row_spacing = 54, 65
    detail_offset, track_offset, track_height, bottom_padding = 18, 26, 11, 20
    height = first_row_y + max(0, len(rows) - 1) * row_spacing + track_offset + track_height + bottom_padding
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" viewBox="0 0 480 {height}" role="img" aria-label="Service quota usage">',
        '<style>text{font:13px system-ui;fill:#24292f}.muted{fill:#57606a;font-size:11px}@media(prefers-color-scheme:dark){text{fill:#c9d1d9}.muted{fill:#8b949e}}</style>',
        '<text x="14" y="22" font-weight="600">Service quota usage</text>']
    if cf_observed is not None:
        stamp = f'{cf_observed:%b} {cf_observed.day}, {cf_observed:%H:%M} UTC'
        lines.append(f'<text class="muted" x="466" y="22" text-anchor="end">Cloudflare as of {stamp}</text>')
    for i, (name, row) in enumerate(rows):
        y = first_row_y + i * row_spacing
        lines.append(f'<text x="14" y="{y}">{html.escape(name)}</text>')
        lines.append(f'<rect x="14" y="{y+track_offset}" width="452" height="{track_height}" rx="3" fill="#8b949e" opacity=".30"/>')
        if row is None:
            msg = 'Unavailable'
        else:
            used, cap, label, reset = row
            msg = label if reset is None else f'{label} | resets in {countdown_label(reset, now)}'
            color = '#f38020' if name.startswith('Cloudflare ') else '#40c463'
            width = 452 * min(1,used/cap)
            lines.append(f'<rect x="14" y="{y+track_offset}" width="{width:.2f}" height="{track_height}" rx="3" fill="{color}"/>')
        lines.append(f'<text class="muted" x="14" y="{y+detail_offset}">{html.escape(msg)}</text>')
    return '\n'.join(lines+['</svg>',''])

def profile_rows(actions, containers, kv_writes, *, kv_reads=None, browser_run=None):
    return [('GitHub Actions (included compute snapshot)', actions),
            ('Cloudflare Containers', containers),
            ('Cloudflare KV reads', kv_reads),
            ('Cloudflare KV writes', kv_writes),
            ('Cloudflare Browser Run', browser_run)]


def publish(rows, now, path):
    if len(rows) != 5 or any(row is None for _, row in rows):
        return False
    path.write_text(render(rows, now, cf_observed=now),encoding='utf-8')
    return True


def main():
    now = dt.datetime.now(UTC)
    cf = safe(json.loads,os.environ.get('PROFILE_CF_QUOTA','')) or {}
    token = os.environ.get('PROFILE_CF_ANALYTICS_TOKEN','')
    github_row = safe(github_snapshot, GITHUB_BILLING_SNAPSHOT, now)
    # On cycle rollover Cloudflare may not have published rows yet. An empty
    # series stays unknown, and publish() preserves the previous dated card.
    cf_results = {kind:safe(cloudflare,cf,now,token,kind) for kind in ('cpu','kv','kv_reads','browser')}
    rows = profile_rows(github_row,cf_results['cpu'],cf_results['kv'],
                        kv_reads=cf_results['kv_reads'],browser_run=cf_results['browser'])
    if not publish(rows,now,Path('profile-quotas-v2.svg')):
        raise SystemExit('Quota card unchanged: one or more metrics unavailable.')

if __name__ == '__main__':
    raise SystemExit(main())
