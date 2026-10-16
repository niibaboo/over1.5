#!/usr/bin/env python3
"""
Over IQ — built on TheStatsAPI (api.thestatsapi.com)

Purpose-built single-market scanner: MATCH-TOTAL Over 1.5 Goals. Over
1.5 is one of the most reliably safe markets in football precisely
BECAUSE it's a match total, not a per-team line — it only misses on a
0-0 or 1-0 final score, so EITHER team contributing a goal clears it.
That's the opposite of Under IQ's per-team pivot (see that file's own
module docstring): Under IQ moved AWAY from match-total markets
because pooling two teams' scoring exposed a good read on one team to
the other team's variance (a well-read Under pick on Team A could
still lose because Team B had a big night). For Over 1.5, pooling is
the whole advantage — more scoring chances in the pot only RAISES the
probability of clearing it, so this deliberately stays a genuine
match-total market rather than mirroring Under IQ's per-team split.

LEAGUE SCOPE: the union of Match IQ's high-scoring league list and
Under IQ's low-scoring league list (see each file's own
LEAGUE_SEARCH_NAMES for why each league was individually picked).
Over 1.5 doesn't need either curation on its own merits — it clears in
roughly 85-90%+ of matches in almost ANY competitive league, since
it's really only shut out by a 0-0 or 1-0 final score — so this casts
the widest net already proven to work against TheStatsAPI in this
suite, rather than researching a third league list from scratch.

Same GOALS-ONLY model as Under IQ (not Match IQ's heavier stats-based
one) — Over 1.5 only needs each team's own scored/conceded rate, so
this makes ZERO per-match /stats calls, just 1 call per team to list
its recent matches.

VALUE FLOOR: because Over 1.5 is so reliably safe, bookmakers price it
very short (often 1.05-1.15 on genuine near-locks) -- a correct pick
that pays almost nothing isn't worth backing on its own. So the whole
page is filtered to fixtures whose fair odds (100/probability) are at
least MIN_ODDS, rather than just surfacing the highest-probability
matches regardless of how little they'd pay.

Setup:
    pip3 install requests --break-system-packages
    python3 Over1.5.py YOUR_API_KEY   (or set THESTATSAPI_KEY_OVER15)

    Uses its own dedicated TheStatsAPI key (THESTATSAPI_KEY_OVER15),
    separate from the THESTATSAPI_KEY shared by Match IQ/Blue Line/
    Under IQ -- this tool lives in its own repo with its own key so it
    doesn't compete for rate limit with the others.

Output:
    docs/over15-iq/over15_iq_index.html
    docs/over15-iq/over15_iq_predictions.csv
    docs/over15-iq/over15_iq.json
    docs/over15-iq/results/log.json
    docs/over15-iq/results/index.html
"""

import os
import sys
import csv
import json
import math
import time
import requests
from datetime import datetime, timedelta, timezone

BASE = "https://api.thestatsapi.com/api"
RECENT_GAMES = 7
PRIOR_STRENGTH = 3
FIXTURE_WINDOW_DAYS = 10

# A pick this safe is exactly why the real-world price on it is short --
# bookmakers price Over 1.5 off the same near-certainty this model is
# measuring, so a 90%+ projection typically prices at ~1.05-1.11 real
# odds: barely worth staking on its own. Rather than chase near-locks
# that pay almost nothing, this caps the WHOLE page to fixtures whose
# fair odds (100/probability) are at least MIN_ODDS -- so every pick
# shown is one actually worth backing, not just one that's safe.
# Starting point only, like every threshold in this suite -- adjust to
# taste.
MIN_ODDS = 1.20


def implied_odds(prob_pct):
    """Fair decimal odds implied by a probability percentage (100/prob).
    None for 0% (undefined / infinite odds)."""
    if not prob_pct:
        return None
    return round(100 / prob_pct, 2)

