# CAMELS-US v1.2 reference tables (packaged)

Verbatim copies of three attribute files of the CAMELS-US v1.2 data set, shipped so the
`camels_reference` block of `extract_hydrological_signatures` works offline, hermetically and pinned to one
version of the reference.

| file | used for |
|---|---|
| `camels_hydro.txt` | the hydrologic signatures compared (q_mean, runoff_ratio, slope_fdc, baseflow_index, stream_elas, q5, q95, high/low/zero flow statistics, hfd_mean) |
| `camels_topo.txt`  | `area_gages2`, the drainage area CAMELS depth signatures use |
| `camels_clim.txt`  | climate context only: `frac_snow`, `aridity`, `p_seasonality` |

Not shipped (not available locally when this was packaged, and not fetched): the soil, vegetation and geology
attribute files. They are not used by the comparison. For attributes beyond these, call `fetch_camels_us`.

## Source and integrity

Zenodo record 15529996, "CAMELS: Catchment Attributes and MEteorology for Large-sample Studies", version 1.2
(DOI 10.5065/D6MW2F4D). Each file is byte-identical to the published file: `<file>.provenance.json` records the
source URL, download time, the md5 the record publishes (`md5_published`), the md5 observed on download, the sha256
and the size. `ai_hydro.science.camels_reference` re-checks the md5 of each file against its provenance JSON before
using it; a mismatch makes the packaged reference unavailable (it is never used silently).

## License and attribution

The Zenodo record states **CC-BY 4.0** (https://creativecommons.org/licenses/by/4.0/). Redistribution is allowed with
attribution. Please cite:

- Addor, N., Newman, A. J., Mizukami, N., & Clark, M. P. (2017). The CAMELS data set: catchment attributes and
  meteorology for large-sample studies. Hydrology and Earth System Sciences, 21, 5293-5313.
  https://doi.org/10.5194/hess-21-5293-2017
- Newman, A. J., et al. (2015). Hydrology and Earth System Sciences (HESS), 19, 209. Companion paper to the CAMELS
  data set. Title and DOI to be completed from the reviewer's citation: they are not in any local file and were not
  invented here.
- Newman, A. et al. (2014). The NCAR CAMELS data set (title not recorded locally); the DOI the provenance JSON of
  these files gives for the record is 10.5065/D6MW2F4D.
- CAMELS data set v1.2, Zenodo record 15529996 (DOI 10.5065/D6MW2F4D), NCAR/UCAR.

## What the values are (read before comparing)

The CAMELS-US v1.2 signatures are for water years 1990-2009 (1989-10-01 to 2009-09-30) from Daymet forcing and
`area_gages2`. `slope_fdc` is the file's distributed definition `(Q66 - Q33) / (mean(Q) * 0.33)`, not the log-space
Sawicz et al. (2011) slope. See `docs/session-integrity.md` and the watershed 0.2.0 changelog.
