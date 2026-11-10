"""Phase 0 probe: an element picker in a managed page, alongside the live view's screencast.

Launches a persistent context with the managed backend's options (headless, no Chromium
sandbox as on the cloud stack), installs a picker with ``add_init_script``, receives its
reports through ``expose_binding``, runs a screencast as the live view does, clicks with
``page.mouse`` as the live view's input does, navigates again to check the script survives,
and reads the picked node's role and name from Chromium's accessibility tree over CDP.

    uv run --no-sync python -I scripts/cloud/comments_probe/probe_picker.py \
        http://a.fixture.localhost:8790/form
"""

import asyncio
import json
import sys
import tempfile

from playwright.async_api import async_playwright

CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
PICKER = r"""
(() => {
  if (window.__clioPicker) return;
  window.__clioPicker = true;
  const report = (kind, el) => {
    const r = el.getBoundingClientRect();
    window.__clioPickReport({
      kind, selector: el.id ? '#' + CSS.escape(el.id) : el.tagName.toLowerCase(),
      tag: el.tagName.toLowerCase(), text: (el.innerText || '').slice(0, 80),
      rect: [r.x, r.y, r.width, r.height], viewport: innerWidth,
      frame: window === top ? 'top' : 'child',
    });
  };
  addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation(); report('selected', e.target);
  }, true);
})();
"""


async def main(url: str) -> None:
    reports: list[dict] = []
    frames = 0
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(),
            executable_path=CHROMIUM,
            headless=True,
            chromium_sandbox=False,
            viewport={"width": 1280, "height": 800},
        )

        async def on_pick(source: dict, payload: dict) -> None:
            reports.append({**payload, "page_url": source["page"].url})

        await context.expose_binding("__clioPickReport", on_pick)
        await context.add_init_script(PICKER)
        page = await context.new_page()

        def on_frame(_frame: dict) -> None:
            nonlocal frames
            frames += 1

        await page.screencast.start(
            on_frame=on_frame, quality=60, size={"width": 640, "height": 400}
        )
        for _ in range(2):  # the second pass checks the script survives a navigation
            await page.goto(url)
            box = await page.locator("button, input[type=submit]").first.bounding_box()
            assert box is not None
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.wait_for_timeout(500)
        cdp = await context.new_cdp_session(page)
        document = await cdp.send("DOM.getDocument", {"depth": 0})
        selector = reports[-1]["selector"] if reports else "button"
        node = await cdp.send(
            "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector}
        )
        tree = await cdp.send(
            "Accessibility.getPartialAXTree", {"nodeId": node["nodeId"], "fetchRelatives": False}
        )
        picked = tree["nodes"][0]
        print(
            json.dumps(
                {
                    "reports": reports,
                    "screencast_frames": frames,
                    "ax_role": picked.get("role", {}).get("value"),
                    "ax_name": picked.get("name", {}).get("value"),
                },
                indent=1,
            )
        )
        await context.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