# Union of Match IQ's and Under IQ's league lists (deduped) -- see those
# files' own LEAGUE_SEARCH_NAMES for why each league was individually
# picked. Over 1.5 doesn't need either file's curation on its own merits
# (see module docstring), so this reuses both rather than researching a
# third list from scratch.
LEAGUE_SEARCH_NAMES = [
    "Premier League",
    "Championship",
    "Bundesliga",
    "Eredivisie",
    "Liga Portugal Betclic",
    "LaLiga",
    "UEFA Champions League",
    "MLS",
    "Swiss Super League",
    "Danish Superliga",
    "Eliteserien",
    "Serie A",
    "Serie B",
    "Stoiximan Super League",
    "Ligue 2",
]


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def _get(path, key, params=None, timeout=15):
    """Same adaptive rate-limiting as Match IQ/Under IQ -- reads the real
    X-RateLimit-Remaining/Reset headers and backs off only when actually
    close to the limit. 0.5s baseline (Match IQ's already-tuned current
    value, not Under IQ's more conservative 2.0s) since this is the
    goals-only per-team call (1/team, no /stats calls), same cheap
    pattern Under IQ uses -- just scanning more leagues (both lists
    combined) means more total calls, so keeping the per-call cost low
    matters more here, not less."""
    try:
        r = requests.get(f"{BASE}{path}", headers=_headers(key), params=params or {}, timeout=timeout)
    except Exception as e:
        print(f"  [!] request failed: {path} ({e})")
        return None

    remaining = r.headers.get("X-RateLimit-Remaining")
    reset = r.headers.get("X-RateLimit-Reset")
    if remaining is not None:
        try:
            remaining = int(remaining)
            if remaining <= 2 and reset:
                wait = max(0, int(reset) - int(time.time())) + 3
                print(f"  Rate limit nearly exhausted ({remaining} left) — waiting {wait}s...")
                time.sleep(wait)
        except (ValueError, TypeError):
            pass

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", 30))
        print(f"  [!] 429 rate limited — waiting {retry_after}s and retrying once...")
        time.sleep(retry_after)
        return _get(path, key, params, timeout)

    if r.status_code != 200:
        print(f"  [!] {r.status_code} on {path}: {r.text[:200]}")
        return None

    time.sleep(0.5)
    return r.json()


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_cdf(k, lam):
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def find_competition(name, key):
    data = _get("/football/competitions", key, params={"search": name, "per_page": 5})
    if not data:
        return None

    match = None
    for c in data.get("data", []):
        if c["name"].lower() == name.lower():
            match = c
            break
    if not match and data.get("data"):
        print(f"  [!] No exact match for '{name}' — using first result: "
              f"'{data['data'][0]['name']}'. Verify this is correct.")
        match = data["data"][0]
    if not match:
        return None

    detail = _get(f"/football/competitions/{match['id']}", key)
    if detail and detail.get("data"):
        match = {**match, **detail["data"]}
    return match


def get_standings(competition_id, season_id, key):
    data = _get(f"/football/competitions/{competition_id}/seasons/{season_id}/standings", key)
    if not data:
        return {}
    return {row["team"]["id"]: row for row in data.get("data", [])}


def league_averages(standings):
    scored = [r["goals_for"] / r["matches_played"] for r in standings.values() if r.get("matches_played")]
    conceded = [r["goals_against"] / r["matches_played"] for r in standings.values() if r.get("matches_played")]
    lg_scored = sum(scored) / len(scored) if scored else 1.2
    lg_conceded = sum(conceded) / len(conceded) if conceded else 1.2
    return lg_scored, lg_conceded


def get_upcoming_matches(competition_id, season_id, key):
    date_from = datetime.now(timezone.utc).date().isoformat()
    date_to = (datetime.now(timezone.utc).date() + timedelta(days=FIXTURE_WINDOW_DAYS)).isoformat()
    data = _get("/football/matches", key, params={
        "competition_id": competition_id, "season_id": season_id,
        "status": "scheduled", "date_from": date_from, "date_to": date_to,
        "per_page": 20,
    })
    return data.get("data", []) if data else []


team_form_cache = {}


def get_team_form(team_id, competition_id, season_id, key):
    """Last N finished matches for a team — GOALS ONLY, no /stats calls.
    Identical to Under IQ's get_team_form (same reasoning: the base
    /football/matches response already includes each match's score)."""
    cache_key = (team_id, competition_id, season_id)
    if cache_key in team_form_cache:
        return team_form_cache[cache_key]

    data = _get("/football/matches", key, params={
        "team_id": team_id, "competition_id": competition_id, "season_id": season_id,
        "status": "finished", "per_page": 10,
    })
    if not data or not data.get("data"):
        team_form_cache[cache_key] = None
        return None

    matches = sorted(data["data"], key=lambda m: m["utc_date"], reverse=True)[:RECENT_GAMES]
    if not matches:
        team_form_cache[cache_key] = None
        return None

    scored, conceded = [], []
    for m in matches:
        is_home = m["home_team"]["id"] == team_id
        s = m["score"]
        if s.get("home") is None:
            continue
        scored.append(s["home"] if is_home else s["away"])
        conceded.append(s["away"] if is_home else s["home"])

    scored.reverse()
    conceded.reverse()

    if not scored:
        team_form_cache[cache_key] = None
        return None

    n = len(scored)
    form = {
        "n_games": n,
        "avg_scored": round(sum(scored) / n, 2),
        "avg_conceded": round(sum(conceded) / n, 2),
        "goals_list": scored,
    }
    team_form_cache[cache_key] = form
    return form


