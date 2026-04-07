"""
Inlined wood properties table for STRATHMARK handicap predictions.

The STRATHMARK package's HandicapCalculator needs a `wood_df` DataFrame
of species properties (Janka hardness, specific gravity, MOR, MOE, etc.)
to drive the diameter scaling and species hardness lookups in the
prediction cascade.

Upstream STRATHMARK loads this from `woodchopping_clean.xlsx` via
`strathmark.loader.load_woodchopping_xlsx()`, but that xlsx is at the
root of the STRATHMARK repo and is NOT bundled inside the pip-installable
`strathmark/` Python package (per its `pyproject.toml` — only `packages =
["strathmark"]` ships in the wheel).

Rather than ship a copy of the xlsx in this repo's `instance/` or `data/`
directory and add a file-path env var, we inline the 13-row wood table
here as a Python constant.  Tradeoffs:

  + Zero file dependency, zero env var, zero deploy data plumbing.
  + Version-controlled with the rest of the code.
  + One source of truth — if STRATHMARK ships an updated wood table,
    update this file in the same PR that bumps the strathmark pin.
  - When STRATHMARK ships an updated wood table, this file must be
    re-synced manually.  See REGENERATE_FROM section below.

REGENERATE_FROM
---------------
This data was extracted from
    STRATHMARK/woodchopping_clean.xlsx, sheet "Wood"
on 2026-04-06 against strathmark commit a101c8e4 (the version pinned in
this repo's requirements.txt).  To re-extract::

    python -c "
    import pandas as pd
    wood = pd.read_excel('woodchopping_clean.xlsx', sheet_name='Wood')
    print(wood.to_dict(orient='records'))
    "

Schema (matches `strathmark/loader.py::_WOOD_REQUIRED` plus the optional
columns the predictor reads):

    Scientific Name : str  — Linnaean name (informational only)
    species         : str  — display name used by predictor matching
    speciesID       : str  — short code (S01..S13)
    country         : str  — origin (informational)
    region          : str  — origin (informational)
    janka_hard      : int  — Janka hardness (lbf)
    spec_gravity    : float — specific gravity
    crush_strength  : int  — crushing strength (psi)
    shear           : int  — shear strength (psi)
    MOR             : int  — modulus of rupture (psi)
    MOE             : int  — modulus of elasticity (psi)
"""
from __future__ import annotations

import pandas as pd

# ---------------------------------------------------------------------------
# Wood properties table — DO NOT EDIT WITHOUT RE-SYNCING FROM STRATHMARK
# ---------------------------------------------------------------------------
#
# Each dict is one row of the upstream "Wood" sheet, exactly as it appears
# in woodchopping_clean.xlsx.  Order is preserved for stability.

WOOD_TABLE_ROWS: list[dict] = [
    {"Scientific Name": "Pinus strobus",          "species": "eastern white pine", "speciesID": "S01", "country": "USA", "region": "EAST", "janka_hard": 1690, "spec_gravity": 0.34, "crush_strength": 4800, "shear":  900, "MOR":  8600, "MOE": 1240000},
    {"Scientific Name": "Liriodendron tulipifera","species": "yellow-poplar",      "speciesID": "S02", "country": "USA", "region": "EAST", "janka_hard": 2400, "spec_gravity": 0.40, "crush_strength": 5540, "shear": 1190, "MOR": 10100, "MOE": 1580000},
    {"Scientific Name": "Populus termuloides",    "species": "quaking aspen",      "speciesID": "S03", "country": "USA", "region": "EAST", "janka_hard": 1560, "spec_gravity": 0.35, "crush_strength": 4250, "shear":  850, "MOR":  8400, "MOE": 1180000},
    {"Scientific Name": "Alnus spp",              "species": "alder",              "speciesID": "S04", "country": "USA", "region": "WEST", "janka_hard": 2620, "spec_gravity": 0.37, "crush_strength": 5820, "shear": 1080, "MOR":  9800, "MOE": 1380000},
    {"Scientific Name": "Pinus ponderosa",        "species": "ponderosa pine",     "speciesID": "S05", "country": "USA", "region": "WEST", "janka_hard": 2050, "spec_gravity": 0.38, "crush_strength": 5320, "shear": 1130, "MOR":  9400, "MOE": 1290000},
    {"Scientific Name": "Pinus monticola",        "species": "western white pine", "speciesID": "S06", "country": "USA", "region": "WEST", "janka_hard": 1870, "spec_gravity": 0.35, "crush_strength": 5040, "shear": 1040, "MOR":  9700, "MOE": 1460000},
    {"Scientific Name": "Pinus lambertiana",      "species": "sugar pine",         "speciesID": "S07", "country": "USA", "region": "WEST", "janka_hard": 1690, "spec_gravity": 0.34, "crush_strength": 4460, "shear": 1130, "MOR":  8200, "MOE": 1190000},
    {"Scientific Name": "Populus spp",            "species": "cottonwood",         "speciesID": "S08", "country": "USA", "region": "WEST", "janka_hard": 1560, "spec_gravity": 0.38, "crush_strength": 4500, "shear": 1040, "MOR":  8500, "MOE": 1270000},
    {"Scientific Name": "Populus plantationus",   "species": "poplar (Hybrid)",    "speciesID": "S09", "country": "USA", "region": "WEST", "janka_hard": 1820, "spec_gravity": 0.35, "crush_strength": 5540, "shear": 1040, "MOR":  6800, "MOE": 1100000},
    {"Scientific Name": "Populus spp",            "species": "poplar (European)",  "speciesID": "S10", "country": "EUR", "region": "CENT", "janka_hard": 2400, "spec_gravity": 0.36, "crush_strength": 5080, "shear":  950, "MOR":  9430, "MOE": 1290000},
    {"Scientific Name": "Populus lombardi",       "species": "poplar (Lombardi)",  "speciesID": "S11", "country": "AUS", "region": "TAS",  "janka_hard": 2020, "spec_gravity": 0.31, "crush_strength": 5220, "shear": 1040, "MOR":  9230, "MOE": 1045000},
    {"Scientific Name": "Pinus radiata",          "species": "Monterey pine",      "speciesID": "S12", "country": "AUS", "region": "VIC",  "janka_hard": 3150, "spec_gravity": 0.41, "crush_strength": 6030, "shear":  754, "MOR": 11480, "MOE": 1458000},
    {"Scientific Name": "Tilia americana",        "species": "basswood",           "speciesID": "S13", "country": "USA", "region": "EAST", "janka_hard": 1820, "spec_gravity": 0.32, "crush_strength": 4730, "shear":  990, "MOR":  8700, "MOE": 1460000},
]


# Module-level cache so we only build the DataFrame once per process.
_WOOD_DF_CACHE: pd.DataFrame | None = None


def get_wood_dataframe() -> pd.DataFrame:
    """Return the wood properties DataFrame STRATHMARK expects.

    Schema matches the "Wood" sheet of `woodchopping_clean.xlsx`.  The
    DataFrame is built once per process and cached.

    Returns:
        pd.DataFrame with columns matching strathmark/loader.py::_WOOD_REQUIRED
        (species, speciesID, janka_hard, spec_gravity) plus the optional
        columns the predictor reads.
    """
    global _WOOD_DF_CACHE
    if _WOOD_DF_CACHE is None:
        _WOOD_DF_CACHE = pd.DataFrame(WOOD_TABLE_ROWS)
    return _WOOD_DF_CACHE
