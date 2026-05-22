---
name: interactive-module-builder
description: Create AI-Hydro interactive learning modules — self-contained HTML files with executable Python cells, sliders, quizzes, scrollytelling, and branded dark-theme styling. Use when the user asks to create a learning module, tutorial, interactive lesson, educational artifact, or "explain X with visuals" for any hydrology topic.
when_to_use: When the user asks to create a learning module, tutorial, interactive lesson, educational content, or wants to "explain X with visuals" for hydrology topics. Also when the user says "make a module about", "create a tutorial for", or "build an interactive lesson on"
domain: general
tools_used: []
tags:
  - learning-module
  - interactive
  - html-artifact
  - education
  - tutorial
---

# AI-Hydro Interactive Module Builder

Create self-contained HTML learning modules that render in the AI-Hydro HTML Preview panel. Modules combine executable Python cells, interactive JavaScript visualizations, quizzes, scrollytelling, and branded styling into a single `.html` file.

## When This Workflow Applies

- User asks to create a learning module or tutorial
- User wants to "explain X with visuals" for a hydrology concept
- User wants an interactive educational artifact
- User asks to build something for teaching hydrology

## Module Architecture

Every module is a single `.html` file containing:
1. **Module manifest** — metadata for the marketplace
2. **Branded header** — AI-Hydro logo + title + author + license
3. **Workflow strip** — horizontal step navigation
4. **Content sections** — mix of text, interactive JS, executable Python cells, quizzes
5. **Provenance footer** — methods, data sources, citations

## Step 1 — Choose the Topic and Structure

Break the topic into 5–7 sections, each building on the previous:

```
1. Introduction (scrollytelling or animated concept)
2. Core concept (interactive visualization with sliders)
3. Deep dive (executable Python cell with real data)
4. Exploration (parameter space explorer)
5. Checkpoint (quiz — 3 questions)
6. Real data application (Python cell with CAMELS/USGS data)
7. Summary + provenance
```

## Step 2 — Write the Module Manifest

Embed in the HTML `<head>`:

```html
<script type="application/vnd.aihydro.module+json">
{
  "id": "topic-name",
  "title": "Human Readable Title",
  "version": "0.1.0",
  "authors": [{"name": "AI-Hydro Agent", "affiliation": "AI-Hydro"}],
  "license": "CC-BY-4.0",
  "topic": "hydrology-subtopic",
  "level": "intro",
  "estimated_minutes": 15,
  "requires": {
    "executable": true,
    "python": ["numpy", "matplotlib", "pandas"]
  }
}
</script>
```

## Step 3 — Apply AI-Hydro Branding

Every module uses the AI-Hydro design system. These CSS variables are mandatory:

```css
:root {
  --bg-dark: #0a0a15;
  --bg-panel: #1a1a2e;
  --bg-card: #0f0f1e;
  --accent: #00A3FF;
  --accent-end: #00DDFF;
  --hero-gradient: linear-gradient(135deg, #00D4FF 0%, #00FFFF 100%);
  --text-primary: #e2e8f0;
  --text-muted: #94a3b8;
  --text-accent: #7dd3fc;
}

body {
  background: var(--bg-dark);
  color: var(--text-primary);
  font-family: 'Nunito', sans-serif;
  line-height: 1.7;
  max-width: 900px;
  margin: 0 auto;
  padding: 2rem;
}

h1, h2, h3 { font-family: 'Poppins', sans-serif; }
code, pre { font-family: 'JetBrains Mono', monospace; }
```

**Never use:** warm colors (red/orange), white backgrounds by default, generic fonts (Inter, Roboto, Arial).

## Step 4 — Build Interactive Blocks

### Executable Python Cell

```html
<div class="aihydro-cell" data-lang="python" data-autorun="false">
<button class="aihydro-run-cell">▶ Run</button>
<pre><code>
import numpy as np
import matplotlib.pyplot as plt

# Compute something
Q = np.random.lognormal(3, 1, 365)
plt.figure(figsize=(12, 4))
plt.plot(Q, color='#00A3FF', linewidth=0.8)
plt.fill_between(range(365), 0, Q, alpha=0.15, color='#00A3FF')
plt.title('Synthetic Daily Streamflow')
plt.ylabel('Q (m³/s)')
plt.show()
</code></pre>
<div class="aihydro-output"></div>
</div>
```

### Interactive Parameter Slider (JavaScript)