def shrink(value, n, league_avg, prior=PRIOR_STRENGTH):
    if value is None:
        return league_avg
    return round((n * value + prior * league_avg) / (n + prior), 2)


def predict_over15(h_form, a_form, lg_scored, lg_conceded):
    """Opponent-adjusted expected goals for each side (same approach as
    Match IQ/Under IQ's predict_goals), then the MATCH-TOTAL Over 1.5
    probability computed off the COMBINED expected goals.

    This relies on a real property of the Poisson distribution, not an
    approximation: the sum of two independent Poisson variables (home
    goals, away goals) is itself Poisson with the summed rate. So
    P(total >= 2) = 1 - P(total <= 1) = 1 - poisson_cdf(1, exp_home +
    exp_away) is the exact match-total Over 1.5 probability under this
    model, not a shortcut."""
    h_scored = shrink(h_form["avg_scored"], h_form["n_games"], lg_scored)
    h_conceded = shrink(h_form["avg_conceded"], h_form["n_games"], lg_conceded)
    a_scored = shrink(a_form["avg_scored"], a_form["n_games"], lg_scored)
    a_conceded = shrink(a_form["avg_conceded"], a_form["n_games"], lg_conceded)

    exp_home = round(h_scored * (a_conceded / lg_conceded), 2)
    exp_away = round(a_scored * (h_conceded / lg_conceded), 2)
    exp_total = round(exp_home + exp_away, 2)

    p_over15 = 1 - poisson_cdf(1, exp_home + exp_away)

    return {
        "exp_home": exp_home, "exp_away": exp_away, "exp_total": exp_total,
        "over15": round(p_over15 * 100),
    }


def group_by_date(predictions):
    by_date = {}
    for p in predictions:
        by_date.setdefault(p["date_key"], []).append(p)
    return dict(sorted(by_date.items()))


def build_all_predictions(key):
    all_predictions = []

    for name in LEAGUE_SEARCH_NAMES:
        comp = find_competition(name, key)
        if not comp:
            print(f"[!] Couldn't find competition '{name}' — skipping.")
            continue

        season_id = comp.get("current_season_id") or comp.get("season_id")
        if not season_id:
            print(f"[!] No season id for '{comp['name']}' — skipping.")
            continue

        print(f"\nLooking up competition: {comp['name']}")
        standings = get_standings(comp["id"], season_id, key)
        lg_scored, lg_conceded = league_averages(standings)
        print(f"  League averages: {lg_scored:.2f} scored/gm, {lg_conceded:.2f} conceded/gm")

        matches = get_upcoming_matches(comp["id"], season_id, key)
        print(f"  {len(matches)} upcoming matches in window")

        for m in matches:
            print(f"    {m['home_team']['name']} vs {m['away_team']['name']}")
            h_form = get_team_form(m["home_team"]["id"], comp["id"], season_id, key)
            a_form = get_team_form(m["away_team"]["id"], comp["id"], season_id, key)
            if not h_form or not a_form:
                continue

            proj = predict_over15(h_form, a_form, lg_scored, lg_conceded)
            odds = implied_odds(proj["over15"])
            if odds is None or odds < MIN_ODDS:
                print(f"      -> skipped ({proj['over15']}% implies ~{odds} odds, below {MIN_ODDS} floor)")
                continue

            merged = {
                "match_id": m["id"],
                "league": comp["name"], "date": m["utc_date"], "date_key": m["utc_date"][:10],
                "home_team": m["home_team"]["name"], "away_team": m["away_team"]["name"],
                "home_form": h_form, "away_form": a_form,
                "odds": odds,
                **proj,
            }
            all_predictions.append(merged)

    all_predictions.sort(key=lambda p: (p["date_key"], -p["over15"]))
    return all_predictions


def write_csv(predictions, path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "League", "HomeTeam", "AwayTeam",
                          "ExpHome", "ExpAway", "ExpTotal", "Over15Pct", "ImpliedOdds"])
        for p in predictions:
            writer.writerow([p["date"], p["league"], p["home_team"], p["away_team"],
                              p["exp_home"], p["exp_away"], p["exp_total"], p["over15"], p["odds"]])


