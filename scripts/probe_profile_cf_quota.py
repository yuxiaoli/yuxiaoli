"""Read-only reachability check for the scoped Cloudflare Profile token."""
import datetime as dt
import json
import math
import os
import re
import urllib.request

UTC = dt.timezone.utc
ENDPOINT = "https://api.cloudflare.com/client/v4/graphql"
METRICS = ("containers", "kv_reads", "kv_writes", "browser_run")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPEN = urllib.request.build_opener(NoRedirect()).open


def midnight(text):
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if not text.endswith("Z") or value.time() != dt.time():
        raise ValueError()
    return value


def monthly_window(day, now):
    month_offset = now.year * 12 + now.month - 1

    def boundary(offset):
        year, month = divmod(offset, 12)
        return dt.datetime(year, month + 1, day, tzinfo=UTC)

    if now < boundary(month_offset):
        month_offset -= 1
    return boundary(month_offset - 1), boundary(month_offset), boundary(month_offset + 1)


def cycle_windows(config, now):
    if config.get("plan") != "paid":
        raise ValueError()
    if "start" in config or "reset" in config:
        start, reset = midnight(config["start"]), midnight(config["reset"])
        day = start.day
        if not 1 <= day <= 28 or monthly_window(day, start)[1:] != (start, reset):
            raise ValueError()
        previous, current, end = monthly_window(day, now)
        if (start, reset) not in ((previous, current), (current, end)):
            raise ValueError()
    else:
        day = config["billing_day"]
        if type(day) is not int or not 1 <= day <= 28:
            raise ValueError()
        previous, current, end = monthly_window(day, now)
    return previous, current, min(end, now)


def graphql(token, account, types, fields, variables):
    header = "query($account: string!" + types + ")"
    query = header + " {viewer {accounts(filter:{accountTag:$account}) {" + fields + "}}}"
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query":query, "variables":dict(variables, account=account)}).encode(),
        headers={"Authorization":"Bearer " + token, "Content-Type":"application/json"},
    )
    with OPEN(req, timeout=12) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise ValueError()
    accounts = payload["data"]["viewer"]["accounts"]
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise ValueError()
    return accounts[0]


def metric_available(token, account, types, fields, variables, name):
    try:
        rows = graphql(token, account, types, fields, variables)["metric"]
        if not isinstance(rows, list) or not 0 < len(rows) < 100:
            return False
        values = [row["sum"][name] for row in rows]
        return all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in values)
    except Exception:
        return False


def browser_range_available(token, account, start, end, now):
    try:
        cfg = graphql(token, account, "", """
          settings {browserRenderingBrowserTimeUsageAdaptiveGroups {
            enabled maxDuration notOlderThan maxPageSize availableFields
          }}""", {})["settings"]["browserRenderingBrowserTimeUsageAdaptiveGroups"]
        return (cfg["enabled"] is True and cfg["maxPageSize"] >= 100
                and cfg["maxDuration"] >= math.ceil((end - start).total_seconds())
                and cfg["notOlderThan"] >= math.ceil((now - start).total_seconds())
                and "sum_totalSessionDurationMs" in cfg["availableFields"])
    except Exception:
        return False


def check_cycle(token, account, start, end, now):
    report = dict.fromkeys(METRICS, False)
    if not start < end or now-start > dt.timedelta(days=32):
        return report
    dates = {"s": str(start.date()), "e": str((end-dt.timedelta(microseconds=1)).date())}
    report["containers"] = metric_available(
        token, account, ", $s: Time, $e: Time", """
          metric:containersUsageAdaptiveGroups(limit:100,
            filter:{date_geq:$s,date_leq:$e},orderBy:[date_ASC]) {dimensions {date} sum {cpuTimeSec}}
        """, dates, "cpuTimeSec")
    for action in ("read", "write"):
        report["kv_" + action + "s"] = metric_available(
            token, account, ", $s: Date, $e: Date", """
          metric:kvOperationsAdaptiveGroups(limit:100,
            filter:{date_geq:$s,date_leq:$e,actionType:""" + json.dumps(action) + """}) {sum {requests}}
        """, dates, "requests")
    if browser_range_available(token, account, start, end, now):
        stamps = {k: v.isoformat(timespec="seconds").replace("+00:00","Z")
                  for k, v in (("s",start),("e",end))}
        report["browser_run"] = metric_available(
            token, account, ", $s: Time, $e: Time", """
          metric:browserRenderingBrowserTimeUsageAdaptiveGroups(limit:100,
            filter:{datetime_geq:$s,datetime_lt:$e}) {dimensions {date} sum {totalSessionDurationMs}}
        """, stamps, "totalSessionDurationMs")
    return report


def run(env, now):
    result = {"configuration_valid":False, "current":dict.fromkeys(METRICS,False),
              "previous_attempted":False, "previous":dict.fromkeys(METRICS,False)}
    try:
        cfg = json.loads(env["PROFILE_CF_QUOTA"])
        token = env["PROFILE_CF_ANALYTICS_TOKEN"]
        account = cfg["account"]
        if not token or not isinstance(account, str) or re.fullmatch("[a-f0-9]{32}", account) is None:
            raise ValueError()
        previous, current, end = cycle_windows(cfg, now)
        result["configuration_valid"] = True
    except Exception:
        return result, 1
    result["current"] = check_cycle(token, account, current, end, now)
    # After reset, the fresh cycle can have no data; verify the immediately
    # preceding monthly cycle only while it remains inside known retention.
    if not all(result["current"].values()) and now-previous <= dt.timedelta(days=32):
        result["previous_attempted"] = True
        result["previous"] = check_cycle(token, account, previous, current, now)
    return result, 0 if all(result["current"].values()) or all(result["previous"].values()) else 1


if __name__ == "__main__":
    result, status = run(os.environ, dt.datetime.now(UTC))
    print(json.dumps(result,sort_keys=True))
    raise SystemExit(status)
