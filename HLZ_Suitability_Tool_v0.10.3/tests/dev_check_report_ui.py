# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Developer check of HLZ_Report.html in a headless browser (needs playwright; not part of RUN_ALL_TESTS).

    python tests/dev_check_report_ui.py file:///path/to/HLZ_Report.html

Map tiles are replaced by a plain image so the check works without internet.
"""
import io, json
from PIL import Image
from playwright.sync_api import sync_playwright
buf = io.BytesIO(); Image.new("RGB", (256, 256), (88, 110, 82)).save(buf, "PNG"); TILE = buf.getvalue()
import sys
path = sys.argv[1] if len(sys.argv) > 1 else "file:///tmp/v65/r/HLZ_Report.html"
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True); pg = ctx.new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(("file:", "data:")) else r.fulfill(status=200, content_type="image/png", body=TILE))
    pg.goto(path); pg.wait_for_timeout(1200)
    out = {"js_errors": errs[:3]}
    out["headers_with_tips"] = pg.evaluate("document.querySelectorAll('thead th .tip').length")
    out["header_texts"] = pg.evaluate("Array.from(document.querySelectorAll('thead th')).map(t => t.firstChild.textContent)")
    out["best_approach_first_row"] = pg.inner_text("tbody tr.r:first-child td:nth-child(10)").replace("\n", " | ")
    out["from_centre_first_row"] = pg.inner_text("tbody tr.r:first-child td:nth-child(6)")
    out["plain_words_first_card"] = pg.inner_text(".detail .plain")[:330]
    out["howto_open"] = pg.evaluate("document.getElementById('howto').open")
    # tooltip visible on hover of the Slope header
    pg.hover("thead th:nth-child(7) .tip i"); pg.wait_for_timeout(200)
    out["tooltip_visible"] = pg.is_visible("thead th:nth-child(7) .tipbox"); out["tooltip_text"] = pg.inner_text("thead th:nth-child(7) .tipbox")[:200].replace("\n", " ")
    pg.screenshot(path="/tmp/ui_planner_tip.png")
    pg.evaluate("window.scrollTo(0,0)")
    pg.screenshot(path="/tmp/ui_planner_top.png")
    # switch to the pilot brief
    pg.click("#vpilot"); pg.wait_for_timeout(300)
    out["planner_hidden"] = not pg.is_visible("#planner"); out["pilot_visible"] = pg.is_visible("#pilot")
    out["pilot_cards_visible"] = pg.evaluate("Array.from(document.querySelectorAll('.pcard')).filter(c => !c.hidden).length")
    pg.fill("#pwd", "270"); pg.fill("#pws", "15"); pg.wait_for_timeout(200)
    out["wind_result"] = pg.inner_text(".pcard:not([hidden]) .windres")
    pg.select_option("#pn", "5"); pg.wait_for_timeout(200)
    out["cards_after_select_5"] = pg.evaluate("Array.from(document.querySelectorAll('.pcard')).filter(c => !c.hidden).length")
    pg.screenshot(path="/tmp/ui_pilot_top.png")
    pg.evaluate("document.querySelector('.pcard').scrollIntoView()"); pg.wait_for_timeout(150)
    pg.screenshot(path="/tmp/ui_pilot_card.png")
    with pg.expect_download() as dl: pg.click("#pdl")
    saved = dl.value.path(); text = open(saved, encoding="utf-8").read()
    out["download"] = {"name": dl.value.suggested_filename, "bytes": len(text), "has_cards": text.count("class=\"pcard\""), "has_download_button": "id='pdl'" in text}
    # back to planner; map still works
    pg.click("#vplan"); pg.wait_for_timeout(500)
    out["planner_back"] = pg.is_visible("#planner") and not pg.is_visible("#pilot")
    out["map_zoom_ok"] = pg.evaluate("HLZ.map.getSize().x > 100")
    ctx.close()
    # the stand-alone file, opened on its own
    ctx2 = b.new_context(viewport={"width": 1100, "height": 900}); pg2 = ctx2.new_page(); e2 = []; pg2.on("pageerror", lambda e: e2.append(str(e)))
    pg2.goto("file:///tmp/v65/r/HLZ_Pilot_Brief.html"); pg2.wait_for_timeout(300); pg2.fill("#pwd", "90"); pg2.wait_for_timeout(150)
    out["standalone"] = {"errors": e2, "cards": pg2.evaluate("document.querySelectorAll('.pcard').length"), "wind": pg2.inner_text(".windres")[:140]}
    pg2.screenshot(path="/tmp/ui_standalone.png"); b.close()
print(json.dumps(out, indent=1))
