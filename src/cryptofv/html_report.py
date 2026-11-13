"""Self-contained HTML dashboard: one file, inline SVG charts, no dependencies.

Charts are drawn directly as SVG so the report opens anywhere, including
offline and in print. Floats appear only here, for pixel geometry; every
number shown in a table or tooltip is formatted from the Decimal ledger.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from html import escape

from . import analytics
from .engine import Result
from .models import ZERO

_METHOD = {"fifo": "FIFO", "lifo": "LIFO", "hifo": "Specific identification (highest cost first)"}
_ASSET_SLOTS = ["--series-4", "--series-5", "--series-6", "--series-7", "--series-8"]
_OTHER = "--other"

W, H = 720, 280
W_HALF = 500  # half-width cards get a narrower viewBox so text keeps its size
ML, MT, MB = 56, 14, 30


# ------------------------------------------------------------------ formatting
def _m(x: Decimal) -> str:
    return f"({-x:,.2f})" if x < ZERO else f"{x:,.2f}"


def _m0(x: Decimal) -> str:
    return f"({-x:,.0f})" if x < ZERO else f"{x:,.0f}"


def _signed(x: Decimal) -> str:
    return f"+{x:,.0f}" if x > ZERO else (f"−{-x:,.0f}" if x < ZERO else "0")


def _units(x: Decimal) -> str:
    return f"{x.normalize():f}"


def _compact(v: float, top: float) -> str:
    sign = "−" if v < 0 else ""
    a = abs(v)
    if top >= 1e6:
        return f"{sign}{a / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if top >= 1e4:
        return f"{sign}{a / 1e3:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{sign}{a:,.0f}"


def _ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first, last = math.floor(lo / step), math.ceil(hi / step)
    return [k * step for k in range(first, last + 1)]


def _tip(title: str, rows: list[tuple[str, str, str]]) -> str:
    """Encode a tooltip payload: title, then name/value/color-var rows."""
    body = "\n".join([title] + ["\t".join(r) for r in rows])
    return escape(body, quote=True).replace("\n", "&#10;").replace("\t", "&#9;")


# ---------------------------------------------------------------------- charts
class _Scale:
    def __init__(self, values: list[float], right: int, w: int = W):
        self.w = w
        lo, hi = min(values + [0.0]), max(values + [0.0])
        self.ticks = _ticks(lo, hi)
        self.lo, self.hi = self.ticks[0], self.ticks[-1]
        self.right = right
        self.pw, self.ph = w - ML - right, H - MT - MB

    def y(self, v: float) -> float:
        return MT + self.ph * (1 - (v - self.lo) / (self.hi - self.lo))

    def axis(self) -> str:
        top = max(abs(self.lo), abs(self.hi))
        out = []
        for t in self.ticks:
            y = self.y(t)
            cls = "base" if t == 0 else "grid"
            out.append(f'<line class="{cls}" x1="{ML}" x2="{self.w - self.right}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(f'<text class="tick" x="{ML - 8}" y="{y + 4:.1f}" text-anchor="end">{_compact(t, top)}</text>')
        return "".join(out)


def _x_labels(labels: list[str], xs: list[float]) -> str:
    return "".join(
        f'<text class="tick" x="{x:.1f}" y="{H - 8}" text-anchor="middle">{escape(l)}</text>'
        for l, x in zip(labels, xs)
    )


def _legend(items: list[tuple[str, str]], line: bool = False) -> str:
    key = "key-line" if line else "key-box"
    return '<div class="legend">' + "".join(
        f'<span><i class="{key}" style="background:var({c})"></i>{escape(n)}</span>' for n, c in items
    ) + "</div>"


def _svg(body: str, label: str, w: int = W) -> str:
    return (f'<svg viewBox="0 0 {w} {H}" role="img" aria-label="{escape(label, quote=True)}" '
            f'preserveAspectRatio="xMidYMid meet">{body}</svg>')


def _line_chart(labels: list[str], series: list[tuple[str, str, list[Decimal]]], label: str) -> str:
    right = 78
    sc = _Scale([float(v) for _, _, vals in series for v in vals], right)
    n = len(labels)
    xs = [ML + sc.pw / 2] if n == 1 else [ML + 12 + i * (sc.pw - 24) / (n - 1) for i in range(n)]
    band = sc.pw / max(n, 1)
    out = [sc.axis(), _x_labels(labels, xs)]

    for _, color, vals in series:
        pts = " ".join(f"{x:.1f},{sc.y(float(v)):.1f}" for x, v in zip(xs, vals))
        out.append(f'<polyline class="line" style="stroke:var({color})" points="{pts}"/>')
    for _, color, vals in series:
        out += [f'<circle class="dot" style="fill:var({color})" cx="{x:.1f}" cy="{sc.y(float(v)):.1f}" r="4"/>'
                for x, v in zip(xs, vals)]

    # End labels only where they do not collide; the legend always carries identity.
    ends = sorted(((sc.y(float(vals[-1])), vals[-1]) for _, _, vals in series), key=lambda e: e[0])
    clear = all(b[0] - a[0] >= 14 for a, b in zip(ends, ends[1:]))
    for y, v in (ends if clear else [(sc.y(float(series[0][2][-1])), series[0][2][-1])]):
        out.append(f'<text class="endlabel" x="{xs[-1] + 10:.1f}" y="{y + 4:.1f}">{_m0(v)}</text>')

    for i, (lab, x) in enumerate(zip(labels, xs)):
        tip = _tip(lab, [(name, _m0(vals[i]), color) for name, color, vals in series])
        out.append(
            f'<g class="hit" tabindex="0" data-tip="{tip}">'
            f'<rect x="{x - band / 2:.1f}" y="{MT}" width="{band:.1f}" height="{sc.ph}" fill="transparent"/>'
            f'<line class="xhair" x1="{x:.1f}" x2="{x:.1f}" y1="{MT}" y2="{MT + sc.ph}"/></g>'
        )
    return _svg("".join(out), label)


def _column(x: float, w: float, y0: float, y1: float, color: str, tip: str, r: float = 4) -> str:
    """A column from baseline y0 to value y1, rounded at the data end only."""
    h = abs(y1 - y0)
    if h < 0.5:
        return ""
    r = min(r, h, w / 2)
    s = -1 if y1 < y0 else 1  # -1 grows upward on screen
    d = (f"M{x:.1f},{y0:.1f} V{y1 - s * r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
         f"H{x + w - r:.1f} Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 - s * r:.1f} V{y0:.1f} Z")
    return f'<path class="bar" tabindex="0" style="fill:var({color})" d="{d}" data-tip="{tip}"/>'


def _grouped_chart(labels: list[str], series: list[tuple[str, str, list[Decimal]]], label: str) -> str:
    sc = _Scale([float(v) for _, _, vals in series for v in vals], 16, W_HALF)
    n, k = len(labels), len(series)
    band = sc.pw / max(n, 1)
    bw, gap = min(18.0, (band * 0.6 - 2 * (k - 1)) / k), 2.0
    group = k * bw + (k - 1) * gap
    xs = [ML + band * (i + 0.5) for i in range(n)]
    out = [sc.axis(), _x_labels(labels, xs)]
    y0 = sc.y(0)
    for i, x in enumerate(xs):
        for j, (name, color, vals) in enumerate(series):
            tip = _tip(labels[i], [(name, _signed(vals[i]), color)])
            out.append(_column(x - group / 2 + j * (bw + gap), bw, y0, sc.y(float(vals[i])), color, tip))
    return _svg("".join(out), label, W_HALF)


def _stacked_chart(labels: list[str], series: list[tuple[str, str, list[Decimal]]], label: str) -> str:
    totals = [sum((vals[i] for _, _, vals in series), ZERO) for i in range(len(labels))]
    sc = _Scale([float(t) for t in totals], 16, W_HALF)
    band = sc.pw / max(len(labels), 1)
    bw = min(24.0, band * 0.6)
    xs = [ML + band * (i + 0.5) for i in range(len(labels))]
    out = [sc.axis(), _x_labels(labels, xs)]
    for i, x in enumerate(xs):
        live = [(name, color, vals[i]) for name, color, vals in series if vals[i] > ZERO]
        base = ZERO
        for j, (name, color, v) in enumerate(live):
            y_bot, y_top = sc.y(float(base)), sc.y(float(base + v))
            base += v
            tip = _tip(labels[i], [(name, _m0(v), color), ("Total", _m0(totals[i]), "")])
            top = j == len(live) - 1
            y_bot -= 0 if j == 0 else 2  # 2px surface gap between segments
            if y_bot - y_top < 0.5:
                continue
            if top:
                out.append(_column(x - bw / 2, bw, y_bot, y_top, color, tip))
            else:
                out.append(f'<rect class="bar" tabindex="0" style="fill:var({color})" x="{x - bw / 2:.1f}" '
                           f'y="{y_top:.1f}" width="{bw:.1f}" height="{y_bot - y_top:.1f}" data-tip="{tip}"/>')
    return _svg("".join(out), label, W_HALF)


# ---------------------------------------------------------------------- tables
def _table(headers: list[str], rows: list[list[str]], total: bool = False, text_cols: int = 1) -> str:
    def cells(row, tag):
        return "".join(
            f'<{tag}{"" if i < text_cols else " class=num"}>{escape(c)}</{tag}>' for i, c in enumerate(row)
        )
    body = "".join(
        f'<tr{" class=total" if total and i == len(rows) - 1 else ""}>{cells(r, "td")}</tr>'
        for i, r in enumerate(rows)
    )
    return f'<div class="scroll"><table><thead><tr>{cells(headers, "th")}</tr></thead><tbody>{body}</tbody></table></div>'


def _rollforward_table(rows) -> str:
    cols = ["opening", "additions", "dispositions", "gains", "losses", "closing"]
    neg = {"dispositions", "losses"}
    body = [[r.asset] + [_m(-getattr(r, c) if c in neg else getattr(r, c)) for c in cols] for r in rows]
    tot = {c: sum((getattr(r, c) for r in rows), ZERO) for c in cols}
    body.append(["Total"] + [_m(-tot[c] if c in neg else tot[c]) for c in cols])
    return _table(["Asset", "Opening fair value", "Additions", "Dispositions", "Gains", "Losses",
                   "Closing fair value"], body, total=True)


def _tile(label: str, value: str, note: str) -> str:
    return (f'<div class="tile"><div class="tile-l">{escape(label)}</div>'
            f'<div class="tile-v">{escape(value)}</div><div class="tile-n">{escape(note)}</div></div>')


def _card(title: str, sub: str, body: str, wide: bool = False) -> str:
    return (f'<section class="card{" wide" if wide else ""}"><h2>{escape(title)}</h2>'
            f'<p class="sub">{escape(sub)}</p>{body}</section>')


def asset_series(result: Result) -> list[tuple[str, str, list[Decimal]]]:
    """Fair value per asset per period, folded to "Other" beyond the palette's slots."""
    ends = result.period_ends
    assets = sorted({h.asset for h in result.holdings})
    fv = {(h.period_end, h.asset): h.fair_value for h in result.holdings}
    keep = assets
    if len(assets) > len(_ASSET_SLOTS):
        ranked = sorted(assets, key=lambda a: -fv.get((ends[-1], a), ZERO))
        keep = sorted(ranked[:len(_ASSET_SLOTS) - 1])
    series = [(a, _ASSET_SLOTS[i], [fv.get((e, a), ZERO) for e in ends]) for i, a in enumerate(keep)]
    if len(keep) < len(assets):
        series.append(("Other", _OTHER, [
            sum((fv.get((e, a), ZERO) for a in assets if a not in keep), ZERO) for e in ends]))
    return series


