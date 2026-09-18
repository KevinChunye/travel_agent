"""Price-history charts (PNG, matplotlib, no network access).

Renders our own observed price history for a watch as a small PNG
suitable for sending as WhatsApp media. Single data series (observed
best price), a neutral dashed target-price reference line when set, and
the current price called out directly — no legend box needed for one
series; the title carries identity.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless; never opens a display
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from src.models.watch import PriceObservation, PriceWatch  # noqa: E402

# Single accessible hue for the one series; neutral ink for text/grid.
_SERIES = "#1D4ED8"
_INK = "#1F2937"
_MUTED = "#6B7280"
_GRID = "#E5E7EB"
_TARGET = "#9CA3AF"
_SURFACE = "#FFFFFF"


def render_price_chart(
    watch: PriceWatch,
    observations: list[PriceObservation],
    out_path: str | Path,
) -> str:
    """Render the watch's observed price history to a PNG. Returns the path."""
    if not observations:
        raise ValueError("No price observations recorded for this watch yet")

    xs = [o.observed_at for o in observations]
    ys = [o.best_price for o in observations]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)

    ax.plot(xs, ys, color=_SERIES, linewidth=2, zorder=3)
    ax.scatter(xs, ys, color=_SERIES, s=24, zorder=4)

    # Current price: direct label on the last point (selective labeling).
    ax.annotate(
        f"${ys[-1]:,.0f}",
        (xs[-1], ys[-1]),
        textcoords="offset points",
        xytext=(8, 6),
        color=_INK,
        fontsize=10,
        fontweight="bold",
    )
    lowest = min(ys)
    if ys.index(lowest) != len(ys) - 1:
        ax.annotate(
            f"low ${lowest:,.0f}",
            (xs[ys.index(lowest)], lowest),
            textcoords="offset points",
            xytext=(0, -14),
            color=_MUTED,
            fontsize=9,
            ha="center",
        )

    if watch.target_price is not None:
        ax.axhline(watch.target_price, color=_TARGET, linewidth=1.5,
                   linestyle=(0, (4, 3)), zorder=2)
        ax.annotate(
            f"target ${watch.target_price:,.0f}",
            (xs[0], watch.target_price),
            textcoords="offset points",
            xytext=(0, 5),
            color=_MUTED,
            fontsize=9,
        )

    dates = watch.request.outbound_date.start.strftime("%b %d") if watch.request.outbound_date else ""
    if watch.request.return_date:
        dates += f" – {watch.request.return_date.start.strftime('%b %d')}"
    ax.set_title(
        f"{watch.origin} → {watch.destination} · {dates} · observed best price",
        color=_INK, fontsize=11, loc="left",
    )

    ax.grid(axis="y", color=_GRID, linewidth=0.8, zorder=1)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(_GRID)
    ax.tick_params(colors=_MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_ylabel(f"Price ({watch.currency})", color=_MUTED, fontsize=9)
    ax.margins(x=0.06, y=0.18)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, format="png", facecolor=_SURFACE)
    plt.close(fig)
    return str(out_path)
