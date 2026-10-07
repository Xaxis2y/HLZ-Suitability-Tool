# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Interactive map for HLZ_Report.html (v0.6.7).

The report stays ONE self-contained file: the Leaflet library (BSD-2-Clause, see
``assets/leaflet/LICENSE-Leaflet-BSD-2-Clause.txt``) and the candidate data are
embedded. The map offers:

* zoom in/out, pan and a "zoom to all" button;
* a basemap choice (Esri imagery, Esri topographic, OpenStreetMap) - the tiles are
  fetched from the internet when the report is opened; the report can be built
  with NO basemap, in which case it makes no network request at all;
* markers coloured by rating; hovering shows a summary;
* clicking an ID in the table (or a "Show on map" button) zooms to that site and
  draws its touchdown point, cleared ring, 36 approach sectors and the best
  approach; clicking a marker highlights its row in the table;
* rating filters that hide markers and table rows together.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .models import SECTOR_BLOCKED, SECTOR_CLEAR, SECTOR_INCOMPLETE, SECTOR_MARGINAL, Candidate

ASSETS = Path(__file__).resolve().parent / "assets" / "leaflet"
MAP_CANDIDATE_LIMIT = 2000
SECTOR_LETTER = {SECTOR_CLEAR: "C", SECTOR_MARGINAL: "M", SECTOR_BLOCKED: "B", SECTOR_INCOMPLETE: "I"}
_TRANSPARENT_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def leaflet_css() -> str:
    """Leaflet's stylesheet with its images embedded (the report has no other files)."""
    css = (ASSETS / "leaflet.css").read_text(encoding="utf-8")
    css = css.replace("url(images/layers-2x.png)", f"url({_data_uri(ASSETS / 'layers-2x.png')})")
    css = css.replace("url(images/layers.png)", f"url({_data_uri(ASSETS / 'layers.png')})")
    css = css.replace("url(images/marker-icon.png)", f"url({_TRANSPARENT_GIF})")
    return css


def leaflet_js() -> str:
    return (ASSETS / "leaflet.js").read_text(encoding="utf-8")


def _json_for_html(value: Any) -> str:
    """JSON that is safe inside a <script> element."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def map_payload(
    candidates: list[Candidate],
    aircraft: dict[str, Any],
    azimuth_step_deg: float,
    rating_names: tuple[str, ...],
    rating_colours: dict[str, str],
    basemap: str,
    search: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Data for the map, or None when no candidate has coordinates."""
    shown = [c for c in candidates if c.latitude is not None and c.longitude is not None][:MAP_CANDIDATE_LIMIT]
    if not shown:
        return None
    points = []
    for candidate in shown:
        sectors = sorted(candidate.sectors, key=lambda sector: sector.azimuth_deg)
        points.append(
            {
                "id": candidate.candidate_id,
                "r": candidate.rank,
                "rt": rating_names.index(candidate.rating) if candidate.rating in rating_names else 0,
                "s": _round(candidate.score, 1),
                "la": _round(candidate.latitude, 7),
                "lo": _round(candidate.longitude, 7),
                "m": candidate.mgrs,
                "el": _round(candidate.elevation_m, 1),
                "sl": _round(candidate.slope_deg, 1),
                "up": 1 if candidate.upslope_only else 0,
                "af": _round(candidate.best_approach_azimuth_deg, 0),
                "lh": _round(candidate.landing_heading_deg, 0),
                "aa": _round(candidate.best_approach_angle_deg, 1),
                "cl": candidate.clear_sector_count,
                "as": candidate.assessed_sector_count,
                "lim": candidate.limiting_factor,
                "ex": _round(candidate.excluded_distance_m, 0),
                "ob": _round(candidate.nearest_obstacle_m, 0),
                "obt": candidate.nearest_obstacle_text,
                "sd": _round(candidate.search_distance_m, 0),
                "sb": _round(candidate.search_bearing_deg, 0),
                "sec": "".join(SECTOR_LETTER.get(sector.classification, "I") for sector in sectors),
            }
        )
    return {
        "cfg": {
            "basemap": "OFFLINE" if str(basemap).upper() == "OFFLINE" else "ONLINE",
            "names": list(rating_names),
            "colors": [rating_colours.get(name, "#9e9e9e") for name in rating_names],
            "tdp_r": float(aircraft.get("tdp_diameter_m", 50.0)) / 2.0,
            "ring_m": float(aircraft.get("cleared_ring_m", 20.0)),
            "range_m": float(aircraft.get("approach_range_m", 1000.0)),
            "half_width": float(aircraft.get("corridor_half_width_deg", 15.0)),
            "az_step": float(azimuth_step_deg),
            "total": len(candidates),
            "search": (
                {"centres": search.get("centres_latlon") or [], "radius_m": float(search.get("radius_m", 0.0))}
                if search and search.get("centres_latlon")
                else None
            ),
        },
        "pts": points,
    }