```html
<div class="param-block">
  <label>Filter coefficient (α): 
    <span class="param-value" id="alpha-val">0.925</span>
  </label>
  <input type="range" min="0.90" max="0.999" step="0.001" value="0.925"
         oninput="updateAlpha(this.value)">
</div>
<canvas id="bfi-canvas" width="800" height="300"></canvas>

<script>
function updateAlpha(alpha) {
  document.getElementById('alpha-val').textContent = parseFloat(alpha).toFixed(3);
  // Recompute baseflow with Lyne-Hollick filter
  const Q = SYNTH_DATA; // baked-in synthetic data
  const bf = lyneHollick(Q, parseFloat(alpha));
  drawHydrograph('bfi-canvas', Q, bf);
}
</script>
```

### Quiz Checkpoint

```html
<div class="aihydro-question" data-answer="2">
  <p><strong>Q: A BFI of 0.85 most likely indicates:</strong></p>
  <button class="quiz-btn" onclick="aihydro.quiz(this, false)">
    Clay-dominated, flashy catchment
  </button>
  <button class="quiz-btn" onclick="aihydro.quiz(this, true)">
    Permeable geology with strong groundwater contribution
  </button>
  <button class="quiz-btn" onclick="aihydro.quiz(this, false)">
    Highly urbanized watershed
  </button>
</div>
```

### Scrollytelling Section

```html
<div class="scrolly-container">
  <div class="scrolly-graphic">
    <svg id="hydrograph-viz" width="600" height="300"></svg>
  </div>
  <div class="scrolly-steps">
    <div class="scrolly-step" data-step="rising">
      <h3>Rising Limb</h3>
      <p>Rapid increase in discharge as rainfall generates runoff...</p>
    </div>
    <div class="scrolly-step" data-step="peak">
      <h3>Peak Flow</h3>
      <p>Maximum discharge — the concentration time has elapsed...</p>
    </div>
    <div class="scrolly-step" data-step="recession">
      <h3>Recession</h3>
      <p>Flow decreases as stored water drains from the catchment...</p>
    </div>
  </div>
</div>
```

### Callout Blocks

```html
<div class="callout tip">
  <strong>Tip:</strong> BFI values below 0.25 usually indicate impermeable
  clay or granite geology — expect flashy storm response.
</div>

<div class="callout agent">
  <strong>Try it:</strong> Ask the agent to run this analysis on your gauge.
  <button onclick="aihydro.askAgent('Run baseflow separation on USGS 01031500')">
    Ask Agent
  </button>
</div>
```

## Step 5 — Standalone Fallback

Modules must render statically in any browser. Executable cells degrade gracefully:

```html
<script>
// If not inside AI-Hydro preview, disable Run buttons
if (typeof window.vscode === 'undefined' && typeof window.aihydro === 'undefined') {
  document.querySelectorAll('.aihydro-run-cell').forEach(btn => {
    btn.textContent = '🔒 Open in AI-Hydro to run';
    btn.disabled = true;
    btn.style.opacity = '0.5';
  });
}
</script>
```

## Step 6 — Provenance Footer

Every module ends with provenance:

```html
<footer class="provenance">
  <h2>Provenance</h2>
  <dl>
    <dt>Methods</dt>
    <dd>Lyne & Hollick (1979) recursive digital filter for baseflow separation</dd>
    <dt>Data</dt>
    <dd>USGS NWIS via dataretrieval Python package</dd>
    <dt>Software</dt>
    <dd>AI-Hydro v0.1.24, Python 3.11, NumPy, Matplotlib</dd>
    <dt>Generated</dt>
    <dd id="gen-date"><script>document.getElementById('gen-date').textContent = 
        new Date().toISOString().split('T')[0]</script></dd>
  </dl>
  <p class="refs">
    <strong>References:</strong><br>
    Lyne, V. & Hollick, M. (1979). Stochastic time-variable rainfall-runoff modelling...
  </p>
</footer>
```

## Design Principles

1. **Dark-first, always.** Match AI-Hydro's `#0a0a15` background.
2. **Bake synthetic data in.** First load should work without network. Real data only in the final Python cell.
3. **One interactive per section.** Don't overwhelm. Each section: explain → interact → reflect.
4. **Progressive complexity.** Start with visual intuition, end with real computation.
5. **Every quiz question teaches.** Wrong answers should explain why they're wrong.
6. **Self-contained single file.** Inline all CSS and JS. Use CDN fonts only (Google Fonts).

## Checklist Before Saving

- [ ] Module manifest is valid JSON in the `<head>`
- [ ] All sections have unique IDs for workflow-strip navigation
- [ ] At least one executable Python cell
- [ ] At least one interactive JavaScript visualization
- [ ] At least one quiz checkpoint
- [ ] Standalone fallback: Run buttons disabled outside AI-Hydro
- [ ] Provenance footer with methods, data sources, citations
- [ ] Tested: opens in plain browser without errors (minus Python execution)
- [ ] All colors from the AI-Hydro palette — no warm tones
