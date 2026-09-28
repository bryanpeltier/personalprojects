"""
NHL_DMan_Shift_Processor.py
===========================
How long does a defenseman take to get the puck out of his own zone once he is
in it, and on what share of those shifts does he manage to get it out at all?

Data sources (NHL public JSON feeds; no scraping, no jersey-number matching)
  * api-web.nhle.com  /club-schedule-season/{team}/{season}   -> game list
  * api-web.nhle.com  /gamecenter/{gameId}/play-by-play       -> events, zone codes, rosterSpots
  * api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId= -> every player's shifts (with teamId)

Why the feeds fix the trade problem
  Every shift row carries the playerId AND the teamId he was playing for in that
  game. Stats are therefore aggregated per (team, playerId). A defenseman traded
  from A to B gets one row/dot for A (games with A) and one for B (games with B),
  with no game-number bookkeeping.

Method (per defenseman shift, from that defenseman's team's point of view)
  * The play-by-play only timestamps events, not puck position, so each
    zone-tagged event (faceoff, hit, shot, block, give/takeaway, goal) is
    converted to "D / N / O" for the defenseman's team. Events without a
    zone code are ignored (never treated as "outside the zone").
  * A shift is a "D-zone shift" only if the puck is in the defensive zone at
    some point while he is on the ice. Shifts that never touch the D-zone are
    excluded from BOTH metrics (they are not failed clears).
  * A D-zone "stint" begins
        - at the shift start if the shift starts in the D-zone (D-zone faceoff
          at the shift's first second, or an on-the-fly change with the puck
          last seen in the D-zone <= carryover_seconds earlier), or
        - when the puck is first seen in the D-zone during the shift.
  * A stint ends as an EXIT only when the next zone-tagged event is a live-play
    event (not a faceoff) outside the D-zone. A faceoff is always preceded by a
    whistle, so D-zone -> faceoff elsewhere (icing, offside, goal against, ...)
    is a "whistle" ending, not a clear. If the shift ends with the puck last
    seen in the D-zone, the stint is "open" (no exit observed).
  * Boundaries are placed halfway between the last event seen in the zone and
    the first evidence otherwise (interpolate=True), because entry/exit
    themselves are never timestamped.

Blocked shots
  eventOwnerTeamId is the SHOOTING team but zoneCode is from the BLOCKER's side.
  This module resolves the reference team from blockingPlayerId. Run
  check_zone_conventions() on any game to verify this against the coordinates.
"""
from __future__ import annotations

import gzip
import json
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # optional: nicer, non-overlapping labels
    from adjustText import adjust_text
except ImportError:  # pragma: no cover
    adjust_text = None

try:  # optional: nicer text tables
    from tabulate import tabulate
except ImportError:  # pragma: no cover
    tabulate = None

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SEASON = 20252026
API_WEB = "https://api-web.nhle.com/v1"
API_STATS = "https://api.nhle.com/stats/rest/en"
CACHE_DIR = Path("nhl_cache")
SCHEDULE_TTL_SECONDS = 24 * 3600  # re-pull schedules daily; game data is cached forever

REGULAR_SEASON = 2
FINAL_STATES = {"OFF", "FINAL"}
REGULAR_SHIFT_TYPE = 517  # shiftcharts typeCode for a real shift (505 = goal marker rows)
MIN_GAMES = 10

# 2025-26 franchises (Utah's abbreviation stayed UTA after the Mammoth rebrand)
TEAMS = (
    "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM",
    "FLA", "LAK", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT",
    "SEA", "SJS", "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WPG", "WSH",
)