MAP_CSS = r"""
#explore { position: relative; }
#mapbox { position: sticky; top: 0; z-index: 1000; background: #fff; padding: 0 0 6px; border-bottom: 2px solid var(--line); }
#map { height: 46vh; min-height: 300px; border: 1px solid var(--line); border-radius: 4px; background: #eef1f2; }
#map.offline { background: repeating-linear-gradient(0deg, #f3f5f6, #f3f5f6 39px, #e1e6e8 40px); }
#mapbar { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; font-size: 13px; padding: 5px 2px 0; min-height: 22px; }
#mapinfo { flex: 1 1 280px; color: var(--muted); min-height: 18px; }
#mapfilters label { margin-right: 12px; white-space: nowrap; cursor: pointer; }
#mapfilters i { display: inline-block; width: 11px; height: 11px; border-radius: 50%; margin-right: 4px; vertical-align: -1px; border: 1px solid #212121; }
.mapnote { font-size: 12px; color: var(--muted); margin: 4px 0 8px; }
a.goto { color: #0d47a1; text-decoration: none; font-weight: 700; }
a.goto:hover, a.goto:focus { text-decoration: underline; }
button.goto { font: inherit; font-size: 12px; margin-left: 10px; padding: 2px 9px; border: 1px solid #0d47a1; color: #0d47a1; background: #fff; border-radius: 12px; cursor: pointer; }
button.goto:hover, button.goto:focus { background: #0d47a1; color: #fff; }
tr.sel td { background: #dfeafd !important; box-shadow: inset 0 1px 0 #0d47a1, inset 0 -1px 0 #0d47a1; }
th { top: var(--mapbox-h, 0px) !important; }
.rk { background: #fff; border: 1px solid #212121; border-radius: 9px; padding: 0 5px; font-weight: 700; font-size: 11px; box-shadow: none; }
.rk::before { display: none; }
.leaflet-container { font: 12px/1.4 "Segoe UI", Arial, sans-serif; }
.hlz-pop b { font-size: 14px; }
.hlz-pop .chip { display: inline-block; color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
.hlz-pop table { border: 0; font-size: 12px; margin-top: 4px; }
.hlz-pop td { border: 0; padding: 1px 6px 1px 0; }
.hlz-home a { font-size: 17px; line-height: 26px; text-align: center; text-decoration: none; color: #212121; }
@media (max-width: 800px) { #map { height: 38vh; } }
@media print { #mapbox { position: static; } #map { height: 340px; } #mapfilters, button.goto, .leaflet-control-container { display: none; } }
"""

