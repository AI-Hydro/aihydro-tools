---
name: hydro-visualization
description: Create publication-quality hydrological figures — hydrographs, flow duration curves, Budyko plots, exceedance probability charts, spatial maps, and multi-panel diagnostic displays. Use when the user asks for a plot, chart, figure, visualization, or wants to create a visual for a report, paper, or presentation. Applies AI-Hydro branding (dark theme, cyan accent, Poppins/Nunito fonts) by default.
when_to_use: When the user asks to "plot", "chart", "visualize", "graph", "show me", "create a figure", or wants publication-ready hydrological graphics
domain: interpretation
tools_used:
  - fetch_streamflow_data
  - extract_hydrological_signatures
  - extract_camels_attributes
tags:
  - visualization
  - matplotlib
  - plotting
  - publication
  - figures
---

# Hydrological Visualization Workflow

Create distinctive, publication-quality hydrological figures that avoid generic matplotlib defaults. Every figure should be immediately recognizable as an AI-Hydro output — dark-themed, cyan-accented, typographically refined.

## When This Workflow Applies

- User asks to plot, chart, or visualize any hydrological data
- User wants figures for a report, paper, thesis, or presentation
- User asks to "show me" results from a previous analysis
- User wants to compare catchments visually
- User wants diagnostic plots for model calibration

## AI-Hydro Brand Palette

Every figure uses this palette by default. The user can override, but start here.

```python
AIHYDRO_STYLE = {
    "bg_dark": "#0a0a15",
    "bg_panel": "#1a1a2e",
    "bg_card": "#0f0f1e",
    "accent": "#00A3FF",
    "accent_gradient": "#00DDFF",
    "text_primary": "#e2e8f0",
    "text_muted": "#94a3b8",
    "text_accent": "#7dd3fc",
    "grid": "#ffffff10",
    "warn": "#fbbf24",
    "error": "#f87171",
    "success": "#34d399",
}
```

### Apply the style before any figure:

```python
import matplotlib.pyplot as plt
import matplotlib as mpl

plt.rcParams.update({
    "figure.facecolor": "#0a0a15",
    "axes.facecolor": "#1a1a2e",
    "axes.edgecolor": "#ffffff20",
    "axes.labelcolor": "#e2e8f0",
    "axes.grid": True,
    "grid.color": "#ffffff10",
    "grid.linestyle": "--",
    "grid.alpha": 0.3,
    "text.color": "#e2e8f0",
    "xtick.color": "#94a3b8",
    "ytick.color": "#94a3b8",
    "legend.facecolor": "#0f0f1e",
    "legend.edgecolor": "#ffffff20",
    "legend.labelcolor": "#e2e8f0",
    "font.family": "sans-serif",
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "figure.dpi": 150,
    "savefig.dpi": 200,
    "savefig.facecolor": "#0a0a15",
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.3,
})
```

## Figure Catalog

### 1. Hydrograph (Time Series)

The most common hydrology plot. Show streamflow over time with optional baseflow separation.

```python
fig, ax = plt.subplots(figsize=(14, 5))
ax.plot(dates, Q, color="#00A3FF", linewidth=0.8, label="Streamflow")
ax.fill_between(dates, 0, Q, alpha=0.15, color="#00A3FF")

# Optional baseflow
if baseflow is not None:
    ax.plot(dates, baseflow, color="#00DDFF", linewidth=0.6, 
            linestyle="--", label=f"Baseflow (BFI={bfi:.2f})")
    ax.fill_between(dates, 0, baseflow, alpha=0.1, color="#00DDFF")

ax.set_ylabel("Discharge (m³/s)")
ax.set_xlabel("")  # dates are self-explanatory
ax.set_title(f"USGS {gauge_id} — Daily Streamflow", pad=12)
ax.legend(loc="upper right", framealpha=0.8)
ax.set_xlim(dates[0], dates[-1])
ax.set_ylim(bottom=0)
plt.tight_layout()
```

### 2. Flow Duration Curve (FDC)

Log-scale probability plot — the hydrologist's fingerprint for a catchment.

```python
Q_sorted = np.sort(Q)[::-1]
exceedance = np.arange(1, len(Q_sorted) + 1) / (len(Q_sorted) + 1) * 100

fig, ax = plt.subplots(figsize=(10, 6))
ax.semilogy(exceedance, Q_sorted, color="#00A3FF", linewidth=1.5)
ax.fill_between(exceedance, Q_sorted, alpha=0.08, color="#00A3FF")

# Mark Q10 and Q90
for pct, label in [(10, "Q₁₀"), (90, "Q₉₀")]:
    idx = np.argmin(np.abs(exceedance - pct))
    ax.axhline(Q_sorted[idx], color="#fbbf24", linewidth=0.5, linestyle=":")
    ax.annotate(f"{label} = {Q_sorted[idx]:.1f}", xy=(pct, Q_sorted[idx]),
                color="#fbbf24", fontsize=9, ha="left", va="bottom")

ax.set_xlabel("Exceedance Probability (%)")
ax.set_ylabel("Discharge (m³/s)")
ax.set_title("Flow Duration Curve", pad=12)
ax.set_xlim(0, 100)
```

### 3. Budyko Plot

Catchment water balance relative to the Budyko curve.

