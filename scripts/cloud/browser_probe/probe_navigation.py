"""Phase 0 probe, part 2: redirect gating, abort codes, annotations, downloads, cdp.

Run against the fixture site (``tests/browser/fixtures/site/server.py --port 8790``) with a
Python that has ``playwright==1.64.1`` and its Chromium::

    python scripts/cloud/browser_probe/probe_navigation.py .cloud-run/probe_navigation.json

Results are recorded in ``docs/design/sdk/browser-ledger.md`` (Phase 0 measurements).
"""

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

BASE = "http://a.fixture.localhost:8790"
OUT: dict = {}


def rec(key, value):
    OUT[key] = value
    print(f"{key}: {value}", flush=True)


async def gated_context(chromium, profile, denied: set[str], seen: list):
    ctx = await chromium.launch_persistent_context(
        profile,
        headless=True,
        channel="chromium",
        chromium_sandbox=False,
        accept_downloads=True,
        viewport={"width": 1280, "height": 800},
    )

    async def gate(page):
        cdp = await ctx.new_cdp_session(page)
        tree = await cdp.send("Page.getFrameTree")
        main_id = tree["frameTree"]["frame"]["id"]

        async def paused(event):
            req = event["request"]
            host = urlsplit(req["url"]).hostname or ""
            main = event.get("frameId") == main_id
            if main:
                seen.append((req["method"], req["url"], bool(event.get("redirectedRequestId"))))
            try:
                if main and host in denied:
                    await cdp.send(
                        "Fetch.failRequest",
                        {"requestId": event["requestId"], "errorReason": "Aborted"},
                    )
                else:
                    await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            except Exception as exc:  # noqa: BLE001
                seen.append(("ERROR", str(exc)[:120], False))

        cdp.on("Fetch.requestPaused", lambda e: asyncio.ensure_future(paused(e)))
        await cdp.send(
            "Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
        )

    for p in ctx.pages:
        await gate(p)
    ctx.on("page", lambda p: asyncio.ensure_future(gate(p)))
    return ctx


async def main():
    async with async_playwright() as pw:
        chromium = pw.chromium
        profile = tempfile.mkdtemp(prefix="p0b-profile-")
        try:
            ctx = await chromium.launch_persistent_context(
                profile, headless=True, chromium_sandbox=True
            )
            await ctx.close()
        except Exception as exc:  # noqa: BLE001
            lines = [line for line in str(exc).splitlines() if "sandbox" in line.lower()]
            rec("sandbox_as_root_reason", lines[:3])
        seen: list = []
        denied: set[str] = set()
        ctx = await gated_context(chromium, profile, denied, seen)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # 1. redirect hops through fetch+fulfill
        await page.goto(BASE + "/redirect-to-b")
        rec("redirect_hops", seen[:])
        rec("redirect_final", page.url)
        seen.clear()
        denied.add("b.fixture.localhost")
        await page.goto(BASE + "/")
        try:
            await page.goto(BASE + "/redirect-to-b")
            rec("denied_redirect_goto", "no error")
        except Exception as exc:  # noqa: BLE001
            rec("denied_redirect_goto", str(exc).splitlines()[0][:160])
        rec("denied_redirect_hops", seen[:])
        rec("denied_redirect_page_url", page.url)
        # 2. abort("aborted") on a clicked link keeps the page
        await page.goto(BASE + "/link-to-b")
        await page.click("#go")
        await page.wait_for_timeout(500)
        rec("aborted_click_page_url", page.url)
        denied.clear()
        # 3. cookies set by a fetched 303 response (sign-in through fetch+fulfill)
        await page.goto(BASE + "/signin")
        await page.fill("input[name=username]", "ada")
        await page.fill("input[type=password]", "correct horse")
        await page.click("button")
        await page.wait_for_load_state()
        rec("signin_through_gate_url", page.url)
        rec("signin_through_gate_cookies", [c["name"] for c in await ctx.cookies()])
        # 4. a timed redirect with no agent call
        seen.clear()
        await page.goto(BASE + "/timed-redirect")
        t0 = time.monotonic()
        await page.wait_for_url("**b.fixture.localhost**", timeout=8000)
        rec("timed_redirect_after_s", round(time.monotonic() - t0, 1))
        rec("timed_redirect_hops", [u for _, u, _r in seen])
        # 5. downloads
        await page.goto(BASE + "/download")
        async with page.expect_download(timeout=10000) as info:
            await page.click("text=Download the report")
        dl = await info.value
        target = tempfile.mkdtemp(prefix="p0b-dl-")
        await dl.save_as(f"{target}/{dl.suggested_filename}")
        rec("download", [dl.suggested_filename, dl.url])
        shutil.rmtree(target, ignore_errors=True)
        # 6. annotations: are they in a page screenshot (so also in a real window)?
        await page.goto(BASE + "/form")
        async with await page.screencast.show_actions(
            style={"highlight": "outline: 6px solid rgb(255, 0, 255)"}, duration=2000
        ):
            await page.get_by_label("Title").click()
            await page.wait_for_timeout(150)
            shot = await page.screenshot()
        rec("annotation_png_bytes", len(shot))
        path = sys.argv[1].replace(".json", "-annotation.png")
        open(path, "wb").write(shot)
        rec("annotation_screenshot", path)
        await ctx.close()
        # 7. persistence across relaunch
        ctx = await chromium.launch_persistent_context(
            profile, headless=True, channel="chromium", chromium_sandbox=False
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(BASE + "/account")
        rec("persisted_signin", page.url.endswith("/account"))
        await ctx.close()
        # 8. cdp no_defaults
        port = 9333
        other = tempfile.mkdtemp(prefix="p0b-cdp-")
        proc = subprocess.Popen(
            [
                chromium.executable_path,
                "--headless=new",
                "--no-sandbox",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={other}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            browser = None
            for _ in range(50):
                try:
                    browser = await chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", no_defaults=True
                    )
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.2)
            rec("cdp_contexts", len(browser.contexts))
            page = await browser.contexts[0].new_page()
            await page.goto(BASE + "/")
            rec("cdp_title", await page.title())
            async with await page.screencast.show_actions():
                await page.click("text=table")
            rec("cdp_show_actions", "ok")
            frames = []
            async with await page.screencast.start(on_frame=lambda f: frames.append(1)):
                await page.wait_for_timeout(300)
                await page.mouse.wheel(0, 200)
                await page.wait_for_timeout(300)
            rec("cdp_screencast_frames", len(frames))
            await page.close()
            await browser.close()
        finally:
            proc.terminate()
            proc.wait(10)
            shutil.rmtree(other, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


asyncio.run(main())
json.dump(OUT, open(sys.argv[1], "w"), indent=1)
