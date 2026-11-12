# Hosts the tools reach for soil tables (USDA Soil Data Access)

For operators running with a network allow-list (a proxy or an egress firewall). Each statement names its source and the aihydro-watershed version it was read in. `aihydro-tools` accepts `aihydro-watershed>=0.4.0,<0.5`, so both behaviours below can occur.

## USDA Soil Data Access (SDA)

- **Host, all versions read: `sdmdataaccess.nrcs.usda.gov`, first and only.** `SDA_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"` in `aihydro_watershed/terrain/ssurgo.py` (read in 0.4.4 and 0.4.5; the DEMO-1 rehearsal ran 0.4.3). No second hostname is tried. The older `sdmdataaccess.sc.egov.usda.gov` name is not used, so an allow-list that has only that name refuses every SDA request.
- **Transport and retries differ by version.**
  - **0.4.0 to 0.4.4: `requests`, then a `curl` subprocess.** `sda_query` posts with `requests` up to 4 times (sleeping 2, 4, 6, 8 s after each failure), then, if `curl` is on `PATH`, once more through a `curl` subprocess to the same URL. That subprocess bypasses Python-level proxy and policy hooks; a restricted run had to route `curl` itself through its proxy (for example `HOME/.curlrc`). Read in 0.4.4 and observed in 0.4.3 (DEMO-1 rehearsal); 0.4.0 to 0.4.2 were not read.
  - **0.4.5 and later: `requests` only.** Up to 4 attempts, sleeping 2, 4, 6 s between attempts only (none after the last). There is no `curl` fallback, so nothing needs to be done for `curl`. On final failure `sda_query` raises `RuntimeError` and `fetch_soil_data` falls back to POLARIS texture thresholds, recording the reason in `attrs['_soil_fallback_reason']`.
- **Who calls it.** `create_cn_grid` and `fetch_soil_attributes_ssurgo` (hydrologic soil group from SSURGO component and horizon tables). When SDA is unreachable they fall back to POLARIS and report the method (`hsg_method`, `soil_fallback_reason`); the fallback is not silent.
- **Other hosts in the same step (not SDA).** The gNATSGO raster is searched through the Planetary Computer STAC API (`planetarycomputer.microsoft.com`). The raster's storage host is taken from the STAC asset hrefs; `soils.blob.core.windows.net` was seen in the DEMO-1 rehearsal's blocked-request log and is **unverified** as a fixed host (it was not read from code).

Not verified here: whether NRCS documents `sdmdataaccess.sc.egov.usda.gov` and `sdmdataaccess.nrcs.usda.gov` as aliases. If the host in `SDA_URL` should change, that is an `aihydro-watershed` change, not a tools one.
