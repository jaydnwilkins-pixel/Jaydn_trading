"""PNG price chart for a trade decision (Telegram approvals).

~6 months of daily candles, SMA20/50, volume, and labelled horizontal lines
for entry / stop / target / last price — dark theme matching web/style.css,
sized and lettered to stay readable on a phone screen.

Rendering uses matplotlib's object API on the Agg canvas only (no pyplot
global state, no display), so it is safe to call from a background thread of
the dashboards process.
"""
import io
from datetime import datetime, timezone

# web/style.css palette
BG, PANEL, LINE = "#06090f", "#0b1220", "#16233b"
TEXT, DIM = "#d7e6f2", "#5d7590"
ACCENT, GOOD, BAD, WARN = "#35d6ff", "#26a69a", "#ef5350", "#f5b944"
BLUE, PURPLE = "#6ea8fe", "#b48cff"

LEVEL_STYLE = {                     # name -> (colour, line style, label)
    "target": (GOOD, "-", "target"),
    "entry": (BLUE, "-", "entry"),
    "stop": (BAD, "-", "stop"),
    "last": (WARN, "--", "last"),
}


def _bars(chart):
    """(timestamp, o, h, l, c, v) rows with every price present."""
    rows = []
    ts = chart.get("timestamps") or []
    cols = [chart.get(k) or [] for k in ("open", "high", "low", "close",
                                         "volume")]
    for i, t in enumerate(ts):
        try:
            o, h, l, c = (cols[j][i] for j in range(4))
        except IndexError:
            break
        if None in (o, h, l, c):
            continue
        v = cols[4][i] if i < len(cols[4]) else None
        rows.append((t, float(o), float(h), float(l), float(c),
                     float(v or 0)))
    return rows


def _sma(closes, n):
    out, s = [], 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def _fmt(x):
    if x is None:
        return "—"
    return f"{x:,.2f}" if abs(x) < 10_000 else f"{x:,.0f}"


def render_chart(chart: dict, *, title: str, subtitle: str = "",
                 levels: dict | None = None, currency: str = "") -> bytes:
    """PNG bytes. `chart` is Market.chart() output; `levels` maps any of
    entry/stop/target/last to a price (None/missing are skipped). Raises
    ValueError when there are too few bars to draw anything useful."""
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    bars = _bars(chart)
    if len(bars) < 5:
        raise ValueError(f"only {len(bars)} usable bars — nothing to chart")
    n = len(bars)
    xs = list(range(n))
    opens = [b[1] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    levels = {k: float(v) for k, v in (levels or {}).items()
              if v is not None and k in LEVEL_STYLE}

    fig = Figure(figsize=(10, 6.6), dpi=110, facecolor=BG)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.3, 1], hspace=0.04,
                          left=0.075, right=0.82, top=0.88, bottom=0.08)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)
    for a in (ax, axv):
        a.set_facecolor(PANEL)
        for s in a.spines.values():
            s.set_color(LINE)
        a.tick_params(colors=DIM, labelsize=11)
        a.grid(True, color=LINE, linewidth=0.7)
        a.set_axisbelow(True)

    # candles
    w = 0.62
    for x, o, h, l, c in zip(xs, opens, highs, lows, closes):
        col = GOOD if c >= o else BAD
        ax.vlines(x, l, h, color=col, linewidth=1.0, zorder=2)
        ax.add_patch(Rectangle((x - w / 2, min(o, c)), w,
                               max(abs(c - o), (h - l) * 0.01 or 1e-9),
                               facecolor=col, edgecolor=col, linewidth=0.6,
                               zorder=3))
    for period, col in ((20, ACCENT), (50, PURPLE)):
        s = _sma(closes, period)
        pts = [(x, y) for x, y in zip(xs, s) if y is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=col,
                    linewidth=1.8, label=f"SMA{period}", zorder=4)

    # y-range must include every level, or a far stop/target vanishes
    lo = min(lows + list(levels.values()))
    hi = max(highs + list(levels.values()))
    pad = (hi - lo) * 0.06 or hi * 0.02 or 1
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, n + 0.5)

    # level lines, labelled in the right margin; labels are nudged apart so
    # entry and last (often close together) never print on top of each other
    order = sorted(levels.items(), key=lambda kv: kv[1])
    span = (hi + pad) - (lo - pad)
    min_gap = span * 0.07
    placed = []
    for name, y in order:
        ly = y if not placed or y - placed[-1] >= min_gap \
            else placed[-1] + min_gap
        placed.append(ly)
        col, ls, label = LEVEL_STYLE[name]
        ax.axhline(y, color=col, linestyle=ls, linewidth=1.6, alpha=0.95,
                   zorder=5)
        ax.annotate(f"{label} {_fmt(y)}", xy=(1.0, y),
                    xycoords=("axes fraction", "data"),
                    xytext=(1.02, ly), textcoords=("axes fraction", "data"),
                    color=BG, fontsize=11.5, fontweight="bold", va="center",
                    ha="left", annotation_clip=False,
                    bbox=dict(boxstyle="round,pad=0.25", fc=col, ec=col),
                    arrowprops=dict(arrowstyle="-", color=col, lw=1.0))
    leg = ax.legend(loc="upper left", fontsize=11, frameon=True,
                    facecolor=PANEL, edgecolor=LINE)
    for t in leg.get_texts():
        t.set_color(TEXT)

    # volume
    axv.bar(xs, vols, width=w,
            color=[GOOD if c >= o else BAD for o, c in zip(opens, closes)],
            alpha=0.7)
    axv.set_ylim(0, (max(vols) or 1) * 1.15)
    axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: (f"{v / 1e9:.1f}B" if v >= 1e9 else f"{v / 1e6:.0f}M"
                      if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3
                      else f"{v:.0f}")))
    axv.text(0.005, 0.9, "volume", transform=axv.transAxes, color=DIM,
             fontsize=10, va="top")
    ax.tick_params(labelbottom=False)

    # month ticks on the bar index (no weekend gaps)
    ticks, labels, last_m = [], [], None
    for x, b in zip(xs, bars):
        d = datetime.fromtimestamp(b[0], tz=timezone.utc)
        if d.month != last_m:
            if last_m is not None:
                ticks.append(x)
                labels.append(d.strftime("%b"))
            last_m = d.month
    axv.set_xticks(ticks)
    axv.set_xticklabels(labels)

    fig.text(0.075, 0.95, title, color=TEXT, fontsize=17, fontweight="bold",
             va="center")
    if subtitle:
        fig.text(0.075, 0.905, subtitle, color=DIM, fontsize=12, va="center")
    if currency:
        fig.text(0.82, 0.905, currency, color=DIM, fontsize=11, va="center",
                 ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return buf.getvalue()
