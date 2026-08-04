import os
import asyncio
from playwright.async_api import BrowserContext, Page
from crawler.config import log

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900, "is_mobile": False},
    "mobile": {"width": 390, "height": 844, "is_mobile": True, "device_scale_factor": 3}
}
USER_AGENTS = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
}

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
"""

async def make_context(pw, viewport_name: str) -> BrowserContext:
    vp = VIEWPORTS[viewport_name]
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions", "--no-first-run", "--mute-audio",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--font-render-hinting=none",
        ]
    )
    ctx_args = dict(
        viewport={"width": vp["width"], "height": vp["height"]},
        device_scale_factor=vp.get("device_scale_factor", 1),
        user_agent=USER_AGENTS[viewport_name],
        ignore_https_errors=True,
        java_script_enabled=True,
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    if vp.get("is_mobile"):
        ctx_args["is_mobile"] = True
        ctx_args["has_touch"] = True
    context = await browser.new_context(**ctx_args)
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    return browser, context

_OVERLAY_JS = """
() => {
    let dismissed = 0;
    const closeSelectors = [
        'button[aria-label*="Close" i]', 'button[aria-label*="close" i]',
        'button[aria-label*="Dismiss" i]', 'button[aria-label*="dismiss" i]',
        '[data-dismiss="modal"]', '[data-role="close"]',
        '.modal-close', '.popup-close', '.close-modal', '.close-popup',
        '#onetrust-accept-btn-handler', '.cc-btn.cc-dismiss', '.cc-dismiss',
        '#cookie-accept', '.accept-cookies', '[id*="cookie-accept"]',
        'button[id*="accept"]', 'button[class*="accept"]',
        'button:has-text("Not now")', 'button:has-text("Maybe later")',
        'button:has-text("Skip")', 'button:has-text("No thanks")',
    ];

    function deepQuery(root, sel) {
        let nodes = [];
        try { nodes = Array.from(root.querySelectorAll(sel)); } catch(e) {}
        try {
            root.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) nodes = nodes.concat(deepQuery(el.shadowRoot, sel));
            });
        } catch(e) {}
        return nodes;
    }

    closeSelectors.forEach(sel => {
        deepQuery(document, sel).forEach(btn => {
            try {
                if ((btn.offsetWidth > 0 || btn.offsetHeight > 0)) {
                    btn.click();
                    dismissed++;
                }
            } catch(e) {}
        });
    });

    const vw = window.innerWidth, vh = window.innerHeight;
    document.querySelectorAll('div,section,aside,dialog,form,iframe,[role="dialog"],[role="alertdialog"]').forEach(el => {
        try {
            const s = window.getComputedStyle(el);
            const z = parseInt(s.zIndex || '0');
            if ((s.position === 'fixed' || s.position === 'absolute') && z > 100) {
                const r = el.getBoundingClientRect();
                const area = r.width * r.height;
                if (area > vw * vh * 0.40 &&
                    !['BODY','MAIN','SECTION','ARTICLE','NAV','HEADER','FOOTER'].includes(el.tagName)) {
                    el.style.setProperty('display', 'none', 'important');
                    dismissed++;
                }
            }
        } catch(e) {}
    });

    document.body.style.setProperty('overflow', 'auto', 'important');
    document.documentElement.style.setProperty('overflow', 'auto', 'important');
    document.body.style.setProperty('position', 'static', 'important');

    return dismissed;
}
"""

async def dismiss_all_overlays(page: Page) -> int:
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
    except Exception:
        pass
    try:
        count = await page.evaluate(_OVERLAY_JS)
        if count > 0:
            log(f"Dismissed {count} overlay(s)", "INFO")
        return count
    except Exception:
        return 0

async def settle_page(page: Page, full_scroll: bool = True):
    for state in ("networkidle", "domcontentloaded", "load"):
        try:
            await page.wait_for_load_state(state, timeout=4000)
            break
        except Exception:
            continue

    if full_scroll:
        try:
            await page.evaluate("""
            async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const total = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                const step = Math.min(600, Math.ceil(total / 10));
                for (let y = 0; y < total; y += step) {
                    window.scrollTo(0, y);
                    await delay(80);
                }
                window.scrollTo(0, 0);
                await delay(150);
            }
            """)
        except Exception:
            pass

    await dismiss_all_overlays(page)
    await asyncio.sleep(2)

async def take_screenshot(page: Page, path: str, full_page: bool = True):
    await settle_page(page, full_scroll=full_page)
    try:
        await page.screenshot(path=path, full_page=full_page, type="png")
        log(f"Screenshot saved: {os.path.basename(path)}", "SNAP")
    except Exception as e:
        log(f"Screenshot failed ({e}), retrying viewport-only", "WARN")
        try:
            await page.screenshot(path=path, full_page=False, type="png")
        except Exception:
            pass