# ------------------------------------------------------------------------ page
def to_html(result: Result, adoption_date: date | None = None, title: str = "Crypto asset fair value report") -> str:
    ends = result.period_ends
    if not ends:
        return _PAGE.format(title=escape(title), css=_CSS, js=_JS, body="<p>No reporting periods.</p>")
    last = ends[-1]
    labels = [e.isoformat() for e in ends]
    short = [e.strftime("%b %Y") for e in ends]
    comp = analytics.compare_models(result)
    latest = comp[-1]
    holdings = [h for h in result.holdings if h.period_end == last]
    passed = sum(c.passed for c in result.controls)
    ok = result.controls_passed

    parts = [
        '<header><div><h1>{}</h1><p class="sub">Reporting dates {} to {} · {} cost basis · purchase fees {} · USD</p></div>'
        '<div class="badge {}"><span aria-hidden="true">{}</span> {} of {} controls passed</div></header>'.format(
            escape(title), labels[0], labels[-1], escape(_METHOD[result.cost_method]),
            "capitalised" if result.capitalize_fees else "expensed",
            "good" if ok else "bad", "✓" if ok else "✕", passed, len(result.controls))
    ]

    unreal = latest.fair_value - latest.cost_basis
    parts.append('<div class="tiles">' + "".join([
        _tile("Fair value", _m0(latest.fair_value), f"at {labels[-1]}"),
        _tile("Unrealised gain or loss", _signed(unreal), f"against cost basis of {_m0(latest.cost_basis)}"),
        _tile("Gain or loss in latest period", _signed(latest.fv_gain_loss), "recognised in net income"),
        _tile("Above legacy carrying amount", _signed(latest.cumulative_difference),
              "fair value less cost-less-impairment"),
    ]) + "</div>")

    if adoption_date is not None:
        t = analytics.transition_adjustment(result, adoption_date)
        parts.append(
            f'<section class="card wide callout"><h2>Transition adjustment on adoption at {adoption_date.isoformat()}</h2>'
            f'<p>Carrying amount under the legacy model at {t.period_end.isoformat()} was <strong>{_m(t.legacy_carrying)}</strong> '
            f'against a fair value of <strong>{_m(t.fair_value)}</strong>. The pre-tax cumulative-effect adjustment to opening '
            f'retained earnings is <strong>{_m(t.cumulative_difference)}</strong>.</p>'
            + _table(["Account", "Debit", "Credit"], [
                ["Crypto assets, at fair value", _m(max(t.cumulative_difference, ZERO)), _m(max(-t.cumulative_difference, ZERO))],
                ["Retained earnings (opening)", _m(max(-t.cumulative_difference, ZERO)), _m(max(t.cumulative_difference, ZERO))],
            ]) + "</section>")

    # Chart 1: three measurement bases.
    bases = [("Fair value (ASC 350-60)", "--series-1", [c.fair_value for c in comp]),
             ("Cost basis", "--series-2", [c.cost_basis for c in comp]),
             ("Legacy cost less impairment", "--series-3", [c.legacy_carrying for c in comp])]
    parts.append(_card(
        "Carrying amount under three measurement bases",
        "The gap between the blue and green lines is value the legacy model could not recognise.",
        _legend([(n, c) for n, c, _ in bases], line=True) + _line_chart(short, bases, "Carrying amount by reporting date"),
        wide=True))

    # Chart 2: P&L under each model.
    pl = [("Fair value model", "--series-1", [c.fv_gain_loss for c in comp]),
          ("Legacy model", "--series-3", [c.legacy_gain_loss for c in comp])]
    parts.append(_card(
        "Gain or loss recognised each period",
        "Fair value records moves in both directions. The legacy model records declines and realised gains only.",
        _legend([(n, c) for n, c, _ in pl]) + _grouped_chart(short, pl, "Gain or loss by period under each model")))

    # Chart 3: composition.
    comp_series = asset_series(result)
    parts.append(_card(
        "Fair value by asset", "Holdings at each reporting date.",
        _legend([(n, c) for n, c, _ in comp_series]) + _stacked_chart(short, comp_series, "Fair value by asset and reporting date")))

    # Tables.
    rows = [[h.asset, _units(h.units), _m(h.cost_basis), _m(h.fair_value), _m(h.fair_value - h.cost_basis),
             f"{h.market} ({h.basis})", h.level] for h in holdings]
    rows.append(["Total", "", _m(latest.cost_basis), _m(latest.fair_value), _m(unreal), "", ""])
    parts.append(_card(f"Significant holdings at {labels[-1]}", "ASC 350-60-50-1: units, cost basis and fair value for each significant holding.",
                       _table(["Asset", "Units", "Cost basis", "Fair value", "Unrealised", "Market", "Hierarchy"], rows, total=True), wide=True))

    parts.append(_card(f"Rollforward for the period ended {labels[-1]}", "ASC 350-60-50-2: activity in crypto asset holdings.",
                       _rollforward_table([r for r in result.rollforward if r.period_end == last]), wide=True))

    parts.append(_card(
        "Fair value against the legacy model",
        "Legacy impairment uses the price history file where supplied." if result.has_price_history
        else "No price history supplied: legacy impairment is tested at reporting dates only and is understated.",
        _table(["Period end", "Fair value", "Legacy carrying", "Cumulative difference", "Fair value gain or loss",
                "Legacy gain or loss", "of which impairment", "Period difference"],
               [[c.period_end.isoformat(), _m(c.fair_value), _m(c.legacy_carrying), _m(c.cumulative_difference),
                 _m(c.fv_gain_loss), _m(c.legacy_gain_loss), _m(-c.legacy_impairment), _m(c.period_difference)] for c in comp]),
        wide=True))

    alts = [a for a in result.alternatives if a.period_end == last]
    parts.append(_card(
        "Market selection", "What each holding would be worth in every quoted market. Inaccessible markets cannot be used, whatever their volume.",
        _table(["Asset", "Market", "Status", "Price", "Fair value if used", "Difference"],
               [[a.asset, a.market, "Selected" if a.selected else ("Accessible" if a.accessible else "Not accessible"),
                 _m(a.price), _m(a.fair_value), _m(a.difference)] for a in alts], text_cols=3), wide=True))

    parts.append(_card(
        "Price sensitivity", f"Pre-tax effect on net income of a uniform move in all prices from {labels[-1]}.",
        _table(["Price move", "Fair value", "Effect on pre-tax income"],
               [[f"{s.shock_pct:+d}%", _m(s.fair_value), _m(s.pre_tax_effect)] for s in analytics.price_shocks(result)])))

    parts.append(_card(
        "Controls", "Each control derives the same number two independent ways.",
        '<ul class="controls">' + "".join(
            f'<li><span class="mark {"good" if c.passed else "bad"}" aria-hidden="true">{"✓" if c.passed else "✕"}</span>'
            f'<div><strong>{escape(c.name)}</strong> <span class="state">{"Passed" if c.passed else "Failed"}</span>'
            f'<br><span class="sub">{escape(c.detail)}</span></div></li>' for c in result.controls) + "</ul>"))

    lots = [l for l in result.lots if l.period_end == last]
    parts.append(_card(
        f"Open lots at {labels[-1]}", "The subledger behind the holdings table.",
        _table(["Asset", "Lot", "Acquired", "Units", "Cost basis", "Fair value", "Unrealised", "Legacy carrying"],
               [[l.asset, l.lot_id, l.acquired.isoformat(), _units(l.units), _m(l.cost_basis), _m(l.fair_value),
                 _m(l.unrealized), _m(l.legacy_carrying)] for l in lots], text_cols=3), wide=True))

    tb = analytics.trial_balance(result.journal)
    tb_rows = [[t.account, _m(t.debit) if t.debit else "", _m(t.credit) if t.credit else ""] for t in tb]
    tb_rows.append(["Total", _m(sum((t.debit for t in tb), ZERO)), _m(sum((t.credit for t in tb), ZERO))])
    parts.append(_card("Trial balance", f"Cumulative balances from {len({j.entry_id for j in result.journal})} journal entries.",
                       _table(["Account", "Debit", "Credit"], tb_rows, total=True)))

    if len(ends) > 1:
        earlier = "".join(
            f"<details><summary>Period ended {e.isoformat()}</summary>"
            + _rollforward_table([r for r in result.rollforward if r.period_end == e]) + "</details>"
            for e in reversed(ends[:-1]))
        parts.append(_card("Earlier rollforwards", "One table per prior reporting period.", earlier, wide=True))

    parts.append('<footer>Generated by cryptofv. Figures are derived from the inputs supplied and are not audited.</footer>')
    return _PAGE.format(title=escape(title), css=_CSS, js=_JS, body="\n".join(parts))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><main class="viz-root">
{body}
</main><div id="tip" class="tip" hidden></div><script>{js}</script></body></html>
"""

_DARK = """color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--base:#383835;--border:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;
--series-6:#008300;--series-7:#9085e9;--series-8:#e66767;--other:#6b6a65;--good:#0ca30c;--critical:#e66767;"""

