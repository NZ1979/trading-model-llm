#!/usr/bin/env python3
"""Render the SNDK structure dashboard to a self-contained HTML file.

    python -m scripts.sndk_dashboard
    python -m scripts.sndk_dashboard --symbol SNDK --out data/live/SNDK_dashboard.html

Reads only what is already on disk — no network, no vendor call:

    data/chains/chains.db   walls, gamma exposure, day-over-day OI change
    data/bars/bars.db       session extremes, pre-market setup, % from close
    data/live/<SYM>.json    live quote, if scripts.watch happens to be running

THE STALENESS RULE THIS PAGE IS BUILT AROUND
--------------------------------------------
Open interest published in a chain fetched on day D is the close of D-1, and
outside market hours the newest chain available describes the PREVIOUS session.
Every options-derived number on this page is therefore backward-looking by at
least one session, and during pre/post-market by more.

That is not a footnote. Reading a gamma flip or a wall as "where price is being
pushed right now" when it describes a two-day-old book is the single easiest way
to be confidently wrong. So every options panel carries an explicit as-of stamp,
the header states the age in sessions, and the banner turns amber once the chain
is more than one session behind.

Equity panels carry their own separate stamp. The two ages are different numbers
and the page never merges them.

WHAT IS MEASURED VERSUS WHAT IS ASSUMED
---------------------------------------
Measured: walls, OI change, session extremes, the pre-market bucket, realized
range. All arithmetic on stored data.

Assumed: that dealers are long calls and short puts, which is what makes the
gamma sign convention meaningful. See analysis/gamma_exposure.py. The flip is
rendered as a BAND rather than a line because the vendor-gamma and
Black-Scholes bases disagree — on the 2026-08-19 SNDK chain they implied
opposite regimes between roughly 1570 and 1590. A single line would assert a
precision the two models do not share.

Exit codes: 0 rendered, 2 missing inputs.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from analysis.gamma_exposure import from_stored, gamma_profile
from data.chain_store import ChainStore
from data.price_store import PriceStore

# --- palette: validated defaults, both modes (see dataviz references) -------
PAL = {
    "pos": ("#2a78d6", "#3987e5"),      # diverging warm pole: positive gamma
    "neg": ("#e34948", "#e66767"),      # diverging cool pole: negative gamma
    "call": ("#2a78d6", "#3987e5"),     # categorical slot 1
    "put": ("#eb6834", "#d95926"),      # categorical slot 2
}
STATUS = {"good": "#0ca30c", "warning": "#fab219",
          "serious": "#ec835a", "critical": "#d03b3b"}


# ---------------------------------------------------------------- helpers

def _fmt(v, nd=2, plus=False):
    if v is None:
        return "—"
    s = f"{v:,.{nd}f}"
    return f"+{s}" if plus and v > 0 else s


def _m(v):
    """Dollars to a compact $M string."""
    if v is None:
        return "—"
    return f"{v/1e6:+,.1f}M"


# ------------------------------------------------------------- pre-market

PM_BUCKETS = {
    "held": dict(label="opened near the pre-market high",
                 n=7, filled=2, median_co=-0.30),
    "fading": dict(label="already fading into the open",
                   n=9, filled=7, median_co=+2.76),
}
PM_MAE_MEDIAN = -3.53
PM_NEWHIGH = (11, 16)


def premarket_read(prev_close, pre_high, reg_open):
    """Classify today's open against the giveback buckets.

    Base rates come from 16 pre-market advances >= 2% measured on 23 SNDK
    sessions (2026-07-20 to 2026-08-19). n=7 and n=9 in the buckets: the
    29%-vs-78% split is a real difference in THIS sample and nothing more.
    The page prints n beside every rate for that reason.
    """
    if not (prev_close and pre_high):
        return None
    pm_gain = (pre_high - prev_close) / prev_close * 100
    out = {"pm_gain_pct": pm_gain, "qualifies": pm_gain >= 2.0}
    if reg_open:
        give = (pre_high - reg_open) / prev_close * 100
        out["giveback_pct"] = give
        out["bucket"] = "fading" if give >= 1.0 else "held"
        out["gap_pct"] = (reg_open - prev_close) / prev_close * 100
    return out


# ------------------------------------------------------------------- svg

def _svg_diverging(rows, *, width=560, row_h=17, dark=True, unit="M",
                   label_fmt=lambda k: f"{k:,.0f}"):
    """Horizontal diverging bars, zero-centred. rows = [(label, value), ...]."""
    if not rows:
        return "<p class='muted'>no data</p>"
    pos = PAL["pos"][1 if dark else 0]
    neg = PAL["neg"][1 if dark else 0]
    peak = max(abs(v) for _, v in rows) or 1.0
    h = len(rows) * row_h + 26
    mid = width * 0.52
    half = width * 0.42
    out = [f"<svg viewBox='0 0 {width} {h}' width='100%' "
           f"role='img' class='chart'>"]
    out.append(f"<line x1='{mid}' y1='6' x2='{mid}' y2='{h-20}' "
               f"class='axis-rule'/>")
    for i, (k, v) in enumerate(rows):
        y = 6 + i * row_h
        w = abs(v) / peak * half
        x = mid if v >= 0 else mid - w
        col = pos if v >= 0 else neg
        out.append(
            f"<rect x='{x:.1f}' y='{y}' width='{max(w,1):.1f}' height='{row_h-4}' "
            f"rx='2' fill='{col}' class='mark' "
            f"data-k='{escape(str(k))}' data-v='{v/1e6:+,.1f}{unit}'/>")
        out.append(f"<text x='{mid-half-6:.0f}' y='{y+row_h-7}' "
                   f"class='tick' text-anchor='end'>{label_fmt(k)}</text>")
    out.append(f"<text x='{mid}' y='{h-6}' class='tick' text-anchor='middle'>"
               f"0</text>")
    out.append("</svg>")
    return "".join(out)


def _svg_grouped(rows, *, width=560, row_h=22, dark=True):
    """Two-series horizontal bars. rows = [(label, call_v, put_v), ...]."""
    if not rows:
        return "<p class='muted'>no data</p>"
    c_col = PAL["call"][1 if dark else 0]
    p_col = PAL["put"][1 if dark else 0]
    peak = max(max(a, b) for _, a, b in rows) or 1.0
    h = len(rows) * row_h + 10
    left = 74
    span = width - left - 60
    out = [f"<svg viewBox='0 0 {width} {h}' width='100%' "
           f"role='img' class='chart'>"]
    for i, (k, a, b) in enumerate(rows):
        y = 4 + i * row_h
        bh = (row_h - 6) / 2 - 1        # 2px surface gap between the pair
        for val, col, off, side in ((a, c_col, 0, "call"), (b, p_col, bh + 2, "put")):
            w = val / peak * span
            out.append(
                f"<rect x='{left}' y='{y+off:.1f}' width='{max(w,1):.1f}' "
                f"height='{bh:.1f}' rx='2' fill='{col}' class='mark' "
                f"data-k='{k:,.0f} {side}' data-v='{val:,.0f} OI'/>")
        out.append(f"<text x='{left-8}' y='{y+row_h-9}' class='tick' "
                   f"text-anchor='end'>{k:,.0f}</text>")
        out.append(f"<text x='{left+max(a,b)/peak*span+6:.0f}' y='{y+row_h-9}' "
                   f"class='tick'>{max(a,b):,.0f}</text>")
    out.append("</svg>")
    return "".join(out)


def _svg_ladder(levels, lo, hi, *, flip_lo=None, flip_hi=None,
                width=920, height=380, dark=True):
    """Price ladder: one axis, marks placed by price, labels de-collided.

    Not a series chart, so colour here is status — where price is, what a level
    means — never series identity.

    Two things the first render got wrong, both visible only once rendered:
    labels within a few pixels of each other piled into an unreadable stack,
    and the flip band was emitted as a bare <rect> outside any <svg> element so
    it silently did not draw. Labels now cascade downward when they collide,
    and the band is part of this element.
    """
    if hi <= lo:
        return "<p class='muted'>no range</p>"
    pad = 22
    def y(p):
        return height - pad - (p - lo) / (hi - lo) * (height - 2 * pad)

    x0 = 14
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
           f"role='img' class='chart'>"]

    if flip_lo is not None and flip_hi is not None:
        top, bot = y(flip_hi), y(flip_lo)
        out.append(f"<rect x='{x0}' y='{top:.1f}' width='{width-x0-8}' "
                   f"height='{max(bot-top, 3):.1f}' fill='{STATUS['warning']}' "
                   f"opacity='0.15'/>")

    out.append(f"<line x1='{x0}' y1='{pad}' x2='{x0}' y2='{height-pad}' "
               f"class='axis-rule'/>")

    drawn = [(lab, price, kind, note, y(price))
             for lab, price, kind, note in levels
             if price is not None and lo <= price <= hi]
    drawn.sort(key=lambda r: r[4])

    last_label_y = -99.0
    for lab, price, kind, note, yy in drawn:
        col = {"now": STATUS["good"], "wall": PAL["call"][1 if dark else 0],
               "putwall": PAL["put"][1 if dark else 0],
               "flip": STATUS["warning"], "extreme": "#898781",
               "close": "#c3c2b7" if dark else "#52514e"}.get(kind, "#898781")
        emphasis = 2.5 if kind == "now" else 1.5
        out.append(f"<line x1='{x0}' y1='{yy:.1f}' x2='{width-8}' y2='{yy:.1f}' "
                   f"stroke='{col}' stroke-width='{emphasis}' opacity='0.85'/>")
        # The price rides IN the label, not in a separate tick gutter. A
        # gutter needs its own de-collision pass and the first render stacked
        # 1710/1700/1699 into an unreadable smear; one de-collided text run
        # cannot collide with itself.
        ly = yy - 5
        if ly - last_label_y < 13:
            ly = last_label_y + 13
        last_label_y = ly
        note_txt = f"  {note}" if note else ""
        out.append(f"<text x='{x0+10}' y='{ly:.1f}' class='lvl' fill='{col}'>"
                   f"{price:,.2f}<tspan class='lvl-name'> {escape(lab)}</tspan>"
                   f"<tspan class='muted'>{escape(note_txt)}</tspan></text>")
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ build

def build(symbol: str, chain_dir: str, bars_dir: str, live_path: Path):
    with ChainStore(chain_dir) as cs:
        sessions = cs.sessions(symbol)
        if not sessions:
            raise SystemExit(f"no stored chains for {symbol}")
        latest = sessions[-1]
        prior = cs.prior_session(symbol, latest)
        snap = cs.snapshot(symbol, latest)
        oi_rows = cs.oi_change(symbol, min_open_interest=250,
                               min_days_to_expiration=1) if prior else []

    with PriceStore(bars_dir) as ps:
        b_sessions = ps.sessions(symbol)
        today = b_sessions[-1] if b_sessions else None
        yday = b_sessions[-2] if len(b_sessions) > 1 else None
        ext = ps.session_extremes(symbol, today) if today else None
        prev = ps.session_extremes(symbol, yday) if yday else None

    live = None
    if live_path.exists():
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
        except Exception:
            live = None

    q = (live or {}).get("schwab") or {}
    spot = q.get("last_price") or (ext.regular_close if ext else None)
    prev_close = q.get("close_price") or (prev.regular_close if prev else None)
    underlying_at_fetch = snap[0]["underlying_price"] if snap else None
    if not spot:
        spot = underlying_at_fetch

    gr = from_stored(snap)
    prof = gamma_profile(gr, spot=spot) if spot else None

    walls = defaultdict(lambda: [0, 0])
    for r in snap:
        if (r["days_to_expiration"] or 0) < 1 or not r["open_interest"]:
            continue
        walls[r["strike"]][0 if r["put_call"] == "CALL" else 1] += r["open_interest"]

    oi_by_strike = defaultdict(float)
    for r in oi_rows:
        oi_by_strike[r.strike] += r.oi_change

    pm = premarket_read(prev_close,
                        ext.pre_high if ext else None,
                        ext.regular_open if ext else None)

    return dict(symbol=symbol, latest=latest, prior=prior, snap=snap,
                spot=spot, prev_close=prev_close,
                underlying_at_fetch=underlying_at_fetch,
                prof=prof, walls=walls, oi_by_strike=oi_by_strike,
                oi_rows=oi_rows, ext=ext, prev=prev, live=live,
                today=today, sessions=b_sessions)


def render(d: dict) -> str:
    sym = d["symbol"]
    prof, ext = d["prof"], d["ext"]
    spot, prev_close = d["spot"], d["prev_close"]
    pct = ((spot - prev_close) / prev_close * 100
           if spot and prev_close else None)

    # ---- staleness -------------------------------------------------------
    chain_date = datetime.strptime(d["latest"], "%Y%m%d").date()
    try:
        bars_date = datetime.strptime(d["today"], "%Y-%m-%d").date()
    except Exception:
        bars_date = chain_date
    lag = (bars_date - chain_date).days
    oi_describes = "the close before " + chain_date.isoformat()
    stale_level = "good" if lag <= 0 else ("warning" if lag == 1 else "critical")
    stale_msg = (f"Chain fetched {chain_date.isoformat()}. Its open interest "
                 f"describes {oi_describes}. Bars run to {bars_date.isoformat()}.")

    live_stamp = (d["live"] or {}).get("updated_at_mt", "not running")
    mkt_state = (d["live"] or {}).get("market_state", "unknown")

    # ---- ladder ----------------------------------------------------------
    top_calls = sorted(d["walls"].items(), key=lambda kv: -kv[1][0])[:3]
    top_puts = sorted(d["walls"].items(), key=lambda kv: -kv[1][1])[:3]
    levels = []
    if spot:
        levels.append(("price now", spot, "now", ""))
    if prev_close:
        levels.append(("prior close", prev_close, "close", ""))
    if ext:
        levels += [("session high", ext.session_high, "extreme", "all phases"),
                   ("session low", ext.session_low, "extreme", "all phases")]
        if ext.regular_high:
            levels.append(("regular high", ext.regular_high, "extreme", ""))
        if ext.regular_low:
            levels.append(("regular low", ext.regular_low, "extreme", ""))
    for k, (c, _p) in top_calls:
        levels.append((f"call wall", k, "wall", f"{c:,.0f} OI"))
    for k, (_c, p) in top_puts:
        levels.append((f"put wall", k, "putwall", f"{p:,.0f} OI"))

    # Anchor the scale on the session, not on the chain. A 3/8-coverage put
    # wall at 1000 is real open interest and a terrible axis bound: it squashed
    # everything that matters into a thin strip on the first render.
    anchors = [p for p in (spot, prev_close,
                           ext.session_high if ext else None,
                           ext.session_low if ext else None) if p]
    if not anchors:
        anchors = [p for _, p, _, _ in levels if p] or [1.0]
    a_lo, a_hi = min(anchors), max(anchors)
    span = max(a_hi - a_lo, a_hi * 0.04)
    lo, hi = a_lo - span * 0.35, a_hi + span * 0.35
    if prof and prof.flip and lo <= prof.flip <= hi:
        pass
    levels = [lv for lv in levels
              if lv[1] is None or lo <= lv[1] <= hi]

    flip_lo = flip_hi = None
    if prof and prof.flip:
        # band, not a line: the two gamma bases disagree, so the flip carries
        # the size of that disagreement as width
        spread = abs(prof.basis_divergence_pct or 0)
        w = min(max(spread / 100 * 0.01, 0.004), 0.02) * prof.flip
        flip_lo, flip_hi = prof.flip - w, prof.flip + w

    ladder = _svg_ladder(levels, lo, hi, flip_lo=flip_lo, flip_hi=flip_hi)

    # ---- charts ----------------------------------------------------------
    gex_rows = []
    if prof:
        gex_rows = [(s.strike, s.net_gex)
                    for s in prof.nearest_walls(12)]
        gex_rows.sort(key=lambda kv: -kv[0])
    gex_svg = _svg_diverging(gex_rows)

    wall_rows = sorted(d["walls"].items(),
                       key=lambda kv: -(kv[1][0] + kv[1][1]))[:10]
    wall_rows = [(k, v[0], v[1]) for k, v in wall_rows]
    wall_rows.sort(key=lambda r: -r[0])
    walls_svg = _svg_grouped(wall_rows)

    oi_rows_c = sorted(d["oi_by_strike"].items(),
                       key=lambda kv: -abs(kv[1]))[:12]
    oi_rows_c.sort(key=lambda kv: -kv[0])
    oi_svg = _svg_diverging(oi_rows_c, unit="", )

    # ---- tiles -----------------------------------------------------------
    pm = premarket_read(prev_close, ext.pre_high if ext else None,
                        ext.regular_open if ext else None)
    tiles = [
        ("last", _fmt(spot), mkt_state.lower()),
        ("vs prior close", f"{pct:+.2f}%" if pct is not None else "—",
         f"prior {_fmt(prev_close)}"),
        ("session high", _fmt(ext.session_high) if ext else "—",
         "incl. pre & post"),
        ("session low", _fmt(ext.session_low) if ext else "—",
         "incl. pre & post"),
        ("regular range", f"{ext.regular_range_pct:.2f}%"
         if ext and ext.regular_range_pct else "—", "median 8.84% over 23"),
        ("gamma regime", prof.regime.split(" ")[0] if prof else "—",
         "at spot, vendor basis"),
    ]

    def tile_html(t):
        k, v, sub = t
        return (f"<div class='tile'><div class='tk'>{escape(k)}</div>"
                f"<div class='tv'>{escape(v)}</div>"
                f"<div class='ts'>{escape(sub)}</div></div>")

    # ---- premarket panel -------------------------------------------------
    if pm and pm.get("bucket"):
        b = PM_BUCKETS[pm["bucket"]]
        pm_html = (
            f"<p class='big'>{escape(b['label'])}</p>"
            f"<p>pre-market advance <b>{pm['pm_gain_pct']:+.2f}%</b>, "
            f"giveback into the bell <b>{pm['giveback_pct']:+.2f}%</b>, "
            f"gap at the open <b>{pm['gap_pct']:+.2f}%</b></p>"
            f"<table class='tbl'><tbody>"
            f"<tr><td>round-tripped to prior close</td>"
            f"<td class='num'>{b['filled']}/{b['n']}</td>"
            f"<td class='num'>{b['filled']/b['n']*100:.0f}%</td></tr>"
            f"<tr><td>median close vs open</td>"
            f"<td class='num'>—</td><td class='num'>{b['median_co']:+.2f}%</td></tr>"
            f"<tr><td>median drawdown from the open (all 16)</td>"
            f"<td class='num'>—</td><td class='num'>{PM_MAE_MEDIAN:+.2f}%</td></tr>"
            f"<tr><td>made a new high above the pre-market high</td>"
            f"<td class='num'>{PM_NEWHIGH[0]}/{PM_NEWHIGH[1]}</td>"
            f"<td class='num'>{PM_NEWHIGH[0]/PM_NEWHIGH[1]*100:.0f}%</td></tr>"
            f"</tbody></table>"
            f"<p class='muted'>Base rates from 16 qualifying advances across 23 "
            f"sessions. n=7 and n=9 in the buckets — a promising split in this "
            f"sample, not an established edge.</p>")
    else:
        pm_html = ("<p class='muted'>No qualifying pre-market advance today "
                   "(threshold +2% vs prior close), or no pre-market bars "
                   "stored.</p>")

    # ---- gamma panel notes ----------------------------------------------
    if prof:
        agree = prof.regime_bases_agree
        if prof.vendor_gamma_stale:
            # Both totals are the same number here, so "they agree" is vacuous.
            # Saying so beats printing a 0% divergence that reads as consensus.
            agree_html = (
                f"<p class='warn'>vendor gamma NOT USED — spot has drifted "
                f"<b>{prof.spot_drift_pct:.2f}%</b> from the chain's underlying "
                f"of {_fmt(prof.chain_underlying)}. A gamma published at that "
                f"price does not describe these contracts here, so this is "
                f"Black-Scholes throughout: <b>{_m(prof.total_gex_bs)}</b>."
                f"<br>Re-fetch the chain to restore the vendor basis.</p>")
        else:
            agree_html = (
                f"<p class='{'warn' if agree is False else ''}'>"
                f"{'⚠ ' if agree is False else ''}"
                f"vendor basis <b>{_m(prof.total_gex)}</b> "
                f"({prof.vendor_gamma_rows:,} of {prof.contracts_used:,} rows) · "
                f"Black-Scholes <b>{_m(prof.total_gex_bs)}</b> · "
                f"divergence <b>{prof.basis_divergence_pct:+.0f}%</b> · "
                f"spot drift {prof.spot_drift_pct:.2f}%"
                + ("<br>The two bases imply <b>opposite regimes</b> at this "
                   "spot. Treat the flip as a zone, not a level."
                   if agree is False else "") +
                "</p>")
        cov = (f"<p class='muted'>{prof.contracts_used:,} contracts used · "
               f"{prof.contracts_skipped_no_oi:,} had no OI · "
               f"{prof.contracts_skipped_zero_dte:,} were 0DTE · strikes "
               f"{prof.strike_span[0]:,.0f}–{prof.strike_span[1]:,.0f}. "
               f"A GEX from a narrow strike window is a different number "
               f"from a wide one on the same chain.</p>")
    else:
        agree_html = cov = "<p class='muted'>no spot available</p>"

    oi_note = (f"<p class='muted'>Fetches {d['prior']} → {d['latest']}, so this "
               f"is positioning during the <b>{d['prior']}</b> session. "
               f"{len(d['oi_rows']):,} contracts cleared the 250-OI filter.</p>"
               if d["prior"] else
               "<p class='muted'>Only one stored chain — no day-over-day diff "
               "yet. Fetch again tomorrow.</p>")

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(sym)} structure</title>
<style>
:root {{ color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --rule:#383835; }}
:root[data-theme="light"] {{ color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --rule:#c3c2b7; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--plane); color:var(--ink);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:22px 18px 60px }}
h1 {{ font-size:20px; margin:0 0 2px; font-weight:600 }}
h2 {{ font-size:13px; margin:0 0 10px; font-weight:600; letter-spacing:.04em;
  text-transform:uppercase; color:var(--ink2) }}
.sub {{ color:var(--muted); font-size:12px; margin:0 0 16px }}
.banner {{ border-left:3px solid; padding:9px 13px; border-radius:0 6px 6px 0;
  background:var(--surface); margin:0 0 18px; font-size:13px }}
.banner.good {{ border-color:{STATUS['good']} }}
.banner.warning {{ border-color:{STATUS['warning']} }}
.banner.critical {{ border-color:{STATUS['critical']} }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px; margin-bottom:18px }}
.tile {{ background:var(--surface); border-radius:8px; padding:12px 14px }}
.tk {{ font-size:11px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.05em }}
.tv {{ font-size:26px; font-weight:600; margin:3px 0 1px }}
.ts {{ font-size:11px; color:var(--muted) }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
  gap:14px }}
.card {{ background:var(--surface); border-radius:10px; padding:15px 16px }}
.card.wide {{ grid-column:1/-1 }}
.chart {{ display:block; overflow:visible; height:auto }}
.tick {{ font-size:10px; fill:var(--muted); font-variant-numeric:tabular-nums }}
.lvl {{ font-size:11px; font-weight:600; font-variant-numeric:tabular-nums;
  paint-order:stroke; stroke:var(--surface); stroke-width:3px;
  stroke-linejoin:round }}
.lvl-name {{ font-weight:500 }}
.axis-rule {{ stroke:var(--rule); stroke-width:1 }}
.mark {{ transition:opacity .12s }}
.mark:hover {{ opacity:.75 }}
.legend {{ display:flex; gap:14px; font-size:11px; color:var(--ink2);
  margin:0 0 8px }}
.legend i {{ width:10px; height:10px; border-radius:2px; display:inline-block;
  margin-right:5px; vertical-align:-1px }}
.tbl {{ width:100%; border-collapse:collapse; font-size:12px; margin-top:6px }}
.tbl td, .tbl th {{ padding:4px 6px; border-bottom:1px solid var(--grid);
  text-align:left }}
.tbl .num {{ text-align:right; font-variant-numeric:tabular-nums }}
.muted {{ color:var(--muted); font-size:12px }}
.warn {{ color:{STATUS['warning']} }}
.big {{ font-size:16px; font-weight:600; margin:0 0 6px }}
details {{ margin-top:10px }} summary {{ cursor:pointer; font-size:12px;
  color:var(--muted) }}
#tip {{ position:fixed; pointer-events:none; background:var(--surface);
  border:1px solid var(--rule); border-radius:6px; padding:5px 9px;
  font-size:12px; opacity:0; transition:opacity .1s; z-index:9 }}
button.theme {{ background:none; border:1px solid var(--rule); color:var(--ink2);
  border-radius:6px; padding:4px 10px; font-size:12px; cursor:pointer;
  float:right }}
</style></head><body><div class="wrap">

<button class="theme" onclick="document.documentElement.dataset.theme=
 document.documentElement.dataset.theme==='dark'?'light':'dark'">theme</button>
<h1>{escape(sym)} structure</h1>
<p class="sub">generated {gen} · price feed {escape(str(live_stamp))} ·
 market {escape(str(mkt_state))}</p>

<div class="banner {stale_level}">
<b>Options data is backward-looking.</b> {escape(stale_msg)}
{'Outside market hours the newest chain describes the previous session; nothing on the options panels reflects the current tape.' if mkt_state not in ('REGULAR HOURS',) else ''}
</div>

<div class="tiles">{''.join(tile_html(t) for t in tiles)}</div>

<div class="grid">
  <div class="card wide"><h2>price against levels</h2>
    <div class="legend">
      <span><i style="background:{STATUS['good']}"></i>price now</span>
      <span><i style="background:{PAL['call'][1]}"></i>call wall</span>
      <span><i style="background:{PAL['put'][1]}"></i>put wall</span>
      <span><i style="background:{STATUS['warning']};opacity:.4"></i>gamma flip zone</span>
      <span><i style="background:#898781"></i>session extreme</span>
    </div>
    {ladder}
    <p class="muted">Flip drawn as a band whose width is the disagreement
      between the two gamma bases, not a line.</p>
  </div>

  <div class="card"><h2>net gamma exposure by strike</h2>
    <div class="legend">
      <span><i style="background:{PAL['pos'][1]}"></i>positive · dampening</span>
      <span><i style="background:{PAL['neg'][1]}"></i>negative · amplifying</span>
    </div>
    {gex_svg}
    {agree_html}{cov}
    <details><summary>table view</summary><table class="tbl">
    <tr><th>strike</th><th class="num">net GEX</th></tr>
    {''.join(f"<tr><td>{k:,.0f}</td><td class='num'>{_m(v)}</td></tr>" for k,v in gex_rows)}
    </table></details>
  </div>

  <div class="card"><h2>open-interest walls</h2>
    <div class="legend">
      <span><i style="background:{PAL['call'][1]}"></i>calls · resistance</span>
      <span><i style="background:{PAL['put'][1]}"></i>puts · support</span>
    </div>
    {walls_svg}
    <p class="muted">As of {escape(d['latest'])}, describing {escape(oi_describes)}.
      Totals depend on the strike window of the fetch.</p>
    <details><summary>table view</summary><table class="tbl">
    <tr><th>strike</th><th class="num">call OI</th><th class="num">put OI</th></tr>
    {''.join(f"<tr><td>{k:,.0f}</td><td class='num'>{a:,.0f}</td><td class='num'>{b:,.0f}</td></tr>" for k,a,b in wall_rows)}
    </table></details>
  </div>

  <div class="card"><h2>open-interest change by strike</h2>
    <div class="legend">
      <span><i style="background:{PAL['pos'][1]}"></i>positions opened</span>
      <span><i style="background:{PAL['neg'][1]}"></i>positions closed</span>
    </div>
    {oi_svg}
    {oi_note}
    <details><summary>table view</summary><table class="tbl">
    <tr><th>strike</th><th class="num">net OI change</th></tr>
    {''.join(f"<tr><td>{k:,.0f}</td><td class='num'>{v:+,.0f}</td></tr>" for k,v in oi_rows_c)}
    </table></details>
  </div>

  <div class="card"><h2>pre-market setup</h2>{pm_html}</div>
</div>

<div id="tip"></div>
<script>
const tip=document.getElementById('tip');
document.querySelectorAll('.mark').forEach(m=>{{
  const show=e=>{{tip.textContent=m.dataset.k+'  '+m.dataset.v;
    tip.style.opacity=1;tip.style.left=(e.clientX+14)+'px';
    tip.style.top=(e.clientY-10)+'px';}};
  m.addEventListener('mousemove',show);
  m.addEventListener('mouseenter',show);
  m.addEventListener('mouseleave',()=>tip.style.opacity=0);
}});
</script>
</div></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sndk_dashboard")
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--chain-dir", default="data/chains")
    ap.add_argument("--bars-dir", default="data/bars")
    ap.add_argument("--live", default=None,
                    help="path to the watcher JSON (default data/live/<SYM>.json)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    sym = args.symbol.upper()
    live = Path(args.live) if args.live else Path("data/live") / f"{sym}.json"
    out = Path(args.out) if args.out else Path("data/live") / f"{sym}_dashboard.html"

    try:
        d = build(sym, args.chain_dir, args.bars_dir, live)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d), encoding="utf-8")

    prof = d["prof"]
    print(f"{sym}: wrote {out}")
    print(f"  chain {d['latest']} (prior {d['prior']}) · "
          f"bars through {d['today']} · spot {_fmt(d['spot'])}")
    if prof:
        flip = f"{prof.flip:,.2f}" if prof.flip else "outside window"
        print(f"  GEX vendor {_m(prof.total_gex)} · BS {_m(prof.total_gex_bs)} · "
              f"flip {flip} · bases agree {prof.regime_bases_agree}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
