"""Phase 0 probe: render an HTML version on the server for a crop, as the preview frame shows it.

The preview frame (``desktop-html-frame.tsx``) runs scripts (``sandbox="allow-scripts"``) and
its content route's CSP refuses every network request. The probe renders in its own short-lived
Chromium with scripts on and every request aborted, at the viewer's width and 2x, then crops a
CSS-pixel box (a picked element's rectangle).

    uv run --no-sync python -I scripts/cloud/comments_probe/probe_html_render.py
"""

import asyncio
import io
import time

from PIL import Image
from playwright.async_api import async_playwright

CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
HTML = """<!doctype html><html><body style="margin:0;font:16px sans-serif">
<main><section class="controls" style="padding:96px">
<button id="run-sim" class="btn primary" disabled>Run simulation</button></section>
<img src="https://example.org/x.png" alt="remote"></main>
<script>document.querySelector('.controls')
  .insertAdjacentHTML('beforeend', '<p id="added">added by script</p>')</script>
</body></html>"""


async def main() -> None:
    started = time.monotonic()
    blocked: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROMIUM, headless=True, chromium_sandbox=False
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800}, device_scale_factor=2, offline=True
        )

        async def deny(route) -> None:
            blocked.append(route.request.url)
            await route.abort()

        await context.route("**/*", deny)
        page = await context.new_page()
        await page.set_content(HTML, wait_until="load")
        box = await page.locator("#run-sim").bounding_box()
        script_ran = await page.locator("#added").count() == 1
        image = Image.open(io.BytesIO(await page.screenshot(full_page=True, type="png")))
        assert box is not None
        x, y, w, h = box["x"], box["y"], box["width"], box["height"]
        crop = image.crop((int(x * 2), int(y * 2), int((x + w) * 2), int((y + h) * 2)))
        print(
            {
                "button_css_box": box,
                "script_ran": script_ran,
                "blocked_requests": blocked,
                "page_png_px": image.size,
                "crop_px": crop.size,
                "elapsed_s": round(time.monotonic() - started, 2),
            }
        )
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
