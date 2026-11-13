"""The view snapshot paints what the person sees, and nothing they do not (CM2).

Found in the cross-app matrix: a third-party view kept its side panel laid out inside a
collapsed (zero-width, ``overflow: hidden``) container and a pull tab at ``opacity: 0``;
the snapshot painted their labels over the view, and the agent described a sidebar the
person could not see. Runs the snapshot script in headless Chromium.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.gact.mcp_app_snapshot_script import SNAPSHOT_SCRIPT

sync_api = pytest.importorskip("playwright.sync_api")


def _chromium() -> str | None:
    roots = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")), Path("/opt/pw-browsers")]
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in [root / "chromium", *sorted(root.glob("chromium-*"), reverse=True)]:
            for sub in ("", "chrome-linux64/chrome", "chrome-linux/chrome"):
                exe = candidate / sub if sub else candidate
                if exe.is_file() and os.access(exe, os.X_OK):
                    return str(exe)
    return None


_PAGE = """<!doctype html><html><body style="margin:0;background:#fff">
<div id="seen" style="font:16px sans-serif;color:#000">Shown label</div>
<div style="width:0;overflow:hidden;height:200px">
  <aside style="width:300px;background:#123456;font:16px sans-serif">
    <span>Collapsed panel label</span>
  </aside>
</div>
<div style="opacity:0"><span style="font:16px sans-serif">Transparent label</span></div>
<div style="width:60px;overflow:hidden;white-space:nowrap">
  <span id="cut" style="font:16px sans-serif">Partly cut label</span>
</div>
<div style="position:relative;width:0;height:0;overflow:visible">
  <div style="overflow:hidden;width:0;height:0">
    <span style="position:absolute;left:200px;top:-100px;white-space:nowrap;font:16px sans-serif">Escapes its clip</span>
  </div>
</div>
</body></html>"""


def test_hidden_clipped_and_transparent_html_is_not_painted() -> None:
    executable = _chromium()
    if executable is None:
        pytest.skip("no Chromium for the snapshot script")
    with sync_api.sync_playwright() as playwright:
        # Chromium's singleton socket must fit a UNIX socket path: a short temporary root.
        browser = playwright.chromium.launch(
            executable_path=executable, env={**os.environ, "TMPDIR": "/tmp"}
        )
        try:
            page = browser.new_page(viewport={"width": 400, "height": 400})
            page.set_content(_PAGE)
            page.add_script_tag(content=SNAPSHOT_SCRIPT + "\nwindow.__snap = clioViewSnapshot;")
            result = page.evaluate(
                "async () => { const r = await window.__snap(window, {}); "
                "return {painted: r.painted, skipped: r.skipped}; }"
            )
            # Shown, partly cut (clipped, still drawn) and the absolutely positioned label
            # whose overflow-hidden ancestor is not its containing block: three of five.
            assert result["painted"]["text"] == 3
            # The collapsed panel's background is not painted either: the pixel where its
            # label would be is the page's white.
            pixel = page.evaluate(
                """async () => {
                  const r = await window.__snap(window, {});
                  const img = new Image();
                  img.src = r.image;
                  await img.decode();
                  const c = document.createElement('canvas');
                  c.width = img.width; c.height = img.height;
                  const g = c.getContext('2d');
                  g.drawImage(img, 0, 0);
                  return Array.from(g.getImageData(20, 40, 1, 1).data);
                }"""
            )
            assert pixel[:3] == [255, 255, 255]
        finally:
            browser.close()
