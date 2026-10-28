# CAMELS attributes v2.0 reference tables (packaged)

Verbatim copies of three attribute files of the CAMELS attributes v2.0 data set, shipped so the
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
- Data citation: "A. Newman; K. Sampson; M. P. Clark; A. Bock; R. J. Viger; D. Blodgett, 2014. A large-sample
  watershed-scale hydrometeorological dataset for the contiguous USA. Boulder, CO: UCAR/NCAR.
  https://dx.doi.org/10.5065/D6MW2F4D"
- Associated paper: "A. J. Newman, M. P. Clark, K. Sampson, A. Wood, L. E. Hay, A. Bock, R. J. Viger, D. Blodgett,
  L. Brekke, J. R. Arnold, T. Hopson, and Q. Duan: Development of a large-sample watershed-scale hydrometeorological
  dataset for the contiguous USA: dataset characteristics and assessment of regional variability in hydrologic model
  performance. Hydrol. Earth Syst. Sci., 19, 209-223, doi:10.5194/hess-19-209-2015, 2015."

  (The two Newman texts above are copied verbatim from the `metadata.description` ("TIME SERIES" section) of the
  Zenodo record 15529996.)
- CAMELS data set v1.2, Zenodo record 15529996 (DOI 10.5065/D6MW2F4D), NCAR/UCAR.

## What the values are (read before comparing)

The CAMELS attributes v2.0 signatures are for water years 1990-2009 (1989-10-01 to 2009-09-30) from Daymet forcing and
`area_gages2`. `slope_fdc` is the file's distributed definition `(Q66 - Q33) / (mean(Q) * 0.33)`; the log-space
Sawicz et al. (2011) slope is `slope_fdc_sawicz2011`. See `docs/session-integrity.md` and the watershed 0.2.0 changelog.