_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--base:#c3c2b7;--border:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;
--series-6:#008300;--series-7:#6250d6;--series-8:#e34948;--other:#a3a29b;--good:#0ca30c;--critical:#d03b3b;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){""" + _DARK + """}}
:root[data-theme="dark"]{""" + _DARK + """}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:28px 20px 48px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
header,.tiles,.wide,footer{grid-column:1/-1}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
h1{font-size:22px;margin:0 0 2px;font-weight:650}
h2{font-size:15px;margin:0;font-weight:650}
.sub{color:var(--text-secondary);margin:2px 0 12px;font-size:13px}
.badge{display:inline-flex;gap:6px;align-items:center;padding:6px 12px;border-radius:999px;border:1px solid var(--border);
background:var(--surface-1);font-weight:600;white-space:nowrap}
.badge.good span,.mark.good{color:var(--good)} .badge.bad span,.mark.bad{color:var(--critical)}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.tile,.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;min-width:0}
.tile-l{color:var(--text-secondary);font-size:13px}
.tile-v{font-size:26px;font-weight:650;margin:2px 0;overflow-wrap:anywhere}
.tile-n{color:var(--muted);font-size:12px}
.callout p{margin:8px 0 12px;max-width:80ch}
svg{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1} .base{stroke:var(--base);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.endlabel{fill:var(--text-primary);font-size:12px;font-weight:600}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.dot{stroke:var(--surface-1);stroke-width:2}
.xhair{stroke:var(--base);stroke-width:1;opacity:0;pointer-events:none}
.hit:hover .xhair,.hit:focus .xhair{opacity:1} .hit,.bar{outline:none}
.bar:hover,.bar:focus{filter:brightness(1.15)} .bar:focus-visible{stroke:var(--text-primary);stroke-width:1.5}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--text-secondary);font-size:12px;margin-bottom:8px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.key-box{width:10px;height:10px;border-radius:2px;display:inline-block}
.key-line{width:14px;height:2px;border-radius:1px;display:inline-block}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--grid);white-space:nowrap}
th{color:var(--text-secondary);font-weight:600;font-size:12px}
.num{text-align:right;font-variant-numeric:tabular-nums}
tr.total td{font-weight:650;border-top:1px solid var(--base);border-bottom:none}
.controls{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.controls li{display:flex;gap:10px;align-items:flex-start}
.mark{font-weight:700;font-size:15px;line-height:1.4} .state{color:var(--text-secondary);font-size:12px;margin-left:4px}
.controls .sub{margin:0}
details{border-top:1px solid var(--grid);padding:8px 0} summary{cursor:pointer;font-weight:600}
details .scroll{margin-top:8px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
.tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.16);max-width:280px}
.tip-h{color:var(--text-secondary);margin-bottom:4px}
.tip-r{display:flex;align-items:center;gap:6px;white-space:nowrap} .tip-r strong{font-variant-numeric:tabular-nums}
.tip-r span:last-child{color:var(--text-secondary)}
.tip-k{width:12px;height:2px;border-radius:1px;flex:none}
@media (max-width:820px){main{grid-template-columns:minmax(0,1fr)}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media print{body{background:#fff}.card,.tile{break-inside:avoid}details{display:none}}
"""

_JS = """
(function(){
  var tip=document.getElementById('tip');
  function show(el,x,y){
    var lines=el.dataset.tip.split('\\n');
    tip.replaceChildren();
    var h=document.createElement('div');h.className='tip-h';h.textContent=lines[0];tip.appendChild(h);
    lines.slice(1).forEach(function(l){
      var p=l.split('\\t'),r=document.createElement('div');r.className='tip-r';
      var k=document.createElement('span');k.className='tip-k';if(p[2])k.style.background='var('+p[2]+')';
      var v=document.createElement('strong');v.textContent=p[1];
      var n=document.createElement('span');n.textContent=p[0];
      r.append(k,v,n);tip.appendChild(r);
    });
    tip.hidden=false;
    var b=tip.getBoundingClientRect(),left=x+14,top=y+14;
    if(left+b.width>innerWidth-8)left=x-b.width-14;
    if(top+b.height>innerHeight-8)top=y-b.height-14;
    tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px';
  }
  function hide(){tip.hidden=true;}
  document.querySelectorAll('[data-tip]').forEach(function(el){
    el.addEventListener('pointermove',function(e){show(el,e.clientX,e.clientY);});
    el.addEventListener('pointerleave',hide);
    el.addEventListener('focus',function(){var b=el.getBoundingClientRect();show(el,b.left+b.width/2,b.top+b.height/2);});
    el.addEventListener('blur',hide);
  });
})();
"""
