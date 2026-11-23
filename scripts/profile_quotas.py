import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import urllib.request

UTC = dt.timezone.utc
CF = 'https://api.cloudflare.com/client/v4/graphql'
CPU = '''query($account: String, $start: Time, $end: Time) {
  viewer { accounts(filter:{accountTag:$account}) {
    containersUsageAdaptiveGroups(limit:100,
      filter:{date_geq:$start,date_leq:$end}) { dimensions { date } sum { cpuTimeSec } }
  } }
}'''
KV = '''query($account: string!, $start: Date, $end: Date) {
  viewer { accounts(filter:{accountTag:$account}) {
    kvOperationsAdaptiveGroups(limit:100,
      filter:{date_geq:$start,date_leq:$end,actionType:"write"}) { sum { requests } }
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

def github(item, now):
    _, reset = window(item, now)
    observed = when(item['observed_at'])
    if not when(item['start']) <= observed <= now or now - observed > dt.timedelta(hours=36):
        raise ValueError()
    pct, minutes = item['allowance_percent'], item['runner_minutes']
    if not number(pct) or pct > 100 or not number(minutes):
        raise ValueError()
    return pct, 100, f'{pct:.1f}% of included compute used; {minutes:,.0f} actual runner min', reset

def query(body, args, token, key, field):
    req = urllib.request.Request(CF, data=json.dumps({'query':body,'variables':args}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    with urllib.request.urlopen(req, timeout=12) as res:
        payload = json.loads(res.read())
    if payload.get('errors'):
        raise ValueError()
    accounts = payload['data']['viewer']['accounts']
    if len(accounts) != 1:
        raise ValueError()
    rows = accounts[0][key]
    if not rows or len(rows) >= 100:
        raise ValueError()
    values = [r['sum'][field] for r in rows]
    if not all(map(number, values)):
        raise ValueError()
    return sum(values)

def cloudflare(item, now, token, kind, get=query):
    plan = item['plan']
    if not token or plan not in ('free', 'paid'):
        raise ValueError()
    if kind == 'kv' and plan == 'free':
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
    used = get(KV,args,token,'kvOperationsAdaptiveGroups','requests')
    cap = 1_000 if plan == 'free' else 1_000_000
    return used, cap, f'{used:,.0f} / {cap:,} writes (analytics)', reset

def safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        # Never print exceptions: provider errors can include account or payload details.
        return None

def render(rows, now):
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="205" viewBox="0 0 480 205" role="img" aria-label="Service quota usage">',
        '<style>text{font:13px system-ui;fill:#24292f}.muted{fill:#57606a;font-size:11px}@media(prefers-color-scheme:dark){text{fill:#c9d1d9}.muted{fill:#8b949e}}</style>',
        '<text x="14" y="22" font-weight="600">Service quota usage</text>']
    for i, (name, row) in enumerate(rows):
        y = 49 + i*50
        lines.append(f'<text x="14" y="{y}">{html.escape(name)}</text>')
        lines.append(f'<rect x="14" y="{y+21}" width="452" height="6" rx="3" fill="#8b949e" opacity=".30"/>')
        if row is None:
            msg = 'Unavailable'
        else:
            used, cap, label, reset = row
            remaining = math.ceil((reset-now).total_seconds()/86400)
            countdown = '1 day' if remaining == 1 else f'{remaining} days'
            msg = f'{label} | resets in {countdown}'
            width = 452 * min(1,used/cap)
            lines.append(f'<rect x="14" y="{y+21}" width="{width:.2f}" height="6" rx="3" fill="#40c463"/>')
        lines.append(f'<text class="muted" x="14" y="{y+15}">{html.escape(msg)}</text>')
    return '\n'.join(lines+['</svg>',''])

def main():
    now = dt.datetime.now(UTC)
    gh = safe(json.loads,os.environ.get('PROFILE_GITHUB_QUOTA','')) or {}
    cf = safe(json.loads,os.environ.get('PROFILE_CF_QUOTA','')) or {}
    token = os.environ.get('PROFILE_CF_ANALYTICS_TOKEN','')
    rows = [('GitHub Actions (free allowance)',safe(github,gh,now)),
            ('Cloudflare Containers',safe(cloudflare,cf,now,token,'cpu')),
            ('Cloudflare KV',safe(cloudflare,cf,now,token,'kv'))]
    Path('profile-quotas.svg').write_text(render(rows,now),encoding='utf-8')

if __name__ == '__main__':
    main()