```python
# Budyko curve (Fu equation)
AI = np.linspace(0.01, 5, 200)
w = 2.6  # calibrated shape
EI_budyko = 1 + AI - (1 + AI**w)**(1/w)

fig, ax = plt.subplots(figsize=(8, 7))
ax.plot(AI, EI_budyko, color="#94a3b8", linewidth=1.5, label="Budyko curve")
ax.fill_between(AI, 0, np.minimum(AI, 1), alpha=0.05, color="#94a3b8")

# Energy limit and water limit
ax.plot([0, 5], [0, 5], color="#ffffff20", linestyle=":", linewidth=0.5)
ax.axhline(1, color="#ffffff20", linestyle=":", linewidth=0.5)

# Plot the catchment point
ax.scatter(aridity_index, evap_index, s=120, c="#00A3FF", 
           edgecolors="#00DDFF", linewidth=2, zorder=5)
ax.annotate(f"USGS {gauge_id}", xy=(aridity_index, evap_index),
            xytext=(10, 10), textcoords="offset points",
            color="#00A3FF", fontsize=10, fontweight="bold")

ax.set_xlabel("Aridity Index (PET/P)")
ax.set_ylabel("Evaporative Index (ET/P)")
ax.set_title("Budyko Space", pad=12)
ax.set_xlim(0, 4)
ax.set_ylim(0, 1.2)
ax.legend()
```

### 4. Signature Spider / Radar Chart

Compare a catchment's signatures against CAMELS percentiles.

```python
categories = ["BFI", "Runoff\nRatio", "FDC\nSlope", "Seasonality", "Flashiness"]
values = [bfi, rr, fdc_slope_norm, si, flashiness_norm]

angles = np.linspace(0, 2 * np.pi, len(categories), endpoint=False).tolist()
values += values[:1]
angles += angles[:1]

fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
ax.set_facecolor("#1a1a2e")
ax.plot(angles, values, "o-", color="#00A3FF", linewidth=2)
ax.fill(angles, values, alpha=0.2, color="#00A3FF")

ax.set_xticks(angles[:-1])
ax.set_xticklabels(categories, color="#e2e8f0", fontsize=10)
ax.set_ylim(0, 1)
ax.set_title(f"Catchment Signatures — {gauge_id}", pad=20, color="#e2e8f0")
```

### 5. Multi-Panel Calibration Diagnostics

Model performance dashboard — 4 panels in one figure.

```python
fig, axes = plt.subplots(2, 2, figsize=(14, 10))

# Panel 1: Observed vs Simulated time series
ax = axes[0, 0]
ax.plot(dates, Q_obs, color="#94a3b8", linewidth=0.6, label="Observed", alpha=0.8)
ax.plot(dates, Q_sim, color="#00A3FF", linewidth=0.8, label="Simulated")
ax.set_title(f"NSE = {nse:.3f}  |  KGE = {kge:.3f}")
ax.legend()

# Panel 2: Scatter (1:1)
ax = axes[0, 1]
ax.scatter(Q_obs, Q_sim, s=3, alpha=0.3, color="#00A3FF")
ax.plot([0, Q_obs.max()], [0, Q_obs.max()], ":", color="#fbbf24", linewidth=1)
ax.set_xlabel("Observed")
ax.set_ylabel("Simulated")
ax.set_title("1:1 Comparison")

# Panel 3: FDC comparison
ax = axes[1, 0]
# ... plot both FDCs

# Panel 4: Residual distribution
ax = axes[1, 1]
residuals = Q_sim - Q_obs
ax.hist(residuals, bins=50, color="#00A3FF", alpha=0.7, edgecolor="#0a0a15")
ax.axvline(0, color="#fbbf24", linestyle="--", linewidth=1)
ax.set_title(f"Residuals (bias = {np.mean(residuals):.2f})")

fig.suptitle(f"Calibration Diagnostics — {gauge_id}", fontsize=16, 
             fontweight="bold", color="#e2e8f0", y=1.02)
plt.tight_layout()
```

### 6. Spatial Map (Watershed)

When watershed geometry is available, plot it with context.

```python
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

fig, ax = plt.subplots(figsize=(10, 10))
# Plot watershed polygon
watershed_geom.plot(ax=ax, facecolor="#00A3FF20", edgecolor="#00A3FF", linewidth=2)
# Plot pour point
ax.scatter(lon, lat, s=100, c="#fbbf24", marker="^", zorder=5, 
           edgecolors="#0a0a15", linewidth=1.5)
ax.set_title(f"Watershed — {gauge_id}\nArea: {area_km2:.0f} km²", pad=12)
```

## Design Principles

1. **Dark-first.** Every figure has `#0a0a15` background. Light themes available on request but not default.
2. **Cyan accent dominates.** Primary data always `#00A3FF`. Secondary `#00DDFF`. Annotations `#fbbf24` (amber).
3. **Minimal chrome.** No box borders. Subtle grid. Labels only where needed.
4. **High DPI.** Always 150+ DPI for screen, 300 for print.
5. **Informative titles.** Include gauge ID and key metrics in the title — the figure should be self-contained.
6. **Consistent sizing.** Single panel: 10×6. Multi-panel: 14×10. Radar: 7×7.

## When the User Asks for "Light Theme"

Override the palette:

```python
LIGHT_STYLE = {
    "bg_dark": "#ffffff",
    "bg_panel": "#f8fafc",
    "accent": "#0369a1",
    "text_primary": "#0f172a",
    "text_muted": "#64748b",
    "grid": "#00000010",
}
```

## Output Format

- Default: render inline via matplotlib in the AI-Hydro kernel
- If user asks for export: save as PNG (300 DPI) or SVG
- If user asks for publication: use SVG + LaTeX-compatible font sizes
