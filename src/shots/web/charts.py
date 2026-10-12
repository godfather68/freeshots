"""Server-rendered SVG: the probability curve P(total shots >= n) for one match."""

from __future__ import annotations

import math

from markupsafe import Markup

W, H, L, R, T, B = 640, 300, 44, 40, 14, 38
LINES = (0.70, 0.80, 0.90)


def curve_svg(ns: list[int], ps: list[float], selected: int | None, bounds: dict[int, int | None]) -> Markup:
    """ns/ps: the stored curve. selected: the user's N. bounds: {70: lb_70, 80: lb_80, 90: lb_90}."""
    pw, ph = W - L - R, H - T - B
    x0, x1 = ns[0], ns[-1]
    X = lambda n: L + (n - x0) / (x1 - x0) * pw
    Y = lambda p: T + (1 - p) * ph
    pts = [(n, p) for n, p in zip(ns, ps) if p is not None and not math.isnan(p)]
    s = [f'<svg class="curve" viewBox="0 0 {W} {H}" role="img" aria-labelledby="ct cd">'
         '<title id="ct">Probability of reaching each total-shots count</title>'
         '<desc id="cd">Chance that the match has at least n total shots, for n from '
         f'{x0} to {x1}. The curve falls as n rises.</desc>']
    # mown-stripe pitch backdrop: alternating bands every 5 shots
    for i, n in enumerate(range(x0, x1, 5)):
        if i % 2 == 0:
            s.append(f'<rect class="mow" x="{X(n):.1f}" y="{T}" width="{X(min(n + 5, x1)) - X(n):.1f}" height="{ph}"/>')
    for t in (0, .25, .5, .75, 1):
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="ax" x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>')
    for n in range(x0, x1 + 1, 5):
        s.append(f'<text class="ax" x="{X(n):.1f}" y="{H - B + 16}" text-anchor="middle">{n}</text>')
    s.append(f'<text class="ax" x="{L + pw / 2}" y="{H - 4}" text-anchor="middle">total shots n (both teams)</text>')
    for q in LINES:
        s.append(f'<line class="qline" x1="{L}" x2="{W - R}" y1="{Y(q):.1f}" y2="{Y(q):.1f}"/>'
                 f'<text class="qlab" x="{W - R + 4}" y="{Y(q) + 4:.1f}">{q:.0%}</text>')
    if len(pts) > 1:
        line = " ".join(f"{X(n):.1f},{Y(p):.1f}" for n, p in pts)
        s.append(f'<polygon class="area" points="{X(pts[0][0]):.1f},{Y(0):.1f} {line} {X(pts[-1][0]):.1f},{Y(0):.1f}"/>'
                 f'<polyline class="cline" points="{line}"/>')
    lookup = dict(pts)
    for q, lb in bounds.items():
        if lb is not None and int(lb) in lookup:
            n = int(lb)
            s.append(f'<circle class="bdot" cx="{X(n):.1f}" cy="{Y(lookup[n]):.1f}" r="4.5">'
                     f'<title>{q}% sure: at least {n} shots (P = {lookup[n]:.0%})</title></circle>')
    if selected is not None and selected in lookup:
        p = lookup[selected]
        left = X(selected) > W - 150
        s.append(f'<line class="sel" x1="{X(selected):.1f}" x2="{X(selected):.1f}" y1="{T}" y2="{Y(0):.1f}"/>'
                 f'<circle class="sdot" cx="{X(selected):.1f}" cy="{Y(p):.1f}" r="6"/>'
                 f'<text class="slab" x="{X(selected) + (-10 if left else 10):.1f}" y="{max(Y(p) - 10, T + 12):.1f}" '
                 f'text-anchor="{"end" if left else "start"}">{selected}+ shots: {p:.0%}</text>')
    s.append("</svg>")
    return Markup("".join(s))


def team_curve_svg(ns: list[int], home: list[float], away: list[float], selected: int | None,
                   home_name: str, away_name: str) -> Markup:
    """Both teams' P(team shots >= n) on one axis. Home is a solid line, away dashed (shape, not only colour)."""
    from html import escape

    pw, ph = W - L - R, H - T - B
    x0, x1 = ns[0], ns[-1]
    X = lambda n: L + (n - x0) / (x1 - x0) * pw
    Y = lambda p: T + (1 - p) * ph
    s = [f'<svg class="curve teamcurve" viewBox="0 0 {W} {H}" role="img" aria-labelledby="tt td">'
         '<title id="tt">Probability of each team reaching each shot count (experimental)</title>'
         f'<desc id="td">Chance that {escape(home_name)} (solid line) and {escape(away_name)} (dashed line) '
         f'each have at least n shots, for n from {x0} to {x1}.</desc>']
    for i, n in enumerate(range(x0, x1, 5)):
        if i % 2 == 0:
            s.append(f'<rect class="mow" x="{X(n):.1f}" y="{T}" width="{X(min(n + 5, x1)) - X(n):.1f}" height="{ph}"/>')
    for t in (0, .25, .5, .75, 1):
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="ax" x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>')
    for n in range(5 * math.ceil(x0 / 5), x1 + 1, 5):
        s.append(f'<text class="ax" x="{X(n):.1f}" y="{H - B + 16}" text-anchor="middle">{n}</text>')
    s.append(f'<text class="ax" x="{L + pw / 2}" y="{H - 4}" text-anchor="middle">shots by one team, n</text>')
    for q in LINES:
        s.append(f'<line class="qline" x1="{L}" x2="{W - R}" y1="{Y(q):.1f}" y2="{Y(q):.1f}"/>'
                 f'<text class="qlab" x="{W - R + 4}" y="{Y(q) + 4:.1f}">{q:.0%}</text>')
    for cls, ps in (("home", home), ("away", away)):
        pts = [(n, p) for n, p in zip(ns, ps) if p is not None and not math.isnan(p)]
        if len(pts) > 1:
            s.append(f'<polyline class="tline {cls}" points="' + " ".join(f"{X(n):.1f},{Y(p):.1f}" for n, p in pts) + '"/>')
    if selected is not None and x0 <= selected <= x1:
        s.append(f'<line class="sel" x1="{X(selected):.1f}" x2="{X(selected):.1f}" y1="{T}" y2="{Y(0):.1f}"/>')
        for cls, ps in (("home", home), ("away", away)):
            p = ps[selected - x0]
            if p is not None and not math.isnan(p):
                s.append(f'<circle class="tdot {cls}" cx="{X(selected):.1f}" cy="{Y(p):.1f}" r="5">'
                         f'<title>{selected}+ shots: {p:.0%}</title></circle>')
    s.append("</svg>")
    return Markup("".join(s))
