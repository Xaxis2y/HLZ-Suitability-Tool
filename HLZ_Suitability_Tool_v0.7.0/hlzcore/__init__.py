# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""HLZ Suitability Tool core engine.

The core depends only on NumPy and SciPy, both of which ship with the
default ArcGIS Pro Python environment (arcgispro-py3). GIS input/output is
delegated to thin adapters (``io_arcpy`` for ArcGIS Pro, ``io_rasterio`` for
the optional command-line interface).
"""

__version__ = "v0.7.0"
TOOL_NAME = "HLZ Suitability Tool"
LICENSE_ID = "GPL-2.0-or-later"
COPYRIGHT = "Copyright (c) 2026 Eui Soo SON"

__all__ = ["__version__", "TOOL_NAME", "LICENSE_ID", "COPYRIGHT"]
