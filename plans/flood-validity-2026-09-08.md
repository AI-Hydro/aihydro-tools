# Joint flood validity — 2026-09-08

Continue support-aware scoring before the major Map workstream. Replace implicit bool coercion of nodata with explicit binary/finite validity intersection. Accept caller-aligned model/reference validity masks, report included/excluded counts, and return null with reasons for undefined ratios. Remove universal CSI skill thresholds. Extent-only GeoJSON is insufficient to establish observed dry cells: require explicit observation support and declared compatible grid CRS before scoring; otherwise return not_assessed.

Acceptance: nodata and masked cells never count as flooded/dry evidence; invalid codes fail; no valid cells and no-event denominators cannot yield perfect skill; real binary overlap retains known ratios. Tests cover joint masks, empty domains, malformed masks and missing GeoJSON support. Existing GFM adapter remains gated until acquisition/quality/grid support is available.

Then begin the Map workstream by inspecting actual runtime/data contracts and choosing an executable research workflow; user explicitly prioritizes substantial Map improvements after scientific prerequisites. No alternate runtime/store, invented scientific layers or mock success states.

## Outcome
Implemented locally; 43 selected tests passed. First Map inspection slice implemented with 6 UI tests and webview TypeScript passing. No live validation. See extension plans/map-research-workspace-2026-09-08.md for the major next workstream.
