"""The view snapshot paints what the person sees, and nothing they do not (CM2).

Found in the cross-app matrix: a third-party view kept its side panel laid out inside a
collapsed (zero-width, ``overflow: hidden``) container and a pull tab at ``opacity: 0``;
the snapshot painted their labels over the view, and the agent described a sidebar the
person could not see. Runs the snapshot script in headless Chromium.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clio_agent.gact.mcp_app_sandbox import _SANDBOX_DOCUMENT
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
<div style="width:90px;font:16px sans-serif">wrapped words on lines</div>
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
            # Shown, partly cut (clipped, still drawn), the absolutely positioned label
            # whose overflow-hidden ancestor is not its containing block, and a wrapped run
            # drawn word by word: four of six, and no wrapped-text gap.
            assert result["painted"]["text"] == 4
            assert not [row for row in result["skipped"] if row["what"] == "text"]
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


# A view whose WebGL canvas reads blank until it redraws (vtk.js keeps no drawing buffer):
# it draws only when its frame is resized, which is what the snapshot's nudge does.
_WEBGL_VIEW = """<!doctype html><html><body style="margin:0">
<canvas id=c width=300 height=200 style="width:300px;height:200px"></canvas>
<script>
const gl = document.getElementById('c').getContext('webgl', {preserveDrawingBuffer: false});
addEventListener('resize', () => { gl.clearColor(0.9, 0.2, 0.6, 1); gl.clear(gl.COLOR_BUFFER_BIT); });
</script></body></html>"""


def _host(style: str) -> str:
    view = json.dumps(_WEBGL_VIEW).replace("</", "<\\/")
    return f"""<!doctype html><html><body style="margin:0">
<iframe id=f src="http://sandbox.test/sandbox.html"
  style="position:absolute;left:0;top:0;width:320px;height:220px;border:0;{style}"></iframe>
<script>
window.answer = new Promise((resolve) => {{
  const f = document.getElementById('f');
  addEventListener('message', (e) => {{
    if (e.data?.method === 'ui/notifications/sandbox-proxy-ready') {{
      f.contentWindow.postMessage({{jsonrpc: '2.0', method: 'ui/notifications/sandbox-resource-ready',
        params: {{html: {view}}}}}, '*');
      setTimeout(() => f.contentWindow.postMessage(
        {{jsonrpc: '2.0', id: 1, method: 'clio/view-snapshot', params: {{}}}}, '*'), 1000);
    }} else if (e.data?.id === 1) resolve(e.data.result);
  }});
}});
</script></body></html>"""


@pytest.mark.parametrize(
    ("style", "expect"),
    [("", "drawn"), ("visibility:hidden", "not_drawing"), ("clip-path:inset(100%)", "either")],
)
def test_a_view_the_browser_is_not_drawing_is_still_answered(style: str, expect: str) -> None:
    """A laid-out view that is hidden or clipped away runs no frame callbacks; the snapshot
    waited for one forever, so the agent's look held its turn (883 s in a demo take)."""
    executable = _chromium()
    if executable is None:
        pytest.skip("no Chromium for the snapshot script")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable, env={**os.environ, "TMPDIR": "/tmp"}
        )
        try:
            page = browser.new_page(viewport={"width": 400, "height": 400})
            page.route(
                "http://host.test/**",
                lambda route: route.fulfill(content_type="text/html", body=_host(style)),
            )
            page.route(
                "http://sandbox.test/**",
                lambda route: route.fulfill(content_type="text/html", body=_SANDBOX_DOCUMENT),
            )
            page.goto("http://host.test/")
            result = page.evaluate(
                "Promise.race([window.answer, "
                "new Promise((r) => setTimeout(() => r('no answer'), 10000))])"
            )
        finally:
            browser.close()
    assert result != "no answer"
    reasons = [row["reason"] for row in result["skipped"]]
    drew = result["painted"]["canvas"] == 1 and not reasons
    not_drawing = "view_not_drawing" in reasons and result["painted"]["canvas"] == 0
    # Chromium may or may not draw a frame clipped to nothing: either way it is answered.
    assert {"drawn": drew, "not_drawing": not_drawing, "either": drew or not_drawing}[expect]
