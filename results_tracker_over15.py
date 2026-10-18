"""
Over IQ — results tracker.

Simpler than Under IQ's tracker: every entry here carries a real
match_id straight from TheStatsAPI (this is a match-total market, not
a flattened per-team one), so verification goes directly to
/football/matches/{match_id} and never needs Under IQ's date+team-name
search fallback.
"""

import os
import json
import time
import requests

BASE = "https://api.thestatsapi.com/api"
LOG_PATH = "docs/over15-iq/results/log.json"
DASHBOARD_PATH = "docs/over15-iq/results/index.html"

SCANNER_LABELS = {
    "over15": "Match Over 1.5 Goals",
    # "safest" is kept only so any already-logged entries from before the
    # MIN_ODDS value-floor change still render with a readable label on
    # the dashboard -- no new entries use this scanner.
    "safest": "Safest Pick (legacy)",
}


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def _get(path, key, params=None, timeout=15):
    try:
        r = requests.get(f"{BASE}{path}", headers=_headers(key), params=params or {}, timeout=timeout)
    except Exception as e:
        print(f"  [!] request failed: {path} ({e})")
        return None
    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", 30))
        time.sleep(retry_after)
        return _get(path, key, params, timeout)
    if r.status_code != 200:
        print(f"  [!] {r.status_code} on {path}: {r.text[:200]}")
        return None
    time.sleep(0.5)
    return r.json()


def _entry_id(match_id, scanner):
    return f"{scanner}:{match_id}"


def load_log():
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_log(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2, default=str)


def log_todays_signals(predictions, log):
    """One 'over15' entry per fixture. predictions is already filtered to
    the MIN_ODDS value floor upstream (Over1.5.py), so every entry here
    is a pick worth backing, not just a safe one -- no separate "safest"
    tier needed anymore."""
    existing_ids = {e["id"] for e in log}
    added = 0

    def add(scanner, p):
        nonlocal added
        eid = _entry_id(p["match_id"], scanner)
        if eid in existing_ids:
            return
        log.append({
            "id": eid,
            "scanner": scanner,
            "match_id": p["match_id"],
            "match": f"{p['home_team']} vs {p['away_team']}",
            "league": p["league"],
            "date": p["date"],
            "prob": p["over15"],
            "exp_total": p["exp_total"],
            "odds": p.get("odds"),
            "status": "pending",
            "result": None,
            "actual_total": None,
        })
        existing_ids.add(eid)
        added += 1

    for p in predictions:
        if p["over15"] <= 0:
            continue
        add("over15", p)

    print(f"  Logged {added} new entries ({len(log)} total in log).")
    return log


def _verify_entry(entry):
    data = _get(f"/football/matches/{entry['match_id']}", key=entry["_key"])
    if not data or not data.get("data"):
        return None
    m = data["data"]
    if m.get("status") != "finished":
        return None
    score = m.get("score", {})
    if score.get("home") is None or score.get("away") is None:
        return None
    total = score["home"] + score["away"]
    return {"actual_total": total, "result": "hit" if total >= 2 else "miss"}


def verify_pending_results(log, key, max_checks=60):
    checked = 0
    for entry in log:
        if entry.get("status") != "pending":
            continue
        if checked >= max_checks:
            break
        entry["_key"] = key
        outcome = _verify_entry(entry)
        del entry["_key"]
        if outcome is None:
            checked += 1
            continue
        entry["status"] = "settled"
        entry["result"] = outcome["result"]
        entry["actual_total"] = outcome["actual_total"]
        checked += 1
    return log