MAP_JS = r"""
(function () {
  var D = JSON.parse(document.getElementById('hlzdata').textContent);
  var C = D.cfg, P = D.pts, byId = {}, markers = {}, offline = C.basemap === 'OFFLINE';
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function pad3(v) { return ('000' + Math.round(v % 360)).slice(-3); }
  P.forEach(function (p) { byId[p.id] = p; });

  var map = L.map('map', {zoomControl: true, preferCanvas: true, maxZoom: 21, minZoom: 2, zoomSnap: 0.5});
  map.attributionControl.setPrefix(false);
  if (!offline) {
    var bases = {
      'Imagery (Esri)': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        {maxZoom: 21, maxNativeZoom: 19, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics and the GIS User Community'}),
      'Topographic (Esri)': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
        {maxZoom: 21, maxNativeZoom: 19, attribution: 'Tiles &copy; Esri, HERE, Garmin, USGS, OpenStreetMap contributors and the GIS User Community'}),
      'Streets (OpenStreetMap)': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
        {maxZoom: 21, maxNativeZoom: 19, attribution: '&copy; OpenStreetMap contributors'})
    };
    bases['Imagery (Esri)'].addTo(map);
    L.control.layers(bases, null, {position: 'topright'}).addTo(map);
  } else {
    document.getElementById('map').classList.add('offline');
    document.getElementById('mapnote').textContent = 'No basemap: this report was built without map tiles and makes no network requests.';
  }
  L.control.scale({imperial: false}).addTo(map);

  var group = L.featureGroup().addTo(map);
  var selLayer = L.layerGroup().addTo(map), hl = null, selected = null, popup = null;
  var info = document.getElementById('mapinfo'), idle = info.textContent;

  P.forEach(function (p) {
    var m = L.circleMarker([p.la, p.lo], {radius: p.r <= 15 ? 8 : 6, color: '#212121', weight: 1, fillColor: C.colors[p.rt], fillOpacity: 0.92});
    if (p.r <= 15) m.bindTooltip(String(p.r), {permanent: true, direction: 'right', offset: [7, 0], className: 'rk'});
    m.on('click', function () { select(p.id, {from: 'map'}); });
    m.on('mouseover', function () { info.textContent = p.id + ' \u00b7 ' + C.names[p.rt] + ' \u00b7 score ' + p.s + ' \u00b7 ' + p.m + ' (click for details)'; });
    m.on('mouseout', function () { info.textContent = selected ? selectedText() : idle; });
    markers[p.id] = m; group.addLayer(m);
  });
  if (D.obs && D.obs.length) {
    var obsLayer = L.layerGroup().addTo(map);
    D.obs.forEach(function (o) {
      var mk = L.circleMarker([o[0], o[1]], {radius: 5, color: '#4a148c', weight: 2, fillColor: o[5] ? '#ffffff' : '#ce93d8', fillOpacity: 0.95});
      mk.bindTooltip(esc((o[3] || 'Obstacle') + ' \u00b7 ' + o[2] + ' m AGL' + (o[5] ? ' (assumed height)' : '') + (o[4] !== 'point' ? ' \u00b7 ' + o[4] : '')), {direction: 'top'});
      obsLayer.addLayer(mk);
    });
  }
  var searchBounds = null;
  if (C.search && C.search.centres.length) {
    searchBounds = L.latLngBounds([]);
    C.search.centres.forEach(function (c) {
      L.circle(c, {radius: C.search.radius_m, color: '#0d47a1', weight: 2.5, dashArray: '9 6', fill: false, interactive: false}).addTo(map);
      L.circleMarker(c, {radius: 6, color: '#0d47a1', weight: 3, fillColor: '#fff', fillOpacity: 1, interactive: false}).addTo(map);
      [0, 90, 180, 270].forEach(function (b) { searchBounds.extend(dest(c[0], c[1], b, C.search.radius_m)); });
    });
  }
  function fitAll() {
    var b = null;
    if (P.length) { var g = group.getBounds(); b = L.latLngBounds(g.getSouthWest(), g.getNorthEast()); }
    if (searchBounds && searchBounds.isValid()) { if (b) b.extend(searchBounds); else b = searchBounds; }
    if (b) map.fitBounds(b.pad(0.08), {maxZoom: 17}); else map.setView([0, 0], 2);
  }
  fitAll();

  var Home = L.Control.extend({options: {position: 'topleft'}, onAdd: function () {
    var box = L.DomUtil.create('div', 'leaflet-bar hlz-home'), a = L.DomUtil.create('a', '', box);
    a.href = '#'; a.title = 'Zoom to all candidates'; a.setAttribute('role', 'button'); a.setAttribute('aria-label', 'Zoom to all candidates'); a.innerHTML = '&#8962;';
    L.DomEvent.on(a, 'click', function (e) { L.DomEvent.preventDefault(e); fitAll(); });
    return box; }});
  new Home().addTo(map);

  function dest(la, lo, brg, d) {
    var R = 6371008.8, b = brg * Math.PI / 180, f = la * Math.PI / 180, l = lo * Math.PI / 180, dr = d / R;
    var f2 = Math.asin(Math.sin(f) * Math.cos(dr) + Math.cos(f) * Math.sin(dr) * Math.cos(b));
    var l2 = l + Math.atan2(Math.sin(b) * Math.sin(dr) * Math.cos(f), Math.cos(dr) - Math.sin(f) * Math.sin(f2));
    return [f2 * 180 / Math.PI, ((l2 * 180 / Math.PI + 540) % 360) - 180];
  }
  function selectedText() { var p = byId[selected]; return p ? p.id + ' \u00b7 ' + C.names[p.rt] + ' \u00b7 score ' + p.s + ' \u00b7 ' + p.m : idle; }

  function drawSelection(p) {
    selLayer.clearLayers();
    var col = C.colors[p.rt], secCol = {C: '#5cb85c', M: '#f0ad00', B: '#d32f2f', I: '#bdbdbd'};
    for (var i = 0; i < p.sec.length; i++) {
      var az = i * C.az_step, pts = [[p.la, p.lo]];
      for (var a = az - C.half_width; a <= az + C.half_width + 0.01; a += 3) pts.push(dest(p.la, p.lo, a, C.range_m));
      L.polygon(pts, {stroke: false, fillColor: secCol[p.sec.charAt(i)] || '#bdbdbd', fillOpacity: 0.26, interactive: false}).addTo(selLayer);
    }
    L.circle([p.la, p.lo], {radius: C.tdp_r + C.ring_m, color: col, weight: 1.5, dashArray: '5 4', fill: false, interactive: false}).addTo(selLayer);
    L.circle([p.la, p.lo], {radius: C.tdp_r, color: col, weight: 2.5, fillColor: col, fillOpacity: 0.2, interactive: false}).addTo(selLayer);
    if (p.af !== null) {
      L.polyline([dest(p.la, p.lo, p.af, C.range_m), [p.la, p.lo]], {color: '#0d47a1', weight: 3, opacity: 0.95, interactive: false}).addTo(selLayer);
      var inner = C.tdp_r + C.ring_m;
      L.polygon([dest(p.la, p.lo, p.af, inner + 4), dest(p.la, p.lo, p.af + 4, inner + 34), dest(p.la, p.lo, p.af - 4, inner + 34)],
        {stroke: false, fillColor: '#0d47a1', fillOpacity: 1, interactive: false}).addTo(selLayer);
    }
  }

  function popupHtml(p) {
    var rows = [['MGRS', p.m], ['Elevation', p.el + ' m'], ['Slope', p.sl + '\u00b0' + (p.up ? ' (upslope landing only)' : '')]];
    rows.push(['Best approach', p.af === null ? 'none' : 'from ' + pad3(p.af) + '\u00b0, land ' + pad3(p.lh) + '\u00b0, angle ' + p.aa + '\u00b0']);
    rows.push(['Clear sectors', p.cl + ' of ' + p.as]);
    if (p.sd !== null && p.sd !== undefined) rows.push(['From search centre', p.sd + ' m, bearing ' + pad3(p.sb) + '\u00b0']);
    if (p.ex !== null && p.ex !== undefined) rows.push(['Nearest exclusion', p.ex + ' m']);
    if (p.ob !== null && p.ob !== undefined) rows.push(['Nearest listed obstacle', (p.obt ? p.obt + ', ' : '') + p.ob + ' m']);
    rows.push(['Limiting factor', p.lim]);
    return '<div class="hlz-pop"><b>' + esc(p.id) + '</b><span class="chip" style="background:' + C.colors[p.rt] + '">' + esc(C.names[p.rt]) + ' ' + p.s + '</span><table>' +
      rows.map(function (r) { return '<tr><td>' + esc(r[0]) + '</td><td>' + esc(r[1]) + '</td></tr>'; }).join('') + '</table></div>';
  }

  var mapbox = document.getElementById('mapbox');
  function mapVisible() { var r = mapbox.getBoundingClientRect(); return r.bottom > 80 && r.top < window.innerHeight - 80; }
  function select(id, opt) {
    opt = opt || {}; var p = byId[id]; if (!p) return;
    selected = id; drawSelection(p);
    if (hl) hl.remove();
    hl = L.circleMarker([p.la, p.lo], {radius: 13, color: '#0d47a1', weight: 3, fill: false, interactive: false}).addTo(map);
    info.textContent = selectedText();
    if (opt.from !== 'map') {
      var reach = Math.max(C.tdp_r + C.ring_m, 40) * 1.8;
      var b = L.latLngBounds([dest(p.la, p.lo, 180, reach)[0], dest(p.la, p.lo, 270, reach)[1]], [dest(p.la, p.lo, 0, reach)[0], dest(p.la, p.lo, 90, reach)[1]]);
      map.flyToBounds(b, {maxZoom: 19, duration: 0.7, padding: [10, 10]});
      if (!mapVisible()) mapbox.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
    if (popup) map.closePopup(popup);
    popup = L.popup({maxWidth: 320, autoPanPadding: [10, 10]}).setLatLng([p.la, p.lo]).setContent(popupHtml(p)).openOn(map);
    var old = document.querySelector('tr.sel'); if (old) old.classList.remove('sel');
    var row = document.getElementById('row-' + id);
    if (row) {
      row.classList.add('sel');
      if (opt.from === 'map') { var y = row.getBoundingClientRect().top + window.pageYOffset - mapbox.offsetHeight - 110; window.scrollTo({top: Math.max(y, 0), behavior: 'smooth'}); }
    }
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('.goto') : null;
    if (t && t.getAttribute('data-id')) { e.preventDefault(); select(t.getAttribute('data-id'), {from: 'table'}); }
  });

  var filters = document.getElementById('mapfilters');
  if (filters) {
    C.names.forEach(function (name, idx) {
      var count = P.filter(function (p) { return p.rt === idx; }).length; if (!count) return;
      var lab = document.createElement('label'), box = document.createElement('input');
      box.type = 'checkbox'; box.checked = true;
      lab.appendChild(box); var dot = document.createElement('i'); dot.style.background = C.colors[idx]; lab.appendChild(dot);
      lab.appendChild(document.createTextNode(name + ' (' + count + ')'));
      box.addEventListener('change', function () {
        P.forEach(function (p) { if (p.rt === idx) { if (box.checked) group.addLayer(markers[p.id]); else group.removeLayer(markers[p.id]); } });
        Array.prototype.forEach.call(document.querySelectorAll('tr.r[data-rt="' + idx + '"]'), function (r) { r.hidden = !box.checked; });
      });
      filters.appendChild(lab);
    });
  }
  function sizeBar() { document.documentElement.style.setProperty('--mapbox-h', mapbox.offsetHeight + 'px'); map.invalidateSize(); }
  window.addEventListener('resize', sizeBar); sizeBar(); setTimeout(sizeBar, 300);
  window.HLZ = {map: map, select: select, markers: markers, data: D};
})();
"""


