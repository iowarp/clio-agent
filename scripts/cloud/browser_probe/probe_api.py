"""Phase 0 probe, part 1: each Playwright call the plan names, against the fixture site.

Run like ``probe_navigation.py``; results are in ``docs/design/sdk/browser-ledger.md``. Its
download step predates a fixture routing fix and its route-handler denial step shows why the
service gates with DevTools ``Fetch`` instead (BR20).
"""

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time

from playwright.async_api import async_playwright

BASE = "http://a.fixture.localhost:8790"
OTHER = "http://b.fixture.localhost:8790"
OUT: dict = {}


def rec(key, value):
    OUT[key] = value
    print(f"{key}: {value}", flush=True)


async def main():
    async with async_playwright() as pw:
        chromium = pw.chromium
        rec("executable", chromium.executable_path)
        profile = tempfile.mkdtemp(prefix="p0-profile-")
        # 1. sandbox on, as root
        try:
            ctx = await chromium.launch_persistent_context(
                profile, headless=True, channel="chromium", chromium_sandbox=True
            )
            rec("sandbox_on_as_root", "launched")
            await ctx.close()
        except Exception as exc:  # noqa: BLE001
            rec("sandbox_on_as_root", f"failed: {str(exc).splitlines()[0][:300]}")
            text = str(exc)
            rec("sandbox_error_mentions_no_sandbox", "--no-sandbox" in text)
        # 2. unsandboxed persistent context in new headless mode
        ctx = await chromium.launch_persistent_context(
            profile,
            headless=True,
            channel="chromium",
            chromium_sandbox=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        ua = await page.evaluate("navigator.userAgent")
        rec("user_agent", ua)
        rec("new_headless(no HeadlessChrome marker)", "HeadlessChrome" not in ua)
        # 3. route handler on main-frame navigations
        seen = []

        async def route(r):
            req = r.request
            if req.is_navigation_request() and req.frame.parent_frame is None:
                seen.append(
                    {
                        "url": req.url,
                        "redirected_from": bool(req.redirected_from),
                        "method": req.method,
                    }
                )
                if "hold" in req.url:
                    t0 = time.monotonic()
                    await asyncio.sleep(3)
                    seen[-1]["held_s"] = round(time.monotonic() - t0, 2)
                if "b.fixture" in req.url and "deny" in OUT.get("_mode", ""):
                    await r.abort("blockedbyclient")
                    return
            await r.continue_()

        await ctx.route("**/*", route)
        await page.goto(BASE + "/link-to-b")
        await page.click("#go")
        await page.wait_for_load_state()
        rec("route_link_click", [s["url"] for s in seen])
        seen.clear()
        await page.goto(BASE + "/redirect-to-b")
        rec("route_redirect", seen[:])
        seen.clear()
        await page.goto(BASE + "/signin")
        await page.fill("input[name=username]", "ada")
        await page.fill("input[type=password]", "correct horse")
        await page.click("button")
        await page.wait_for_load_state()
        rec("route_form_post", [(s["method"], s["url"]) for s in seen])
        rec("signed_in_text", await page.text_content("#who"))
        seen.clear()
        t0 = time.monotonic()
        await page.goto(BASE + "/?hold=1")
        rec("route_hold_navigation_s", round(time.monotonic() - t0, 2))
        # denied navigation: what does the page/goto see?
        OUT["_mode"] = "deny"
        await page.goto(BASE + "/link-to-b")
        try:
            await page.click("#go")
            await page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            rec("deny_click_raises", str(exc)[:200])
        rec("deny_page_url_after_click", page.url)
        try:
            await page.goto(BASE + "/redirect-to-b")
            rec("deny_redirect_goto", "no error")
        except Exception as exc:  # noqa: BLE001
            rec("deny_redirect_goto", str(exc).splitlines()[0][:200])
        rec("deny_redirect_page_url", page.url)
        OUT["_mode"] = ""
        # 4. aria snapshot ai mode, refs, boxes
        await page.goto(BASE + "/table")
        snap = await page.aria_snapshot(mode="ai")
        rec("aria_ai_first_lines", snap.splitlines()[:6])
        snapb = await page.locator("body").aria_snapshot(mode="ai", boxes=True)
        rec("aria_ai_boxes_sample", [line for line in snapb.splitlines() if "box" in line][:2])
        import re

        ref = re.search(r"\[ref=(e\d+)\]", snap)
        rec("ref_example", ref.group(1) if ref else None)
        link = re.search(r'link "Next" \[ref=(e\d+)\]', snap)
        if link:
            await page.get_by_ref(link.group(1)).click()
            await page.wait_for_load_state()
            rec("get_by_ref_click_url", page.url)
        # strictness
        try:
            await page.get_by_role("row").click(timeout=2000)
        except Exception as exc:  # noqa: BLE001
            rec("strictness_error", str(exc).splitlines()[0][:200])
        # frames
        await page.goto(BASE + "/frames")
        btn = page.frame_locator("iframe#results").get_by_role("button")
        await btn.click()
        rec("frame_button_after", await btn.inner_text())
        # 5. screencast frames + show_actions with style
        frames = []

        def on_frame(frame):
            frames.append((time.monotonic(), len(frame["data"]), frame["viewportWidth"]))

        await page.goto(BASE + "/form")
        async with await page.screencast.start(
            on_frame=on_frame, quality=70, size={"width": 960, "height": 600}
        ):
            async with await page.screencast.show_actions(
                style={
                    "point": "background: #c2410c",
                    "highlight": "outline: 2px solid #c2410c",
                    "title": "font: 16px sans-serif",
                },
                duration=400,
            ):
                await page.wait_for_timeout(300)
                n0 = len(frames)
                t_click = time.monotonic()
                await page.get_by_label("Title").click()
                await page.wait_for_timeout(600)
                after = [t for t, *_ in frames[n0:]]
                rec(
                    "first_frame_after_click_ms",
                    round((after[0] - t_click) * 1000) if after else None,
                )
                dom_has_overlay = await page.evaluate(
                    "() => [...document.querySelectorAll('*')].some(e => e.shadowRoot || "
                    "e.tagName.toLowerCase().includes('playwright') || "
                    "(e.id||'').includes('playwright'))"
                )
                rec("annotation_in_dom", dom_has_overlay)
                t_type = time.monotonic()
                n1 = len(frames)
                await page.keyboard.type("hello")
                await page.wait_for_timeout(400)
                after = [t for t, *_ in frames[n1:]]
                rec(
                    "first_frame_after_type_ms",
                    round((after[0] - t_type) * 1000) if after else None,
                )
        rec("frames_total", len(frames))
        rec(
            "frame_bytes_median", sorted(b for _, b, _ in frames)[len(frames) // 2] if frames else 0
        )
        rec("frame_viewport_width", frames[0][2] if frames else None)
        # 6. download
        await page.goto(BASE + "/download")
        async with page.expect_download() as info:
            await page.click("text=Download the report")
        dl = await info.value
        rec("download_suggested", dl.suggested_filename)
        await ctx.close()
        # 7. persistence of the cookie across a relaunch
        ctx = await chromium.launch_persistent_context(
            profile, headless=True, channel="chromium", chromium_sandbox=False
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(BASE + "/account")
        rec("persisted_signin", page.url.endswith("/account"))
        await ctx.close()
        # 8. connect_over_cdp(no_defaults=True) to a separately started Chromium
        port = 9333
        other_profile = tempfile.mkdtemp(prefix="p0-cdp-")
        proc = subprocess.Popen(
            [
                chromium.executable_path,
                "--headless=new",
                "--no-sandbox",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={other_profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    browser = await chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", no_defaults=True
                    )
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.2)
            rec("cdp_contexts", len(browser.contexts))
            ctx = browser.contexts[0]
            page = await ctx.new_page()
            await page.goto(BASE + "/")
            rec("cdp_page_title", await page.title())
            async with await page.screencast.show_actions():
                await page.click("text=table")
                await page.wait_for_timeout(200)
                rec("cdp_show_actions", "ok")
            await page.close()
            await browser.close()
        finally:
            proc.terminate()
            proc.wait(10)
            shutil.rmtree(other_profile, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


asyncio.run(main())
OUT.pop("_mode", None)
json.dump(OUT, open(sys.argv[1] if len(sys.argv) > 1 else "probe.json", "w"), indent=1)
