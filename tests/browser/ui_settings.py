"""Opt-in real-page checks: python -m pytest tests/browser/ui_settings.py.

The filename deliberately excludes this module from ordinary pytest discovery.
Requires an installed Playwright browser; FRACTAL_TEST_BROWSER defaults to firefox.
All workers belong to LocalStack. Delays and HTTP faults below are test-only.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import async_playwright, expect

from fractal_demo.gateway import GatewaySettings
from tests.support.local_stack import LocalStack

STORAGE_KEY = "fractal-demo.ui-settings.v1"
INPUTS = ("columns", "rows", "iterations", "dwell_ms", "lost_ms")

# Keep real fetch, PNG decoding and canvas painting. Hold exactly one completion
# when requested, with explicit entered/release signals instead of timing a click.
PROBE = r"""
(() => {
  const p = window.uiProbe = {
    frame: 0, paints: [], requests: [], polls: 0, hold: null, entered: false,
    heldBitmap: null, closed: 0, release: null,
  };
  const fill = CanvasRenderingContext2D.prototype.fillRect;
  CanvasRenderingContext2D.prototype.fillRect = function(...args) {
    const result = Reflect.apply(fill, this, args);
    if (this.canvas.id === 'fractal-canvas' && args[0] === 0 && args[1] === 0 &&
        args[2] === this.canvas.width && args[3] === this.canvas.height) p.frame++;
    return result;
  };
  const draw = CanvasRenderingContext2D.prototype.drawImage;
  CanvasRenderingContext2D.prototype.drawImage = function(bitmap, x, y) {
    const result = Reflect.apply(draw, this, arguments);
    if (this.canvas.id === 'fractal-canvas') p.paints.push({
      frame: p.frame, x, y, width: bitmap.width, height: bitmap.height,
      held: bitmap === p.heldBitmap,
    });
    return result;
  };
  const fetch = window.fetch;
  window.fetch = async function(...args) {
    const url = new URL(args[0] instanceof Request ? args[0].url : args[0], location.href);
    const render = url.pathname.startsWith('/api/render/');
    if (url.pathname === '/api/workers') p.polls++;
    if (render) p.requests.push({
      frame: p.frame, worker: url.pathname, ...Object.fromEntries(url.searchParams),
    });
    const response = await Reflect.apply(fetch, this, args);
    if (render && p.hold === 'response' && !p.entered) {
      p.entered = true;
      await new Promise(resolve => { p.release = resolve; });
    }
    return response;
  };
  const decode = window.createImageBitmap;
  window.createImageBitmap = async function(...args) {
    const bitmap = await Reflect.apply(decode, this, args);
    if (p.hold === 'decode' && !p.entered) {
      p.heldBitmap = bitmap;
      p.entered = true;
      await new Promise(resolve => { p.release = resolve; });
    }
    return bitmap;
  };
  const close = ImageBitmap.prototype.close;
  ImageBitmap.prototype.close = function(...args) {
    if (this === p.heldBitmap) p.closed++;
    return Reflect.apply(close, this, args);
  };
})();
"""


def configuration(**changes):
    values = {
        "geometry": {"width": 96, "height": 64, "columns": 4, "rows": 4},
        "view": {"xmin": -2.0, "ymin": -1.0, "pixel_size": 0.035, "iterations": 80},
        "timing": {
            **GatewaySettings().timing,
            "poll_interval_ms": 50,
            "dwell_ms": 300000,
            "render_timeout_ms": 60000,
        },
        "render_limit": 1,
    }
    values.update(changes)
    return GatewaySettings(**values)


@asynccontextmanager
async def local_page(tmp_path, *, workers=1, settings=None, script=""):
    # Even a zero-worker scenario needs capacity for a worker that can return later.
    async with LocalStack(settings or configuration(), max(1, workers), tmp_path, 10) as stack:
        await stack.reset_workers(workers)
        async with async_playwright() as playwright:
            name = os.environ.get("FRACTAL_TEST_BROWSER", "firefox")
            if name not in {"firefox", "chromium"}:
                raise ValueError("FRACTAL_TEST_BROWSER must be firefox or chromium")
            options = {"headless": True}
            if name == "chromium":
                options["chromium_sandbox"] = True
            browser = await getattr(playwright, name).launch(**options)
            try:
                context = await browser.new_context(viewport={"width": 960, "height": 1000})
                try:
                    context.set_default_timeout(30000)
                    await context.add_init_script(script=PROBE + "\n" + script)
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    yield stack, page
                    assert not errors, errors
                finally:
                    await context.close()
            finally:
                await browser.close()


async def open_settings(stack, page):
    await page.goto(stack.origin)
    await expect(page.locator("#setting-columns")).to_be_enabled()
    await page.locator(".settings-panel > summary").click()


async def fill(page, **values):
    for key, value in values.items():
        await page.locator(f"#setting-{key}").fill(str(value))


async def apply(page, **values):
    await fill(page, **values)
    await page.get_by_role("button", name="Apply", exact=True).click()


async def saved(page):
    return await page.evaluate("key => JSON.parse(localStorage.getItem(key))", STORAGE_KEY)


async def finished(page, tiles=16):
    await page.wait_for_function(
        "n => { const p = document.getElementById('render-progress'); "
        "return p.max === n && p.value === n; }",
        arg=tiles,
    )
    await expect(page.locator('#tile-overlay .tile[data-state="done"]')).to_have_count(tiles)
    state = await page.evaluate("""() => ({
      frame: uiProbe.frame,
      paints: uiProbe.paints.filter(p => p.frame === uiProbe.frame),
      requests: uiProbe.requests.filter(r => r.frame === uiProbe.frame),
      width: document.getElementById('fractal-canvas').width,
      height: document.getElementById('fractal-canvas').height,
      image: document.getElementById('fractal-canvas').toDataURL(),
    })""")
    assert len(state["paints"]) == tiles
    sizes = {(p["width"], p["height"]) for p in state["paints"]}
    assert len(sizes) == 1
    width, height = sizes.pop()
    assert state["width"] % width == state["height"] % height == 0
    expected = {
        (x, y) for y in range(0, state["height"], height) for x in range(0, state["width"], width)
    }
    assert {(p["x"], p["y"]) for p in state["paints"]} == expected
    assert not any(p["held"] for p in state["paints"])
    return state


async def poll_twice(page):
    polls = await page.evaluate("uiProbe.polls")
    await page.wait_for_function("n => uiProbe.polls >= n + 2", arg=polls)


def test_navigation_grid_palette_and_auto_cycle(tmp_path):
    async def scenario():
        async with local_page(tmp_path, workers=2) as (stack, page):
            await open_settings(stack, page)
            original = await finished(page)
            assert len({r["worker"] for r in original["requests"]}) == 2
            # Navigation uses accepted iterations while preserving this draft.
            await fill(page, iterations=150)
            await page.get_by_role("button", name="Pan right", exact=True).click()
            panned = await finished(page)
            assert panned["image"] != original["image"]
            assert float(panned["requests"][0]["xmin"]) == pytest.approx(-2 + 95 * 0.035 / 4)
            assert {r["iterations"] for r in panned["requests"]} == {"80"}
            await expect(page.locator("#setting-iterations")).to_have_value("150")
            before_zoom = await saved(page)
            await page.get_by_role("button", name="Zoom in", exact=True).click()
            await finished(page)
            zoomed = await saved(page)
            assert zoomed["pixel_size"] == pytest.approx(0.0175)
            for origin, span in (("xmin", 95), ("ymin", 63)):
                assert zoomed[origin] + span * zoomed["pixel_size"] / 2 == pytest.approx(
                    before_zoom[origin] + span * before_zoom["pixel_size"] / 2
                )
            await page.get_by_role("button", name="Zoom out", exact=True).click()
            await finished(page)
            assert await saved(page) == pytest.approx(before_zoom)
            await page.get_by_role("button", name="Pan left", exact=True).click()
            await finished(page)
            assert (await saved(page))["xmin"] == pytest.approx(-2)

            before_vertical = await saved(page)
            await page.get_by_role("button", name="Pan up", exact=True).click()
            up = await finished(page)
            assert float(up["requests"][0]["ymin"]) == pytest.approx(-1 - 63 * 0.035 / 4)
            assert float(up["requests"][0]["xmin"]) == pytest.approx(before_vertical["xmin"])
            assert float(up["requests"][0]["pixel_size"]) == before_vertical["pixel_size"]
            assert up["image"] != original["image"]
            await expect(page.locator("#setting-iterations")).to_have_value("150")
            await page.get_by_role("button", name="Pan down", exact=True).click()
            down = await finished(page)
            assert down["frame"] == up["frame"] + 1
            assert float(down["requests"][0]["ymin"]) == pytest.approx(-1)
            assert await saved(page) == pytest.approx(before_vertical)

            await apply(page, columns=8, rows=8)
            grid = await finished(page, 64)
            assert {(p["width"], p["height"]) for p in grid["paints"]} == {(12, 8)}
            assert {r["iterations"] for r in grid["requests"]} == {"150"}
            await expect(page.locator("#grid-preview")).to_contain_text("64 tiles")
            await page.locator("#palette-select").select_option("fire")
            palette = await finished(page, 64)
            assert {r["palette"] for r in palette["requests"]} == {"fire"}
            assert {r["iterations"] for r in palette["requests"]} == {"150"}
            await page.locator("#render-button").click()
            manual = await finished(page, 64)
            assert manual["frame"] == palette["frame"] + 1
            await apply(page)
            await poll_twice(page)
            assert await page.evaluate("uiProbe.frame") == manual["frame"]
            await apply(page, dwell_ms=200000, lost_ms=1200)
            await poll_twice(page)
            assert await page.evaluate("uiProbe.frame") == manual["frame"]
            await apply(page, dwell_ms=1)
            await page.wait_for_function("n => uiProbe.frame > n", arg=manual["frame"])
            # Stop short dwell before examining a stable completed frame.
            await apply(page, dwell_ms=300000)
            cycled = await finished(page, 64)
            assert {r["iterations"] for r in cycled["requests"]} == {"150"}
            assert {r["palette"] for r in cycled["requests"]} == {"fire"}

    asyncio.run(scenario())


def test_invalid_apply_and_navigation_boundaries(tmp_path):
    async def scenario():
        config = configuration(geometry=GatewaySettings().geometry, view=GatewaySettings().view)
        async with local_page(tmp_path, workers=0, settings=config) as (stack, page):
            await open_settings(stack, page)
            await apply(page)  # Establish saved values without changing the frame.
            baseline = await saved(page)
            frame = await page.evaluate("uiProbe.frame")
            image = await page.locator("canvas").evaluate("c => c.toDataURL()")
            for change in (
                {"columns": 17},
                {"rows": 0},
                {"columns": 1, "rows": 1},
                {"columns": 960, "rows": 540},
                {"iterations": 10001},
                {"iterations": 1.5},
                {"dwell_ms": 0},
                {"lost_ms": ""},
            ):
                await fill(page, **{key: baseline[key] for key in INPUTS})
                await apply(page, **change)
                await expect(page.locator("#settings-error")).to_be_visible()
                assert await saved(page) == baseline
                assert await page.evaluate("uiProbe.frame") == frame
                assert await page.locator("canvas").evaluate("c => c.toDataURL()") == image

            # Seed exact protocol boundaries; page navigation must never send an invalid view.
            boundary = {**baseline, "xmin": -4, "ymin": 0, "pixel_size": 1e-13}
            await page.evaluate(
                "([key, value]) => localStorage.setItem(key, JSON.stringify(value))",
                [STORAGE_KEY, boundary],
            )
            await page.reload()
            await expect(page.locator("#setting-columns")).to_be_enabled()
            await expect(page.locator("#pan-left")).to_be_disabled()
            await expect(page.locator("#zoom-in")).to_be_disabled()
            await expect(page.locator("#pan-right")).to_be_enabled()
            await expect(page.locator("#zoom-out")).to_be_disabled()
            await page.get_by_role("button", name="Pan right", exact=True).click()
            await expect(page.locator("#pan-left")).to_be_enabled()
            assert (await saved(page))["xmin"] > -4

            # Binary-exact coordinates exercise both vertical limits without rounding drift.
            for outward, inward, origin, direction in (
                ("Pan up", "Pan down", -4, 1),
                ("Pan down", "Pan up", 4 - 539 / 1024, -1),
            ):
                vertical = {**baseline, "xmin": 0, "ymin": origin, "pixel_size": 1 / 1024}
                await page.evaluate(
                    "([key, value]) => localStorage.setItem(key, JSON.stringify(value))",
                    [STORAGE_KEY, vertical],
                )
                await page.reload()
                await expect(page.locator("#setting-columns")).to_be_enabled()
                await expect(page.get_by_role("button", name=outward, exact=True)).to_be_disabled()
                await expect(page.get_by_role("button", name=inward, exact=True)).to_be_enabled()
                await page.get_by_role("button", name=inward, exact=True).click()
                await expect(page.get_by_role("button", name=outward, exact=True)).to_be_enabled()
                assert (await saved(page))["ymin"] == origin + direction * 539 / 1024 / 4

    asyncio.run(scenario())


def test_persistence_reset_and_worker_return(tmp_path):
    async def scenario():
        async with local_page(tmp_path, workers=0) as (stack, page):
            await open_settings(stack, page)
            await apply(page, columns=8, rows=8, iterations=140, dwell_ms=200000, lost_ms=1400)
            await page.get_by_role("button", name="Zoom in", exact=True).click()
            await page.get_by_role("button", name="Pan up", exact=True).click()
            accepted = await saved(page)
            await page.reload()
            await expect(page.locator("#setting-columns")).to_have_value("8")
            for key in INPUTS:
                await expect(page.locator(f"#setting-{key}")).to_have_value(str(accepted[key]))
            await expect(page.locator("#tile-overlay .tile")).to_have_count(64)
            assert await page.evaluate("uiProbe.requests.length") == 0
            await stack.start_worker(next(iter(stack.ports)))
            rendered = await finished(page, 64)
            for key in ("xmin", "ymin", "pixel_size", "iterations"):
                assert float(rendered["requests"][0][key]) == pytest.approx(accepted[key])
            await page.locator("#palette-select").select_option("fire")
            await finished(page, 64)
            await page.locator(".settings-panel > summary").click()
            await fill(page, columns=3)
            await page.get_by_role("button", name="Reset settings", exact=True).click()
            reset = await finished(page)
            assert await saved(page) is None
            restored = {
                "columns": 4,
                "rows": 4,
                "iterations": 80,
                "dwell_ms": 300000,
                "lost_ms": 600,
            }
            for key, value in restored.items():
                await expect(page.locator(f"#setting-{key}")).to_have_value(str(value))
            await expect(page.locator("#palette-select")).to_have_value("fire")
            assert {r["iterations"] for r in reset["requests"]} == {"80"}
            assert {r["pixel_size"] for r in reset["requests"]} == {"0.035"}
            await page.reload()
            await finished(page)
            await expect(page.locator("#setting-columns")).to_have_value("4")

    asyncio.run(scenario())


def test_invalid_saved_data_and_changed_raster(tmp_path):
    async def scenario():
        async with local_page(tmp_path, workers=0) as (stack, page):
            await open_settings(stack, page)
            await apply(page, columns=8)
            accepted = await saved(page)
            invalid = [
                "{",
                "null",
                "[]",
                json.dumps({**accepted, "iterations": "140"}),
                json.dumps({**accepted, "columns": True}),
                json.dumps({**accepted, "pixel_size": 1e-14}),
                json.dumps({**accepted, "xmin": 4}),
                json.dumps({"columns": 8}),
            ]
            for record in invalid:
                await page.evaluate(
                    "([key, value]) => localStorage.setItem(key, value)",
                    [STORAGE_KEY, record],
                )
                await page.reload()
                await expect(page.locator("#setting-columns")).to_have_value("4")
                await expect(page.locator("#storage-notice")).to_contain_text("invalid")
                await expect(page.locator("#tile-overlay .tile")).to_have_count(16)

            await page.evaluate(
                "([key, value]) => localStorage.setItem(key, JSON.stringify(value))",
                [STORAGE_KEY, accepted],
            )

            async def resize_config(route):
                response = await route.fetch()
                config = await response.json()
                config["geometry"].update(width=100, columns=5)
                await route.fulfill(response=response, json=config)

            await page.route("**/api/config", resize_config)
            await page.reload()
            await expect(page.locator("#setting-columns")).to_have_value("5")
            await expect(page.locator("canvas")).to_have_attribute("width", "100")
            await expect(page.locator("#tile-overlay .tile")).to_have_count(20)
            await expect(page.locator("#storage-notice")).to_contain_text("invalid")
            await stack.start_worker(next(iter(stack.ports)))
            resized = await finished(page, 20)
            assert {p["width"] for p in resized["paints"]} == {20}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["access", "write"])
def test_storage_failures_do_not_block_rendering_or_reset(tmp_path, failure):
    script = """
      const fail = () => { throw new DOMException('Test storage failure', 'SecurityError'); };
    """
    if failure == "access":
        script += "Object.defineProperty(window, 'localStorage', {get: fail});"
    else:
        script += "Storage.prototype.setItem = fail; Storage.prototype.removeItem = fail;"

    async def scenario():
        async with local_page(tmp_path, script=script) as (stack, page):
            await open_settings(stack, page)
            await finished(page)
            if failure == "access":
                await expect(page.locator("#storage-notice")).to_contain_text("unavailable")
            await apply(page, columns=8, iterations=140)
            await finished(page, 32)
            await expect(page.locator("#storage-notice")).to_contain_text("could not be saved")
            await page.get_by_role("button", name="Reset settings", exact=True).click()
            restored = await finished(page)
            assert {r["iterations"] for r in restored["requests"]} == {"80"}
            await expect(page.locator("#setting-columns")).to_have_value("4")
            await expect(page.locator("#storage-notice")).to_contain_text("could not be cleared")

    asyncio.run(scenario())


@pytest.mark.parametrize("hold", ["response", "decode"])
def test_old_work_keeps_credits_and_cannot_paint_after_grid_changes(tmp_path, hold):
    async def scenario():
        async with local_page(tmp_path, script=f"uiProbe.hold = {json.dumps(hold)};") as pair:
            stack, page = pair
            await open_settings(stack, page)
            await page.wait_for_function("uiProbe.entered")
            await apply(page, columns=8, rows=8, iterations=150)
            await page.get_by_role("button", name="Pan right", exact=True).click()
            await page.get_by_role("button", name="Zoom in", exact=True).click()
            await apply(page, columns=2, rows=2)
            await poll_twice(page)
            frame = await page.evaluate("uiProbe.frame")
            assert frame == 5
            await expect(page.locator("#tile-overlay .tile")).to_have_count(4)
            await expect(page.locator("#render-progress")).to_have_attribute("max", "4")
            await expect(page.locator("#render-progress")).to_have_attribute("value", "0")
            await expect(page.locator("#active-count")).to_have_text("1 / 1")
            assert await page.evaluate("uiProbe.requests.length") == 1
            assert await page.evaluate("uiProbe.paints.length") == 0
            await page.evaluate("uiProbe.release()")
            rendered = await finished(page, 4)
            assert rendered["frame"] == frame
            assert {(p["width"], p["height"]) for p in rendered["paints"]} == {(48, 32)}
            assert {r["iterations"] for r in rendered["requests"]} == {"150"}
            assert len(rendered["requests"]) == 4
            assert await page.evaluate("uiProbe.paints.length") == 4
            if hold == "decode":
                assert await page.evaluate("uiProbe.closed") == 1

    asyncio.run(scenario())


def test_expired_decoder_barrier_survives_settings_changes(tmp_path):
    async def scenario():
        config = configuration(
            connect_timeout=0.25,
            upstream_timeout=0.5,
            timing={**configuration().timing, "render_timeout_ms": 1000},
        )
        async with local_page(tmp_path, settings=config, script="uiProbe.hold = 'decode';") as pair:
            stack, page = pair
            await open_settings(stack, page)
            await page.wait_for_function("uiProbe.entered")
            await expect(page.locator("#status-text")).to_contain_text("Image decoding timed out")
            await apply(page, columns=2, rows=2)
            await page.get_by_role("button", name="Zoom in", exact=True).click()
            await poll_twice(page)
            await expect(page.locator("#status-text")).to_contain_text("decoder to settle")
            await expect(page.locator("#active-count")).to_have_text("0 / 1")
            assert await page.evaluate("uiProbe.requests.length") == 1
            assert await page.evaluate("uiProbe.paints.length") == 0
            await page.evaluate("uiProbe.release()")
            await finished(page, 4)
            assert await page.evaluate("uiProbe.closed") == 1
            assert await page.evaluate("uiProbe.paints.length") == 4

    asyncio.run(scenario())


def test_lost_duration_change_applies_only_to_new_losses(tmp_path):
    async def scenario():
        config = configuration(timing={**configuration().timing, "lost_ms": 300000})
        async with local_page(tmp_path, settings=config) as (stack, page):
            failures = 1

            async def fail_once(route):
                nonlocal failures
                if failures:
                    failures -= 1
                    await route.fulfill(
                        status=502,
                        json={
                            "code": "upstream_failure",
                            "scope": "gateway",
                            "message": "Test loss",
                        },
                    )
                else:
                    await route.continue_()

            await page.route("**/api/render/**", fail_once)
            await open_settings(stack, page)
            await page.wait_for_function("document.getElementById('render-progress').value === 15")
            lost = page.locator('#tile-overlay .tile[data-state="lost"]')
            await expect(lost).to_have_count(1)
            frame = await page.evaluate("uiProbe.frame")
            await apply(page, lost_ms=1)
            await poll_twice(page)
            assert await page.evaluate("uiProbe.frame") == frame
            await expect(lost).to_have_count(1)
            failures = 1
            await page.locator("#render-button").click()
            await finished(page)
            assert failures == 0
            await expect(lost).to_have_count(0)

    asyncio.run(scenario())