TEAM_COLORS = {
    "ANA": "#F47A38", "BOS": "#FFB81C", "BUF": "#003087", "CAR": "#CC0000",
    "CBJ": "#002654", "CGY": "#C8102E", "CHI": "#CF0A2C", "COL": "#6F263D",
    "DAL": "#006847", "DET": "#CE1126", "EDM": "#FF4C00", "FLA": "#C8102E",
    "LAK": "black",   "MIN": "#154734", "MTL": "#AF1E2D", "NJD": "#CE1126",
    "NSH": "#FFB81C", "NYI": "#00539B", "NYR": "#0038A8", "OTT": "#C52032",
    "PHI": "#F74902", "PIT": "#FCB514", "SEA": "#001628", "SJS": "teal",
    "STL": "#002F87", "TBL": "#002868", "TOR": "#00205B", "UTA": "#6CACE4",
    "VAN": "#00205B", "VGK": "#B4975A", "WPG": "#041E42", "WSH": "#C8102E",
}

# play-by-play typeDescKey values that carry a puck-location zone code
PLAY_EVENTS = frozenset(
    {"hit", "shot-on-goal", "missed-shot", "blocked-shot", "giveaway", "takeaway", "goal"}
)
BREAK_EVENTS = frozenset({"stoppage", "period-end", "game-end"})
PERIOD_SECONDS = 1200


@dataclass(frozen=True)
class ZoneParams:
    """Tunable assumptions of the zone-tracking model."""

    carryover_seconds: float = 5.0
    """On-the-fly change: if the last zone-tagged event before the shift was in the
    D-zone and happened <= this many seconds before the shift start (with no
    stoppage since), the shift is treated as starting in the D-zone. 0 disables."""

    interpolate: bool = True
    """True: place zone entry/exit halfway between the last event seen on one side
    and the first seen on the other. False: use the raw event timestamps
    (entry = first D-zone event, exit = first event outside the zone)."""


def season_label(season: int = SEASON) -> str:
    s = str(season)
    return f"{s[:4]}-{s[6:]}"


