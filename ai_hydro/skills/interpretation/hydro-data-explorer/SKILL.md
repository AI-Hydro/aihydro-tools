---
name: hydro-data-explorer
description: Create interactive HTML playgrounds for exploring hydrological data — parameter sensitivity explorers, gauge comparison dashboards, return-period calculators, and catchment signature browsers. Use when the user asks for an interactive explorer, dashboard, playground, or visual tool to explore hydrological data or parameters.
when_to_use: When the user asks for an interactive explorer, playground, dashboard, or visual tool for hydrological data. Also when they say "let me explore", "build a tool to compare", "I want to play with parameters", or "make an interactive calculator"
domain: interpretation
tools_used:
  - fetch_streamflow_data
  - extract_hydrological_signatures
  - extract_camels_attributes
tags:
  - playground
  - interactive
  - explorer
  - dashboard
  - data-visualization
---

# Hydrological Data Explorer Builder

Build self-contained HTML playgrounds that let users explore hydrological data interactively — adjust parameters, see results immediately, and copy findings back into the conversation.

## When This Workflow Applies

- User asks for an interactive explorer or dashboard
- User wants to explore parameter sensitivity (e.g., "what happens to BFI when I change α?")
- User wants a comparison tool (e.g., "compare these 5 gauges side by side")
- User wants an interactive calculator (e.g., "return period calculator")
- User wants to visually explore a dataset

## Core Requirements

Every explorer must have:

1. **Single HTML file.** Inline all CSS and JS. No external dependencies except CDN fonts.
2. **Live preview.** Updates instantly on every control change. No "Apply" button.
3. **Copy button.** Copies a natural-language summary of the current state to clipboard.
4. **Sensible defaults.** Looks useful on first load with realistic synthetic data.
5. **AI-Hydro dark theme.** `#0a0a15` background, `#00A3FF` accent, Poppins/Nunito fonts.

## Explorer Types

### 1. Parameter Sensitivity Explorer

For questions like "how does α affect baseflow separation?"

**Layout:**
```
+-------------------+----------------------------+
| Controls          | Live visualization         |
| • Parameter       | (chart updates instantly)  |
| sliders           |                            |
| • Method toggle   +----------------------------+
| • Presets (3-5)   | Summary metrics            |
|                   | BFI: 0.65 | Q_bf: 2.3     |
+-------------------+----------------------------+
| Findings output                  [ Copy ]      |
+------------------------------------------------+
```

**Example — Baseflow Filter Explorer:**

```javascript
const state = {
  alpha: 0.925,
  passes: 3,
  method: 'lyne-hollick',
};

function updateAll() {
  const bf = computeBaseflow(SYNTH_Q, state.alpha, state.passes, state.method);
  const bfi = bf.reduce((s, v) => s + v, 0) / SYNTH_Q.reduce((s, v) => s + v, 0);
  drawChart(SYNTH_Q, bf);
  updateMetrics({ bfi, mean_bf: mean(bf), mean_q: mean(SYNTH_Q) });
  updateFindings();
}
```

### 2. Gauge Comparison Dashboard

For questions like "compare these catchments."

**Layout:**
```
+------------------------------------------------+
| Gauge selector (chips: click to add/remove)    |
+-------------------+----------------------------+
| Metric table      | Overlay chart              |
| gauge | BFI | RR  | (FDCs, hydrographs, or    |
| 01031 | 0.6 | 0.4 |  signatures overlaid)      |
| 01047 | 0.3 | 0.7 |                            |
+-------------------+----------------------------+
| Comparison summary                  [ Copy ]   |
+------------------------------------------------+
```

### 3. Return Period Calculator

For questions like "what's the 100-year flood?"

**Layout:**
```
+-------------------+----------------------------+
| Distribution      | Probability plot           |
| ○ GEV  ○ LP3     | (data points + fitted      |
| ○ Gumbel          |  curve + CI bands)         |
|                   |                            |
| Return period     +----------------------------+
| slider: [T years] | Quantile table             |
| 2 ——————— 500     | T=10: 245 m³/s             |
|                   | T=50: 389 m³/s             |
| Confidence level  | T=100: 456 m³/s            |
| ○ 90%  ○ 95%     |                            |
+-------------------+----------------------------+
| Summary                             [ Copy ]   |
+------------------------------------------------+
```

### 4. Catchment Signature Browser

For questions like "show me a signature profile for this catchment."