CARD_TEMPLATE = """<div style="background:#1a1f26;border-radius:12px;padding:14px;margin:10px 0;border:1px solid #1f6f3f">
  <div style="font-size:11px;color:#999">{league} · {time}</div>
  <div style="font-size:15px;font-weight:bold;margin:2px 0 6px">{home_team} vs {away_team}</div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
    <span style="font-size:12px;color:#8b98a8">Exp {home_team} {exp_home} + {away_team} {exp_away} = {exp_total} total</span>
    <span style="text-align:right">
      <span style="font-size:20px;font-weight:800;color:#22c55e">{over15}%</span>
      <div style="font-size:11px;color:#facc15;font-weight:700">~{odds} odds</div>
    </span>
  </div>
  <div style="font-size:10px;color:#8b98a8;margin-top:8px">last 5 (old→new): {home_team} {home_hist} &nbsp;|&nbsp; {away_team} {away_hist}</div>
</div>"""


def render_main_cards(predictions):
    if not predictions:
        return f'<p style="text-align:center;color:#888">No fixtures cleared the {MIN_ODDS}+ odds floor in the current window.</p>'
    cards = ""
    for p in predictions:
        home_hist = "/".join(str(v) for v in p["home_form"]["goals_list"]) or "—"
        away_hist = "/".join(str(v) for v in p["away_form"]["goals_list"]) or "—"
        cards += CARD_TEMPLATE.format(
            league=p["league"], time=p["date"][:16].replace("T", " "),
            home_team=p["home_team"], away_team=p["away_team"],
            exp_home=p["exp_home"], exp_away=p["exp_away"], exp_total=p["exp_total"],
            over15=p["over15"], odds=p["odds"],
            home_hist=home_hist, away_hist=away_hist,
        )
    return cards


def build_legs(all_predictions):
    """One Over 1.5 leg per MATCH (not per team -- see module docstring).
    all_predictions is already filtered to the MIN_ODDS floor by this
    point, so every leg here is one worth backing, not just a safe one.
    subject=match label, so the builder's 1-per-subject cap naturally
    caps this at 1 leg per match (there's only one market here anyway,
    but this keeps the same JS unmodified)."""
    legs = []
    for p in all_predictions:
        if p["over15"] <= 0:
            continue
        match_label = f"{p['home_team']} vs {p['away_team']}"
        legs.append({
            "match": match_label,
            "subject": match_label,
            "market": "Over 1.5 Goals",
            "prob": p["over15"],
            "category": "Match Over 1.5 Goals",
            "detail": f"exp {p['exp_home']} + {p['exp_away']} = {p['exp_total']} total",
            "league": p["league"],
        })
    return legs


HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Over IQ</title></head>
<body style="background:#0b0f14;color:white;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center;margin-bottom:2px">⚽ Over IQ — Over 1.5 Goals</h2>
<p style="text-align:center;color:#888;font-size:11px;margin-top:0">Powered by TheStatsAPI · {generated}</p>
<p style="text-align:center;margin:6px 0 0;font-size:12px"><a href="results/index.html" style="color:#f59e0b;text-decoration:none">📊 Results Tracker</a></p>
<p style="text-align:center;margin:12px 0 4px"><a href="over15_iq_predictions.csv" download style="background:#222;border:1px solid #444;color:white;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px">⬇ Download CSV</a></p>
<p style="text-align:center;color:#8b98a8;font-size:11px;margin:6px 0 0">Match-total Over 1.5 Goals — one of football's safest markets, since EITHER team scoring clears it. Filtered to fixtures implying {min_odds}+ real-world odds, so nothing shown here is priced too short to be worth backing.</p>

<div id="builderPanel" style="background:#121820;border:1px solid #233040;border-radius:12px;padding:16px;margin:14px 0">
  <div style="font-size:15px;font-weight:800;margin-bottom:10px">🎯 Daily Acca Builder</div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
    <label style="font-size:12px;color:#8b98a8">Target odds:</label>
    <input type="number" step="0.05" min="1.1" value="2.5" id="targetOdds" style="width:70px;background:#161d27;border:1px solid #233040;color:white;border-radius:6px;padding:6px 8px;font-size:13px">
    <label style="font-size:12px;color:#8b98a8">Max legs:</label>
    <input type="number" step="1" min="2" value="6" id="maxLegs" style="width:70px;background:#161d27;border:1px solid #233040;color:white;border-radius:6px;padding:6px 8px;font-size:13px">
    <button onclick="buildAcca()" style="background:#22c55e;color:#04140a;font-weight:700;border:none;padding:7px 14px;border-radius:6px;font-size:13px;cursor:pointer">Build</button>
    <button onclick="buildAcca()" style="background:#161d27;border:1px solid #233040;color:white;padding:7px 14px;border-radius:6px;font-size:13px;cursor:pointer">🔀 Shuffle</button>
  </div>
  <div id="builderResult" style="font-size:12px;color:#8b98a8">
    Stacks Over 1.5 picks (each already {min_odds}+ odds on its own) across different
    matches toward your target odds. Capped at ONE leg per match (there's only one
    market here, so this just protects against future duplicate markets on the same
    fixture).
  </div>