def map_block(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (html for the map box, scripts) for a payload from ``map_payload``."""
    cfg = payload["cfg"]
    note = ""
    if cfg["total"] > len(payload["pts"]):
        note = f" The map shows the best {len(payload['pts']):,} of {cfg['total']:,} candidates."
    if cfg["basemap"] == "ONLINE":
        note += (
            " Map tiles are loaded from third-party servers (Esri, OpenStreetMap) when this report is opened, "
            "which reveals the area being viewed. Rebuild the report with the basemap set to None for sensitive work."
        )
    html = (
        '<div id="mapbox"><div id="map" role="region" aria-label="Map of the candidate landing zones"></div>'
        '<div id="mapbar"><span id="mapinfo">Hover a candidate for a summary, click it for details. '
        "Click an ID in the table to zoom to that site.</span><span id=\"mapfilters\"></span></div></div>"
        f'<p class="mapnote" id="mapnote">{"Dashed blue circle = search radius. " if cfg.get("search") else ""}{"Purple dots = listed obstacles (hollow = height assumed). " if payload.get("obs") else ""}Blue arrow = best approach (points into the landing zone). Sector colours for the '
        f"selected site: green clear, amber steep, red blocked, grey no data.{note}</p>"
    )
    scripts = (
        f'<script id="hlzdata" type="application/json">{_json_for_html(payload)}</script>\n'
        f"<script>{leaflet_js()}</script>\n<script>{MAP_JS}</script>"
    )
    return html, scripts


def combined_css() -> str:
    return leaflet_css() + MAP_CSS


def sanity_check() -> bool:
    """True when the embedded library is present and safe to inline."""
    text = leaflet_js()
    return "</script" not in text.lower() and bool(re.search(r"leaflet", text, re.I))