**Layout:**
```
+------------------------------------------------+
| Gauge ID input    [ Load ]   or  [ Use Session ]|
+-------------------+----------------------------+
| Signature cards   | Radar/spider chart         |
| ┌─────┐ ┌─────┐  |                            |
| │BFI  │ │ RR  │  | Overlaid with CAMELS       |
| │0.65 │ │0.42 │  | percentile band            |
| │████ │ │███  │  |                            |
| └─────┘ └─────┘  +----------------------------+
| ┌─────┐ ┌─────┐  | Interpretation panel       |
| │FDC  │ │ SI  │  | "This catchment has..."    |
| │12.3 │ │0.15 │  |                            |
| └─────┘ └─────┘  |                            |
+-------------------+----------------------------+
```

## State Management Pattern

Every explorer follows this pattern:

```javascript
const DEFAULTS = {
  alpha: 0.925,
  passes: 3,
  method: 'lyne-hollick',
};

const state = { ...DEFAULTS };

function updateAll() {
  renderVisualization();
  updateMetrics();
  updateFindings();
}

// Every control calls updateAll()
document.getElementById('alpha-slider').addEventListener('input', e => {
  state.alpha = parseFloat(e.target.value);
  updateAll();
});
```

## Findings Output Pattern

The copy-able output should be natural language, not a value dump:

```javascript
function updateFindings() {
  const parts = [];
  
  if (state.alpha !== DEFAULTS.alpha) {
    parts.push(`filter coefficient α=${state.alpha.toFixed(3)}`);
  }
  
  parts.push(`BFI = ${metrics.bfi.toFixed(2)}`);
  
  if (metrics.bfi > 0.8) {
    parts.push('indicating permeable geology with strong groundwater contribution');
  } else if (metrics.bfi < 0.25) {
    parts.push('indicating impermeable catchment with flashy storm response');
  }

  findingsEl.textContent = `Using the ${state.method} method with ${parts.join(', ')}.`;
}
```

## Presets

Include 3–5 named presets that snap all controls to meaningful combinations:

```javascript
const PRESETS = {
  'Chalk stream': { alpha: 0.98, passes: 3, method: 'eckhardt' },
  'Urban flashy': { alpha: 0.90, passes: 1, method: 'lyne-hollick' },
  'Mixed geology': { alpha: 0.925, passes: 3, method: 'lyne-hollick' },
  'Karst spring': { alpha: 0.995, passes: 5, method: 'eckhardt' },
};
```

## Baking in Data

Explorers should work without network on first load. Embed synthetic or sample data:

```javascript
// Synthetic storm hydrograph (Gamma distribution)
const SYNTH_Q = Array.from({length: 365}, (_, i) => {
  const base = 5 + 3 * Math.sin(2 * Math.PI * i / 365);
  const storms = [60, 120, 200, 280].map(d => 
    Math.max(0, 30 * Math.exp(-0.1 * Math.abs(i - d)))
  );
  return base + storms.reduce((s, v) => s + v, 0) + Math.random() * 2;
});
```

For real data, offer a "Load from USGS" button that calls the AI-Hydro session if available.

## AI-Hydro Theme (CSS)

```css
:root {
  --bg: #0a0a15;
  --bg-panel: #1a1a2e;
  --bg-card: #0f0f1e;
  --accent: #00A3FF;
  --accent-dim: #00A3FF40;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --border: #ffffff15;
  --success: #34d399;
  --warn: #fbbf24;
}

body {
  margin: 0; padding: 1.5rem;
  background: var(--bg);
  color: var(--text);
  font-family: 'Nunito', sans-serif;
}

.explorer-grid {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 1.5rem;
  max-width: 1200px;
  margin: 0 auto;
}

.control-panel {
  background: var(--bg-panel);
  border-radius: 12px;
  padding: 1.5rem;
  border: 1px solid var(--border);
}

input[type="range"] {
  accent-color: var(--accent);
  width: 100%;
}

.copy-btn {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 8px;
  padding: 0.5rem 1.5rem;
  cursor: pointer;
  font-family: inherit;
}
.copy-btn:hover { filter: brightness(1.1); }
```

## Common Mistakes to Avoid

- **Value dump instead of narrative** — findings output should read like a sentence, not `{bfi: 0.65, alpha: 0.925}`
- **No defaults** — the explorer should show useful content immediately, not a blank canvas
- **External dependencies** — everything inline; CDN fonts are the only exception
- **No presets** — users don't know what "good" parameter combinations look like
- **Missing interpretation** — metrics without context are useless; always include qualitative labels
- **Click-to-apply pattern** — every change should update the visualization instantly

## Delivery

After building the explorer:

1. Save as `<topic>_explorer.html`
2. Open in browser: `open <filename>.html`
3. If inside AI-Hydro, it renders in the HTML Preview panel with full Python cell support