# --------------------------------------------------------------------------- #
# Data intake
# --------------------------------------------------------------------------- #
class NHLClient:
    """Thin cached HTTP client. Raw JSON is stored gzip'd under cache_dir so the
    ~2,600 requests for a full season are only ever made once."""

    def __init__(self, cache_dir: Path | str = CACHE_DIR, timeout: float = 30, pause: float = 0.05):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.pause = pause
        self.session = requests.Session()
        retry = Retry(
            total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
        self.session.headers.update({"User-Agent": "dman-zone-exit-analysis/2.0"})

    def _get_json(self, path: Path, url: str, max_age: float | None = None, is_valid=None):
        if path.exists() and (max_age is None or time.time() - path.stat().st_mtime < max_age):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if self.pause:
            time.sleep(self.pause)
        if is_valid is None or is_valid(data):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(data, fh)
            tmp.replace(path)
        return data

    def season_games(self, season: int = SEASON, teams=TEAMS) -> pd.DataFrame:
        """All completed regular-season games, de-duplicated across team schedules."""
        games: dict[int, dict] = {}
        failed_teams = []
        for team in teams:
            path = self.cache_dir / str(season) / f"schedule_{team}.json.gz"
            try:
                data = self._get_json(path, f"{API_WEB}/club-schedule-season/{team}/{season}",
                                      max_age=SCHEDULE_TTL_SECONDS)
            except Exception as exc:  # each game is on two schedules, so one miss is survivable
                failed_teams.append((team, repr(exc)))
                continue
            for g in data.get("games", []):
                games[g["id"]] = {
                    "game_id": g["id"],
                    "game_date": g.get("gameDate"),
                    "game_type": g.get("gameType"),
                    "state": g.get("gameState"),
                    "schedule_state": g.get("gameScheduleState", "OK"),
                    "home": g["homeTeam"]["abbrev"],
                    "away": g["awayTeam"]["abbrev"],
                }
        if failed_teams:
            print(f"WARNING: schedule fetch failed for {failed_teams}")
        df = pd.DataFrame(list(games.values()))
        if df.empty:
            raise RuntimeError("No games returned from the schedule endpoint.")
        df = df[(df.game_type == REGULAR_SEASON) & df.state.isin(FINAL_STATES) & (df.schedule_state == "OK")]
        return df.sort_values(["game_date", "game_id"]).reset_index(drop=True)

    def play_by_play(self, game_id: int) -> dict:
        path = self.cache_dir / "pbp" / f"{game_id}.json.gz"
        return self._get_json(path, f"{API_WEB}/gamecenter/{game_id}/play-by-play")

    def shift_chart(self, game_id: int) -> list[dict]:
        path = self.cache_dir / "shifts" / f"{game_id}.json.gz"
        url = f"{API_STATS}/shiftcharts?cayenneExp=gameId={game_id}"
        data = self._get_json(path, url, is_valid=lambda d: bool(d.get("data")))  # never cache an empty chart
        return data.get("data", [])


# --------------------------------------------------------------------------- #
# Play-by-play -> per-team zone timeline
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Event:
    t: int             # game seconds (period-1)*1200 + seconds into period
    kind: str          # 'faceoff' | 'play' | 'break'
    state: str | None  # 'D' | 'N' | 'O' from the timeline team's perspective; None for breaks


class Timeline:
    def __init__(self, events):
        self.events = list(events)
        self.times = [e.t for e in self.events]


def _mmss_to_seconds(txt) -> int | None:
    if not txt or ":" not in str(txt):
        return None
    m, s = str(txt).split(":")[:2]
    try:
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def _name(field) -> str:
    if isinstance(field, dict):
        return field.get("default", "")
    return field or ""


def _roster(pbp: dict) -> dict[int, dict]:
    out = {}
    for r in pbp.get("rosterSpots", []):
        out[r["playerId"]] = {
            "name": f"{_name(r.get('firstName'))} {_name(r.get('lastName'))}".strip(),
            "team_id": r.get("teamId"),
            "pos": r.get("positionCode"),
            "number": r.get("sweaterNumber"),
        }
    return out


def _zone_reference_team(play: dict, roster: dict, home_id: int, away_id: int):
    """Team whose point of view the play's zoneCode is written from.
    Every event type is from the event owner's side EXCEPT blocked shots, which
    are owned by the shooter but zoned from the blocker's side."""
    d = play.get("details") or {}
    owner = d.get("eventOwnerTeamId")
    if play.get("typeDescKey") != "blocked-shot":
        return owner
    blocker = roster.get(d.get("blockingPlayerId"))
    if blocker is not None and blocker["team_id"] is not None:
        return blocker["team_id"]
    if owner is None:
        return None
    if d.get("reason") == "teammate-blocked":
        return owner
    return away_id if owner == home_id else home_id


def _state_for_team(zone_code, reference_team, team_id) -> str | None:
    if zone_code == "N":
        return "N"
    if zone_code not in ("O", "D") or reference_team is None:
        return None
    if reference_team == team_id:
        return zone_code
    return "O" if zone_code == "D" else "D"


def build_timeline(plays: list[dict], team_id: int, roster: dict, home_id: int, away_id: int) -> Timeline:
    """Zone-tagged events for one team, chronologically ordered (ties broken by sortOrder)."""
    rows = []
    for p in plays:
        typ = p.get("typeDescKey")
        pd_ = p.get("periodDescriptor") or {}
        period, in_period = pd_.get("number"), _mmss_to_seconds(p.get("timeInPeriod"))
        if period is None or in_period is None or pd_.get("periodType") == "SO":
            continue
        t = (period - 1) * PERIOD_SECONDS + in_period
        order = p.get("sortOrder", 0)
        if typ in BREAK_EVENTS:
            rows.append((t, order, Event(t, "break", None)))
            continue
        if typ != "faceoff" and typ not in PLAY_EVENTS:
            continue
        d = p.get("details") or {}
        state = _state_for_team(d.get("zoneCode"), _zone_reference_team(p, roster, home_id, away_id), team_id)
        if state is None:  # no usable zone -> no evidence either way
            continue
        rows.append((t, order, Event(t, "faceoff" if typ == "faceoff" else "play", state)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return Timeline(r[2] for r in rows)


# --------------------------------------------------------------------------- #
# Core logic: one shift -> D-zone stints
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShiftResult:
    dz_shift: bool             # puck was in the D-zone at some point while he was on the ice
    exited: bool               # at least one stint ended in an observed exit
    outcome: str               # 'none' | 'exit' | 'whistle' | 'open'
    n_stints: int
    dzone_seconds: float       # total D-zone time on the shift (all stints)
    first_exit_seconds: float  # length of the first stint that ended in an exit (nan if none)


def _starts_in_dzone(timeline: Timeline, lo: int, window: list[Event], start: int, params: ZoneParams) -> bool:
    """On-the-fly change: was the puck last seen in the D-zone just before the shift began?"""
    if params.carryover_seconds <= 0 or lo == 0:
        return False
    # A faceoff/stoppage at the first second decides the starting state itself.
    if any(ev.t == start and ev.kind in ("faceoff", "break") for ev in window):
        return False
    prev = timeline.events[lo - 1]  # events before `lo` all have t < start
    return prev.kind != "break" and prev.state == "D" and start - prev.t <= params.carryover_seconds


def analyze_shift(timeline: Timeline, start: int, end: int, params: ZoneParams | None = None) -> ShiftResult:
    params = params or ZoneParams()
    lo = bisect_left(timeline.times, start)
    hi = bisect_right(timeline.times, end)
    # A faceoff at the very second the shift ends is taken by his replacement.
    window = [ev for ev in timeline.events[lo:hi] if not (ev.kind == "faceoff" and ev.t == end and end > start)]

    interp = params.interpolate
    entry_t = (lambda outside, t: (outside + t) / 2) if interp else (lambda outside, t: t)
    exit_t = (lambda last_d, t: (last_d + t) / 2) if interp else (lambda last_d, t: t)
    open_t = (lambda last_d, t: (last_d + t) / 2) if interp else (lambda last_d, t: last_d)

    stints: list[tuple[float, float, str]] = []
    cur_start = last_d = None
    last_outside = start  # latest moment we have evidence the puck was NOT in the D-zone (>= shift start)

    if _starts_in_dzone(timeline, lo, window, start, params):
        cur_start = last_d = start

    for ev in window:
        if ev.kind == "break":  # only used above (starting state); the faceoff that follows is the evidence
            continue
        if ev.state == "D":
            if cur_start is None:  # entry: a faceoff is an exact placement, live play is interpolated
                cur_start = ev.t if ev.kind == "faceoff" else entry_t(last_outside, ev.t)
            last_d = ev.t
        else:
            if cur_start is not None:
                outcome = "exit" if ev.kind == "play" else "whistle"  # faceoff => whistle, not a clear
                stints.append((cur_start, max(exit_t(last_d, ev.t), cur_start), outcome))
                cur_start = None
            last_outside = ev.t

    if cur_start is not None:  # shift ended with the puck last seen in the D-zone
        stints.append((cur_start, max(open_t(last_d, end), cur_start), "open"))

    if not stints:
        return ShiftResult(False, False, "none", 0, 0.0, np.nan)

    exits = [s for s in stints if s[2] == "exit"]
    if exits:
        outcome = "exit"
    elif stints[-1][2] == "open":
        outcome = "open"
    else:
        outcome = "whistle"
    return ShiftResult(
        dz_shift=True,
        exited=bool(exits),
        outcome=outcome,
        n_stints=len(stints),
        dzone_seconds=float(sum(e - s for s, e, _ in stints)),
        first_exit_seconds=float(exits[0][1] - exits[0][0]) if exits else np.nan,
    )


# --------------------------------------------------------------------------- #
# One game -> one row per defenseman shift
# --------------------------------------------------------------------------- #
SHIFT_COLUMNS = [
    "game_id", "game_date", "team", "opp", "player_id", "player", "number", "period", "shift_number",
    "start_s", "end_s", "dz_shift", "exited", "outcome", "n_stints", "dzone_seconds", "first_exit_seconds",
]


def analyze_game(pbp: dict, shifts: list[dict], params: ZoneParams | None = None) -> pd.DataFrame:
    params = params or ZoneParams()
    home, away = pbp["homeTeam"], pbp["awayTeam"]
    abbrev = {home["id"]: home["abbrev"], away["id"]: away["abbrev"]}
    opp_of = {home["id"]: away["abbrev"], away["id"]: home["abbrev"]}
    roster = _roster(pbp)
    plays = pbp.get("plays", [])
    timelines = {tid: build_timeline(plays, tid, roster, home["id"], away["id"]) for tid in abbrev}

    rows = []
    for sh in shifts:
        if str(sh.get("typeCode", REGULAR_SHIFT_TYPE)) != str(REGULAR_SHIFT_TYPE):  # skip goal-marker rows etc.
            continue
        info = roster.get(sh.get("playerId"))
        if info is None or info["pos"] != "D":
            continue
        period = sh.get("period")
        s0, e0 = _mmss_to_seconds(sh.get("startTime")), _mmss_to_seconds(sh.get("endTime"))
        if period is None or s0 is None or e0 is None:
            continue
        start, end = (period - 1) * PERIOD_SECONDS + s0, (period - 1) * PERIOD_SECONDS + e0
        if end <= start:
            continue
        # The shift row's teamId is the team he was playing for IN THIS GAME (trade-proof).
        tid = sh.get("teamId", info["team_id"])
        if tid not in timelines:
            tid = info["team_id"]
        if tid not in timelines:
            continue
        res = analyze_shift(timelines[tid], start, end, params)
        rows.append({
            "game_id": pbp.get("id"), "game_date": pbp.get("gameDate"),
            "team": abbrev[tid], "opp": opp_of[tid],
            "player_id": sh["playerId"], "player": info["name"], "number": info["number"],
            "period": period, "shift_number": sh.get("shiftNumber"),
            "start_s": start, "end_s": end,
            "dz_shift": res.dz_shift, "exited": res.exited, "outcome": res.outcome,
            "n_stints": res.n_stints, "dzone_seconds": res.dzone_seconds,
            "first_exit_seconds": res.first_exit_seconds,
        })
    return pd.DataFrame(rows, columns=SHIFT_COLUMNS)


def build_shift_table(season: int = SEASON, params: ZoneParams | None = None, client: NHLClient | None = None,
                      max_workers: int = 6, max_games: int | None = None, verbose: bool = True) -> pd.DataFrame:
    """Every defenseman shift of every completed regular-season game.
    Failed games are listed in the returned frame's .attrs['failed_games']; simply
    call again to retry them (finished games come from the on-disk cache)."""
    params = params or ZoneParams()
    client = client or NHLClient()
    games = client.season_games(season)
    if max_games:
        games = games.head(max_games)
    if verbose:
        print(f"{len(games)} completed regular-season games for {season_label(season)}")

    def work(game_id):
        return analyze_game(client.play_by_play(game_id), client.shift_chart(game_id), params)

    frames, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(work, gid): gid for gid in games.game_id}
        for i, fut in enumerate(as_completed(futures), 1):
            gid = futures[fut]
            try:
                df = fut.result()
                if df.empty:
                    failed.append((gid, "no defenseman shifts found"))
                else:
                    frames.append(df)
            except Exception as exc:
                failed.append((gid, repr(exc)))
            if verbose and (i % 100 == 0 or i == len(futures)):
                print(f"  processed {i}/{len(futures)} games ({len(failed)} failed)")
    if not frames:
        raise RuntimeError(f"No games could be processed. First errors: {failed[:3]}")
    out = pd.concat(frames, ignore_index=True)
    out["game_date"] = pd.to_datetime(out["game_date"])
    out = out.sort_values(["game_date", "game_id", "team", "player", "start_s"]).reset_index(drop=True)
    out.attrs["failed_games"] = failed
    if failed and verbose:
        print(f"WARNING: {len(failed)} games failed (see .attrs['failed_games']); re-run to retry them.")
    return out


# --------------------------------------------------------------------------- #
# Season summary: one row per (team, player) -> one dot per team a player played for
# --------------------------------------------------------------------------- #
def summarize(shifts: pd.DataFrame, min_games: int = MIN_GAMES, time_basis: str = "exit") -> pd.DataFrame:
    """Aggregate shift rows to (team, player).

    Because every shift already carries the team he played for in that game, a
    player traded mid-season yields one row per team, each built only from his
    games with that team.

    time_basis
      'exit' (default): avg_time_s = mean length of the first exiting stint, over shifts with an
                        observed exit  -> "how long it takes him to get out when he does".
      'all'           : avg_time_s = mean total D-zone time over every D-zone shift (the original
                        avg-shift-D-zone-time definition; includes shifts with no observed exit).

    no_exit_pct = 100 * (D-zone shifts without an observed exit) / (D-zone shifts).
    Shifts that never touch the D-zone are in neither numerator nor denominator.
    """
    if time_basis not in ("exit", "all"):
        raise ValueError("time_basis must be 'exit' or 'all'")
    keys = ["team", "player_id"]
    base = shifts.groupby(keys).agg(
        player=("player", "first"), gp=("game_id", "nunique"), shifts=("start_s", "size"),
        first_game=("game_date", "min"), last_game=("game_date", "max"),
    )
    dz = shifts[shifts.dz_shift].groupby(keys).agg(
        dz_shifts=("dz_shift", "size"), exit_shifts=("exited", "sum"),
        avg_dzone_seconds=("dzone_seconds", "mean"),
    )
    outcomes = (shifts[shifts.dz_shift].groupby(keys)["outcome"].value_counts().unstack(fill_value=0)
                .reindex(columns=["exit", "whistle", "open"], fill_value=0)
                .rename(columns={"whistle": "whistle_shifts", "open": "open_shifts"})[["whistle_shifts", "open_shifts"]])
    ex = shifts[shifts.exited].groupby(keys).agg(avg_exit_seconds=("first_exit_seconds", "mean"))

    out = base.join(dz).join(outcomes).join(ex)
    for col in ("dz_shifts", "exit_shifts", "whistle_shifts", "open_shifts"):
        out[col] = out[col].fillna(0).astype(int)
    out["no_exit_pct"] = np.where(out.dz_shifts > 0, 100 * (out.dz_shifts - out.exit_shifts) / out.dz_shifts.replace(0, np.nan), np.nan)
    out["avg_time_s"] = out["avg_exit_seconds"] if time_basis == "exit" else out["avg_dzone_seconds"]
    out["time_n"] = out["exit_shifts"] if time_basis == "exit" else out["dz_shifts"]

    teams_per_player = shifts.groupby("player_id")["team"].nunique()
    out = out.reset_index()
    out["multi_team"] = out["player_id"].map(teams_per_player) > 1
    out = out[(out.gp >= min_games) & (out.dz_shifts > 0)]   # keep 0-exit players; the chart just can't place them on x
    cols = ["team", "player", "player_id", "multi_team", "gp", "first_game", "last_game", "shifts", "dz_shifts",
            "exit_shifts", "whistle_shifts", "open_shifts", "avg_time_s", "time_n", "no_exit_pct",
            "avg_exit_seconds", "avg_dzone_seconds"]
    return out[cols].sort_values(["team", "player"]).reset_index(drop=True)


def traded_players(summary: pd.DataFrame) -> pd.DataFrame:
    """Players with more than one team in the summary, with the date span of each stint.
    Use it to eyeball that the split lines up with the actual trade date."""
    multi = summary[summary.multi_team]
    return (multi[["player", "team", "gp", "first_game", "last_game", "dz_shifts", "avg_time_s", "no_exit_pct"]]
            .sort_values(["player", "first_game"]).reset_index(drop=True))


# --------------------------------------------------------------------------- #
# Charts and tables
# --------------------------------------------------------------------------- #
def _league_reference(summary: pd.DataFrame) -> tuple[float, float]:
    no_exit = 100 * (summary.dz_shifts - summary.exit_shifts).sum() / summary.dz_shifts.sum()
    timed = summary[summary.avg_time_s.notna()]
    avg_t = (timed.avg_time_s * timed.time_n).sum() / timed.time_n.sum()
    return float(avg_t), float(no_exit)


def _limits(summary: pd.DataFrame, pad: float = 0.08):
    plottable = summary[summary.avg_time_s.notna()]
    x, y = plottable["avg_time_s"], plottable["no_exit_pct"]
    dx, dy = (x.max() - x.min()) or 1.0, (y.max() - y.min()) or 1.0
    return x.min() - pad * dx, x.max() + pad * dx, max(0.0, y.min() - pad * dy), min(100.0, y.max() + pad * dy)


def plot_team(summary: pd.DataFrame, team: str, ax=None, limits=None, season: int = SEASON):
    """Scatter of one team's defensemen. `summary` should be the FULL league summary so the
    axes and league-average guide lines are shared by every team's chart."""
    team_rows = summary[summary["team"] == team]
    if team_rows.empty:
        raise ValueError(f"No qualifying defensemen for {team} (check min_games).")
    df = team_rows[team_rows.avg_time_s.notna()]
    unplotted = team_rows[team_rows.avg_time_s.isna()].player.tolist()  # D-zone shifts but no observed exit
    if ax is None:
        _, ax = plt.subplots(figsize=(7.5, 5.5))
    limits = limits or _limits(summary)
    league_t, league_ne = _league_reference(summary)

    ax.axvline(league_t, color="lightgray", lw=1, ls="--", zorder=1)
    ax.axhline(league_ne, color="lightgray", lw=1, ls="--", zorder=1)
    ax.scatter(df.avg_time_s, df.no_exit_pct, s=28, color=TEAM_COLORS.get(team, "black"), zorder=3)
    dx, dy = 0.010 * (limits[1] - limits[0]), 0.012 * (limits[3] - limits[2])  # nudge labels off their dots
    texts = [ax.text(r.avg_time_s + dx, r.no_exit_pct + dy, r.player + ("*" if r.multi_team else ""), fontsize=7, zorder=4)
             for r in df.itertuples()]
    if adjust_text is not None and len(texts) > 1:
        try:
            adjust_text(texts, ax=ax, arrowprops=dict(arrowstyle="-", color="gray", lw=0.5))
        except Exception:  # adjustText's API differs between major versions; labels stay usable either way
            pass
    ax.set_xlim(limits[0], limits[1])
    ax.set_ylim(limits[2], limits[3])
    ax.set_xlabel("Avg. Time Taken to Clear the D-Zone (Sec)")
    ax.set_ylabel("% of D-Zone Shifts w/o a Successful D-Zone Clearance")
    ax.set_title(f"{team} Defensemen D-Zone Metrics {season_label(season)} Season")
    foot = "dashed lines = league average"
    if df.multi_team.any():
        foot += f"   |   * also played for another team; only games with {team} are included"
    ax.text(0.0, -0.12, foot, transform=ax.transAxes, fontsize=6.5, color="gray")
    if unplotted:
        ax.text(0.0, -0.17, "Not plotted (no observed exits): " + ", ".join(unplotted), transform=ax.transAxes,
                fontsize=6.5, color="gray")
    return ax


def team_table(summary: pd.DataFrame, team: str) -> pd.DataFrame:
    df = summary[summary["team"] == team].sort_values("no_exit_pct")
    return pd.DataFrame({
        "Name": df.player + np.where(df.multi_team, "*", ""),
        "GP": df.gp,
        "D-zone shifts": df.dz_shifts,
        "Avg. Time to Clear (Sec)": df.avg_time_s.round(2),
        "% D-Zone Shifts w/o a Clearance": df.no_exit_pct.round(1),
    }).reset_index(drop=True)


def build_team_chart(summary: pd.DataFrame, team: str, show: bool = True, season: int = SEASON) -> str:
    """Drop-in replacement for the old build_team_chart: draws the chart, returns the table as text."""
    fig, ax = plt.subplots(figsize=(7.5, 5.5))
    plot_team(summary, team, ax=ax, season=season)
    if show:
        plt.show()
    tbl = team_table(summary, team)
    return tabulate(tbl, headers="keys", tablefmt="plain", showindex=False) if tabulate else tbl.to_string(index=False)


def plot_all_teams(summary: pd.DataFrame, save_dir: Path | str = "charts", season: int = SEASON, show: bool = False) -> dict:
    """One PNG per team, identical axes for all of them. Returns {team: path}."""
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    limits, paths = _limits(summary), {}
    for team in sorted(summary["team"].unique()):
        fig, ax = plt.subplots(figsize=(7.5, 5.5))
        plot_team(summary, team, ax=ax, limits=limits, season=season)
        paths[team] = save_dir / f"{team}_dman_dzone_{season}.png"
        fig.savefig(paths[team], dpi=150, bbox_inches="tight")
        if show:
            plt.show()
        plt.close(fig)
    return paths


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def check_zone_conventions(pbp: dict) -> pd.DataFrame:
    """Empirically test which team's point of view each event type's zoneCode uses, by comparing it
    with the zone implied by x/y and homeTeamDefendingSide. Expected: 'owner_pct' ~100 for every
    event type except blocked-shot, where 'other_pct' ~100 (blocker's side). Run once on any
    game to confirm the feed still behaves that way before trusting a full-season run."""
    home_id, away_id = pbp["homeTeam"]["id"], pbp["awayTeam"]["id"]
    rows = []
    for p in pbp.get("plays", []):
        d = p.get("details") or {}
        x, z, owner, side = d.get("xCoord"), d.get("zoneCode"), d.get("eventOwnerTeamId"), p.get("homeTeamDefendingSide")
        if x is None or z is None or owner not in (home_id, away_id) or side not in ("left", "right"):
            continue
        if d.get("reason") == "teammate-blocked":  # blocker is on the owner's team, so it is zoned from the owner's side
            continue
        home_sign = -1 if side == "left" else 1          # x-sign of the end the home team defends
        owner_sign = home_sign if owner == home_id else -home_sign
        rel = x * owner_sign                              # > 0: toward the owner's own goal
        coord_zone = "D" if rel >= 25 else "O" if rel <= -25 else "N"
        if coord_zone == "N":                             # N matches both readings; uninformative
            continue
        other = "O" if coord_zone == "D" else "D"
        rows.append({"event": p.get("typeDescKey"), "owner_ok": z == coord_zone, "other_ok": z == other})
    if not rows:
        return pd.DataFrame(columns=["n", "owner_pct", "other_pct"])
    df = pd.DataFrame(rows)
    out = df.groupby("event").agg(n=("event", "size"), owner_pct=("owner_ok", "mean"), other_pct=("other_ok", "mean"))
    out[["owner_pct", "other_pct"]] = (out[["owner_pct", "other_pct"]] * 100).round(1)
    return out


if __name__ == "__main__":  # python NHL_DMan_Shift_Processor.py  -> CSVs + one chart per team
    table = build_shift_table()
    table.to_csv(f"dman_shifts_{SEASON}.csv", index=False)
    summ = summarize(table)
    summ.to_csv(f"dman_summary_{SEASON}.csv", index=False)
    print(f"Wrote {len(plot_all_teams(summ))} team charts to ./charts")