</div>

{cards}

<script>
const LEGS = {legs_json};

function shuffleArr(arr) {{
  for (let i = arr.length - 1; i > 0; i--) {{
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }}
  return arr;
}}
function tieredShuffle(legs, bandSize) {{
  const bands = {{}};
  legs.forEach(l => {{
    const band = Math.floor(l.prob / bandSize);
    (bands[band] = bands[band] || []).push(l);
  }});
  const keys = Object.keys(bands).map(Number).sort((a,b) => b-a);
  let result = [];
  keys.forEach(k => {{ result = result.concat(shuffleArr(bands[k])); }});
  return result;
}}
function buildAcca() {{
  const target = parseFloat(document.getElementById('targetOdds').value) || 2.5;
  const maxLegs = parseInt(document.getElementById('maxLegs').value) || 6;

  const pool = tieredShuffle(LEGS.filter(l => l.prob > 0), 5);
  const chosen = [];
  const subjectCount = {{}};
  let combinedOdds = 1;

  for (const leg of pool) {{
    if (combinedOdds >= target || chosen.length >= maxLegs) break;
    if (subjectCount[leg.subject]) continue;
    chosen.push(leg);
    combinedOdds *= 100 / leg.prob;
    subjectCount[leg.subject] = 1;
  }}

  const out = document.getElementById('builderResult');
  if (!chosen.length) {{ out.innerHTML = 'No legs available to build from.'; return; }}

  const rows = chosen.map(l => `
    <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #233040">
      <span>${{l.match}}<br><span style="color:#facc15">${{l.market}}</span> <span style="color:#8b98a8">· ${{l.league}}</span>
      <br><span style="color:#8b98a8;font-size:10px">${{l.detail}}</span></span>
      <span style="text-align:right"><span style="color:#facc15;font-weight:bold">${{l.prob}}%</span></span>
    </div>
  `).join('');

  const capNote = chosen.length >= maxLegs && combinedOdds < target
    ? ' (hit the leg cap before reaching target — raise Max legs or lower Target odds)'
    : (combinedOdds < target ? ' (ran out of legs before reaching target)' : '');

  out.innerHTML = `
    <div style="color:white;font-size:13px;margin-bottom:6px">
      ${{chosen.length}} legs · est. combined odds ~<b>${{combinedOdds.toFixed(2)}}</b>${{capNote}}
    </div>
    ${{rows}}
    <div style="color:#8b98a8;font-size:10px;margin-top:8px;line-height:1.4">
      Estimate multiplies each leg's fair odds (100/probability) — real sportsbook odds
      include their margin, so treat this as a ranking tool, not a firm price.
    </div>
  `;
}}
</script>
</body></html>"""


if __name__ == "__main__":
    api_key = os.environ.get("THESTATSAPI_KEY_OVER15")
    if not api_key and len(sys.argv) >= 2:
        api_key = sys.argv[1]
    if not api_key:
        print("Usage: python3 Over1.5.py YOUR_API_KEY  (or set THESTATSAPI_KEY_OVER15)")
        raise SystemExit(1)

    all_predictions = build_all_predictions(api_key)

    os.makedirs("docs/over15-iq", exist_ok=True)

    write_csv(all_predictions, "docs/over15-iq/over15_iq_predictions.csv")
    with open("docs/over15-iq/over15_iq.json", "w") as f:
        json.dump(all_predictions, f, indent=2, default=str)

    html = HTML_TEMPLATE.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        min_odds=MIN_ODDS,
        cards=render_main_cards(all_predictions),
        legs_json=json.dumps(build_legs(all_predictions)),
    )
    with open("docs/over15-iq/over15_iq_index.html", "w") as f:
        f.write(html)

    print(f"\nMain page — {len(all_predictions)} fixtures, all at {MIN_ODDS}+ implied odds.")

    try:
        import results_tracker_over15
        results_tracker_over15.run_results_tracker(all_predictions, api_key)
    except Exception as e:
        # Results tracking sits on top of everything above, which has
        # already succeeded by this point -- a failure here should
        # never take down an otherwise-successful run.
        print(f"\n[!] Results tracker failed, but the rest of this run succeeded: {e}")

    print(f"\nDone — {len(all_predictions)} total fixtures projected.")