def build_results_dashboard(log):
    by_scanner = {}
    for e in log:
        by_scanner.setdefault(e["scanner"], {"hit": 0, "miss": 0, "pending": 0})
        if e["status"] == "pending":
            by_scanner[e["scanner"]]["pending"] += 1
        elif e["result"] == "hit":
            by_scanner[e["scanner"]]["hit"] += 1
        else:
            by_scanner[e["scanner"]]["miss"] += 1

    rows = ""
    for scanner, stats in by_scanner.items():
        settled = stats["hit"] + stats["miss"]
        pct = round(100 * stats["hit"] / settled) if settled else 0
        label = SCANNER_LABELS.get(scanner, scanner)
        rows += f"""<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #233040">
          <span>{label}</span>
          <span><b style="color:#22c55e">{stats['hit']}W</b> / <b style="color:#ef4444">{stats['miss']}L</b> ({pct}%) · {stats['pending']} pending</span>
        </div>"""

    # By league (user request, 2026-10-18) -- same shape as "By scanner"
    # above, just grouped on the "league" field every entry already
    # carries (log_todays_signals has logged it since this tracker's
    # first version, so this works retroactively on the existing log,
    # no backfill needed). Sorted by win% desc (settled-only ties broken
    # by most picks) so the leagues actually worth following float to
    # the top instead of being buried alphabetically.
    by_league = {}
    for e in log:
        league = e.get("league") or "Unknown"
        by_league.setdefault(league, {"hit": 0, "miss": 0, "pending": 0})
        if e["status"] == "pending":
            by_league[league]["pending"] += 1
        elif e["result"] == "hit":
            by_league[league]["hit"] += 1
        else:
            by_league[league]["miss"] += 1

    def _league_sort_key(item):
        _, stats = item
        settled = stats["hit"] + stats["miss"]
        pct = stats["hit"] / settled if settled else -1
        return (pct, settled)

    league_rows = ""
    for league, stats in sorted(by_league.items(), key=_league_sort_key, reverse=True):
        settled = stats["hit"] + stats["miss"]
        pct = round(100 * stats["hit"] / settled) if settled else 0
        league_rows += f"""<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #233040">
          <span>{league}</span>
          <span><b style="color:#22c55e">{stats['hit']}W</b> / <b style="color:#ef4444">{stats['miss']}L</b> ({pct}%) · {stats['pending']} pending</span>
        </div>"""

    recent = sorted([e for e in log if e["status"] == "settled"], key=lambda e: e["date"], reverse=True)[:30]
    recent_rows = ""
    for e in recent:
        color = "#22c55e" if e["result"] == "hit" else "#ef4444"
        recent_rows += f"""<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a2029;font-size:12px">
          <span>{e['match']} <span style="color:#8b98a8">({SCANNER_LABELS.get(e['scanner'], e['scanner'])})</span></span>
          <span style="color:{color}">{e['result'].upper()} ({e['actual_total']})</span>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Over IQ — Results</title></head>
<body style="background:#0b0f14;color:white;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center">📊 Over IQ — Results Tracker</h2>
<p style="text-align:center;margin-top:0"><a href="../over15_iq_index.html" style="color:#f59e0b;text-decoration:none">← Back to picks</a></p>
<div style="background:#121820;border:1px solid #233040;border-radius:12px;padding:14px;margin:14px 0">
  <div style="font-weight:700;margin-bottom:6px">By scanner</div>
  {rows}
</div>
<div style="background:#121820;border:1px solid #233040;border-radius:12px;padding:14px;margin:14px 0">
  <div style="font-weight:700;margin-bottom:6px">By league</div>
  {league_rows}
</div>
<div style="background:#121820;border:1px solid #233040;border-radius:12px;padding:14px;margin:14px 0">
  <div style="font-weight:700;margin-bottom:6px">Recent results</div>
  {recent_rows if recent_rows else '<p style="color:#888">No settled results yet.</p>'}
</div>
</body></html>"""


def run_results_tracker(predictions, key):
    print("Running results tracker...")
    log = load_log()
    log = log_todays_signals(predictions, log)
    log = verify_pending_results(log, key)
    save_log(log)
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w") as f:
        f.write(build_results_dashboard(log))
    print(f"Results log: {len(log)} total entries.")
