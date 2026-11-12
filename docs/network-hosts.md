# Hosts the tools reach for soil tables (USDA Soil Data Access)

For operators running with a network allow-list (a proxy or an egress firewall). Source of each statement is named; nothing here is measured.

## USDA Soil Data Access (SDA)

- **Host tried first, and only: `sdmdataaccess.nrcs.usda.gov`.** It is the single constant `SDA_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"` in `aihydro-watershed` (`aihydro_watershed/terrain/ssurgo.py`). No second hostname is tried. The older `sdmdataaccess.sc.egov.usda.gov` name is not used, so an allow-list that has only that name will refuse every SDA request.
- **Retries and the curl fallback.** `sda_query` posts with `requests` up to 4 times (sleeping 2, 4, 6, 8 s), then, if `curl` is on `PATH`, once more through a `curl` subprocess to the same URL. A restricted-network run must route `curl` through its proxy too (for example an `HOME/.curlrc`), or the fallback goes around the allow-list.
- **Who calls it.** `create_cn_grid` and `fetch_soil_attributes_ssurgo` (hydrologic soil group from SSURGO component and horizon tables). When SDA is unreachable they fall back to POLARIS texture thresholds and report the method (`hsg_method`, `soil_fallback_reason`); the fallback is not silent.
- **Other hosts in the same step** (not SDA): the gNATSGO raster is read through the Planetary Computer STAC API (`planetarycomputer.microsoft.com`) from `soils.blob.core.windows.net`.

Not verified here: whether NRCS documents `sdmdataaccess.sc.egov.usda.gov` and `sdmdataaccess.nrcs.usda.gov` as aliases. If the host in `SDA_URL` should change, that is an `aihydro-watershed` change, not a tools one.
