#!/usr/bin/env python3
"""
crawl_map_ai.py - Enterprise Web Flow Mapper v2.0
=====================================================
Agentic dual-viewport website mapper. Uses Playwright to navigate any
website, captures full-page screenshots at both desktop (1440×900) and
mobile (390×844) viewports, bypasses all popups/overlays/ads, and
produces a professional Excalidraw user flow whiteboard with side-by-side
desktop/mobile comparison cards, responsive diff badges, and flow arrows.

AI reasoning via OpenCode CLI determines the next navigation action.
Falls back to deterministic nav-first heuristic when AI is unavailable.

Usage:
    python crawl_map_ai.py --url https://example.com --goal "Map all sections"
    python crawl_map_ai.py --url https://example.com --goal "..." --desktop-only
    python crawl_map_ai.py --url https://example.com --goal "..." --mobile-only
    python crawl_map_ai.py --url https://example.com --goal "..." --no-ai
"""

import os
import sys
import json
import uuid
import time
import base64
import asyncio
import argparse
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag

# ─────────────────────────────────────────────────────────────────────────────
# Auto-discover & activate project .venv
# ─────────────────────────────────────────────────────────────────────────────
_repo_dir = os.path.dirname(os.path.abspath(__file__))
for _venv in [os.path.join(_repo_dir, ".venv"), os.path.join(os.getcwd(), ".venv")]:
    _bin = os.path.join(_venv, "Scripts" if sys.platform == "win32" else "bin")
    if os.path.exists(_bin):
        if _bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
        _sp = (os.path.join(_venv, "Lib", "site-packages") if sys.platform == "win32"
               else os.path.join(_venv, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"))
        if os.path.exists(_sp) and _sp not in sys.path:
            sys.path.insert(0, _sp)
        break

from playwright.async_api import async_playwright, Page, BrowserContext

# ─────────────────────────────────────────────────────────────────────────────
# Constants & Configuration
# ─────────────────────────────────────────────────────────────────────────────
VERSION = "2.0.0"
DEFAULT_MODEL = "google/gemini-flash-1.5-8b"

MAX_STEPS       = 16          # Max navigation steps per viewport
PAGE_SETTLE_MS  = 800         # Base page settle delay
AI_TIMEOUT_S    = 15.0        # OpenCode response timeout
NAV_TIMEOUT_MS  = 30_000      # Playwright navigation timeout
SCROLL_PAUSE_MS = 120         # Pause between scroll chunks for lazy loads
MAX_NAV_LINKS   = 30          # Max nav links to extract upfront

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900,  "device_scale_factor": 1.5},
    "mobile":  {"width": 390,  "height": 844,  "device_scale_factor": 3.0,
                "is_mobile": True, "has_touch": True},
}

USER_AGENTS = {
    "desktop": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"),
    "mobile":  ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4 Mobile/15E148 Safari/604.1"),
}

# Excalidraw colour palette
PALETTE = {
    "bg":         "#0d1117",
    "surface":    "#161b22",
    "border":     "#30363d",
    "accent":     "#2f81f7",
    "accent2":    "#7c3aed",   # mobile accent
    "success":    "#238636",
    "danger":     "#da3633",
    "warn":       "#d29922",
    "text":       "#e6edf3",
    "muted":      "#8b949e",
    "arrow_d":    "#2f81f7",   # desktop arrow
    "arrow_m":    "#7c3aed",   # mobile arrow
    "card_bg":    "#161b22",
    "card_bd":    "#30363d",
    "diff_badge": "#f0883e",
}

# ─────────────────────────────────────────────────────────────────────────────
# State trackers (module-level, reset per run)
# ─────────────────────────────────────────────────────────────────────────────
_failed_selectors: set = set()
_visited_urls:     set = set()
_nav_links_queue:  list = []

# ─────────────────────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log(msg: str, level: str = "INFO"):
    icons = {"INFO": "·", "STEP": "▶", "SHOT": "📸", "AI": "🤖",
             "NAV": "🔗", "GATE": "🚧", "DONE": "✅", "WARN": "⚠️", "ERR": "❌"}
    print(f"[{_ts()}] [{icons.get(level, '·')}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# OpenCode LLM Bridge
# ─────────────────────────────────────────────────────────────────────────────
_OPENCODE_AVAILABLE: bool | None = None

async def check_opencode() -> bool:
    global _OPENCODE_AVAILABLE
    if _OPENCODE_AVAILABLE is not None:
        return _OPENCODE_AVAILABLE
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        _OPENCODE_AVAILABLE = proc.returncode == 0
    except Exception:
        _OPENCODE_AVAILABLE = False
    return _OPENCODE_AVAILABLE


async def ask_opencode(prompt: str, model: str) -> str:
    if not await check_opencode():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", "run", "--format", "json", "-m", model, "--auto",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout_data, _ = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=AI_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ""

        parts = []
        for line in stdout_data.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                t = ev.get("type", "")
                if t == "text":
                    parts.append(ev.get("text") or (ev.get("part") or {}).get("text") or "")
                elif t == "assistant":
                    for blk in ev.get("content") or []:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            parts.append(blk.get("text", ""))
            except Exception:
                pass
        return "".join(parts)
    except Exception:
        return ""


def extract_json_block(text: str) -> str:
    text = text.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Enterprise-Grade Stealth Context Builder
# ─────────────────────────────────────────────────────────────────────────────
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
Object.defineProperty(navigator, 'permissions', {
  get: () => ({ query: (p) => Promise.resolve({ state: 'granted', onchange: null }) })
});
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


# ─────────────────────────────────────────────────────────────────────────────
# Comprehensive Overlay & Popup Bypass Engine
# ─────────────────────────────────────────────────────────────────────────────
_OVERLAY_JS = """
() => {
    let dismissed = 0;

    // 1. Known close button selectors (cookie, modal, chat, newsletter, geo)
    const closeSelectors = [
        // Generic close buttons
        'button[aria-label*="Close" i]', 'button[aria-label*="close" i]',
        'button[aria-label*="Dismiss" i]', 'button[aria-label*="dismiss" i]',
        '[data-dismiss="modal"]', '[data-role="close"]',
        '.modal-close', '.popup-close', '.close-modal', '.close-popup',
        '.dialog-close', '[class*="modal__close"]', '[class*="close-btn"]',

        // Cookie consent
        '#onetrust-accept-btn-handler', '.cc-btn.cc-dismiss', '.cc-dismiss',
        '#cookie-accept', '.accept-cookies', '[id*="cookie-accept"]',
        '[class*="cookie-accept"]', '[class*="gdpr-accept"]',
        'button[id*="accept"]', 'button[class*="accept"]',

        // Chat widgets (Intercom, Drift, Zendesk, HubSpot, Crisp, Tidio)
        '.intercom-launcher-close-button', '[class*="intercom-close"]',
        '#drift-close', '.drift-close', '[class*="drift-close"]',
        '.zd-close-button', '#launcher', '.zsiq_closebtn',
        '.HubSpot .close', '[data-testid="CloseButton"]',
        '.crisp-close', '.tidio-close',

        // Newsletter / email capture modals
        '.klaviyo-close', '[class*="klaviyo-close"]',
        '.optinmonster-close', '.popup-overlay-close',
        '[class*="email-popup-close"]', '[class*="newsletter-close"]',

        // Geo / location
        '[class*="location-modal"] button', '[class*="pincode"] button',
        '.geo-modal-close', '[class*="city-selector-close"]',

        // App banners
        '.smartbanner-close', '[class*="app-banner-close"]',

        // General "not now / skip / maybe later"
        'button:has-text("Not now")', 'button:has-text("Maybe later")',
        'button:has-text("Skip")', 'button:has-text("No thanks")',
        'button:has-text("Continue as Guest")', 'button:has-text("Continue without")',
        'a:has-text("Skip")', 'a:has-text("No, thanks")',
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

    // 2. Heuristic: remove high-z fixed/absolute overlays > 40% viewport
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

    // 3. Restore scroll (overlays often lock body scroll)
    document.body.style.setProperty('overflow', 'auto', 'important');
    document.documentElement.style.setProperty('overflow', 'auto', 'important');
    document.body.style.setProperty('position', 'static', 'important');

    return dismissed;
}
"""

async def dismiss_all_overlays(page: Page) -> int:
    """Enterprise overlay bypass: keyboard escape + comprehensive JS dismisser."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)
    except Exception:
        pass
    try:
        count = await page.evaluate(_OVERLAY_JS)
        if count > 0:
            log(f"Dismissed {count} overlay(s)/popup(s)", "INFO")
        return count
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Page Settlement Engine (Firecrawl-grade)
# ─────────────────────────────────────────────────────────────────────────────
async def settle_page(page: Page, full_scroll: bool = True):
    """
    1. Wait for networkidle / DOMContentLoaded
    2. Full-page scroll pass to trigger lazy loads
    3. Wait for skeleton/shimmer loaders to unmount
    4. Final dismiss pass
    """
    # 1. Network settle
    for state in ("networkidle", "domcontentloaded", "load"):
        try:
            await page.wait_for_load_state(state, timeout=4000)
            break
        except Exception:
            continue

    # 2. Lazy load trigger via full-height scroll
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

    # 3. Wait for skeleton loaders
    try:
        await page.evaluate("""
        async () => {
            const start = Date.now();
            const selectors = '.skeleton,[class*="shimmer" i],[class*="loading" i],[aria-busy="true"],[class*="placeholder" i]';
            while (Date.now() - start < 1200) {
                if (document.querySelectorAll(selectors).length === 0) break;
                await new Promise(r => setTimeout(r, 100));
            }
        }
        """)
    except Exception:
        pass

    # 4. Final overlay sweep
    await dismiss_all_overlays(page)
    await asyncio.sleep(PAGE_SETTLE_MS / 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Full-Page Screenshot (pristine, post-settle)
# ─────────────────────────────────────────────────────────────────────────────
async def take_screenshot(page: Page, path: str, full_page: bool = True):
    """Take a pristine screenshot after full page settlement."""
    await settle_page(page, full_scroll=full_page)
    try:
        await page.screenshot(path=path, full_page=full_page, type="png")
        log(f"Screenshot saved: {os.path.basename(path)}", "SHOT")
    except Exception as e:
        log(f"Screenshot failed ({e}), retrying viewport-only", "WARN")
        try:
            await page.screenshot(path=path, full_page=False, type="png")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Nav Link Extractor (structured sitemap upfront)
# ─────────────────────────────────────────────────────────────────────────────
_NAV_EXTRACT_JS = """
(baseOrigin) => {
    const seen = new Set();
    const links = [];

    // Priority: nav elements, header links, menu items
    const containers = [
        ...document.querySelectorAll('nav, header, [role="navigation"], [class*="navbar" i], [class*="menu" i], [class*="header" i]')
    ];

    // Fallback: all anchors
    if (containers.length === 0) containers.push(document.body);

    containers.forEach(container => {
        container.querySelectorAll('a[href]').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (!href || href.startsWith('javascript:') || href.startsWith('#') || href.startsWith('mailto:') || href.startsWith('tel:')) return;

            let full = '';
            try { full = new URL(href, window.location.href).href; } catch(e) { return; }

            const origin = new URL(full).origin;
            if (origin !== baseOrigin && origin !== window.location.origin) return;

            const path = new URL(full).pathname.split('?')[0].replace(/\/$/, '') || '/';

            // Skip utility pages
            const skip = ['/login', '/signin', '/signup', '/register', '/cart', '/checkout',
                          '/auth', '/account', '/legal', '/terms', '/privacy', '/help', '/support',
                          '/sitemap', '/404', '/500', '/feed', '/rss'];
            if (skip.some(s => path.toLowerCase().includes(s))) return;

            const key = path.toLowerCase();
            if (seen.has(key)) return;
            seen.add(key);

            const text = (a.innerText || a.textContent || '').trim().slice(0, 60);
            if (!text) return;

            links.push({ href: full, text, path });
        });
    });

    return links.slice(0, 30);
}
"""

async def extract_nav_links(page: Page, base_url: str) -> list[dict]:
    origin = urlparse(base_url).scheme + "://" + urlparse(base_url).netloc
    try:
        links = await page.evaluate(_NAV_EXTRACT_JS, origin)
        log(f"Extracted {len(links)} nav links from page structure", "NAV")
        return links
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Interactive Element Extractor (for AI reasoning)
# ─────────────────────────────────────────────────────────────────────────────
_ELEMENTS_JS = """
() => {
    const items = [];
    const els = document.querySelectorAll(
        'a, button, input, textarea, select, [role="button"], [role="link"], [role="menuitem"], [tabindex="0"]'
    );
    let idx = 0;
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.1) continue;

        const tag = el.tagName.toLowerCase();
        const id = el.id || '';
        const name = el.getAttribute('name') || '';
        const href = el.getAttribute('href') || '';
        const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
        const placeholder = el.getAttribute('placeholder') || '';
        const type = el.getAttribute('type') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';

        let selector = tag;
        if (id) selector += '#' + CSS.escape(id);
        else if (name) selector += `[name="${name}"]`;
        else if (tag === 'a' && href && !href.startsWith('javascript:')) {
            const clean = href.split('?')[0];
            selector += `[href*="${clean}"]`;
        }

        items.push({ id: idx++, tag, type, name, placeholder, text, href, selector, ariaLabel });
        if (items.length >= 80) break;
    }
    return items;
}
"""

async def extract_elements(page: Page) -> list[dict]:
    await dismiss_all_overlays(page)
    try:
        return await page.evaluate(_ELEMENTS_JS)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Auth / Paywall Gate Detector
# ─────────────────────────────────────────────────────────────────────────────
async def detect_gate(page: Page) -> tuple[bool, str]:
    await dismiss_all_overlays(page)
    url = page.url.lower()

    auth_url = ("/login", "/signin", "/sign-in", "/signup", "/sign-up", "/register", "/auth/", "/account/login")
    if any(p in url for p in auth_url):
        return True, "Authentication Gate"

    pay_url = ("/paywall", "/checkout", "/payment", "/subscribe", "/billing")
    if any(p in url for p in pay_url):
        return True, "Payment Gate"

    try:
        if await page.locator("input[type='password']").count() > 0:
            return True, "Authentication Gate (Password Field)"
    except Exception:
        pass

    try:
        captcha = await page.evaluate("""
        () => {
            const t = (document.title || '').toLowerCase();
            const b = (document.body?.innerText || '').toLowerCase();
            const phrases = ['just a moment', 'verify you are human', 'cloudflare ray id',
                             'completing the captcha', 'security check'];
            return phrases.some(p => t.includes(p) || b.includes(p)) ||
                   !!document.querySelector('.g-recaptcha,.h-captcha,#challenge-stage,iframe[src*="challenges.cloudflare.com"]');
        }
        """)
        if captcha:
            return True, "Bot Verification Challenge"
    except Exception:
        pass

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Heuristic Fallback Planner (nav-first strategy)
# ─────────────────────────────────────────────────────────────────────────────
async def heuristic_next_action(elements: list[dict], page: Page, goal: str, step: int, nav_queue: list) -> dict:
    """Nav-first: drain the nav queue before exploring heuristically."""
    goal_lower = goal.lower()
    elements = [e for e in elements if e.get("selector") not in _failed_selectors]

    # Priority 1: drain pre-extracted nav queue
    while nav_queue:
        link = nav_queue[0]
        clean = link["href"].split("?")[0].rstrip("/")
        if clean not in _visited_urls:
            nav_queue.pop(0)
            return {
                "thought": f"Nav-first: Visiting '{link['text']}' → {link['path']}",
                "action": "navigate",
                "value": link["href"],
            }
        nav_queue.pop(0)

    # Priority 2: unvisited anchor on current page
    for el in elements:
        href = el.get("href", "")
        if el["tag"] == "a" and href and not href.startswith("javascript:") and not href.startswith("#"):
            clean = href.split("?")[0].rstrip("/")
            if clean not in _visited_urls and not any(p in href.lower() for p in
                    ["/login", "/signin", "/signup", "/cart", "/checkout", "/help", "/legal", "/terms", "/privacy"]):
                return {
                    "thought": f"Exploring unvisited link: {el['text'] or clean}",
                    "action": "click",
                    "id": el["id"],
                }

    return {"action": "done", "thought": "Navigation complete — all major sections mapped."}


# ─────────────────────────────────────────────────────────────────────────────
# Responsive Diff Detector
# ─────────────────────────────────────────────────────────────────────────────
def compute_image_hash(path: str) -> str | None:
    """Simple perceptual hash (file hash as proxy — good enough for diff badge)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:16]
    except Exception:
        return None


def has_responsive_diff(desktop_path: str | None, mobile_path: str | None) -> bool:
    """Returns True if desktop and mobile screenshots are visually different (by hash)."""
    h1 = compute_image_hash(desktop_path)
    h2 = compute_image_hash(mobile_path)
    if h1 is None or h2 is None:
        return False
    return h1 != h2


# ─────────────────────────────────────────────────────────────────────────────
# Excalidraw Whiteboard Builder (dual-viewport, professional layout)
# ─────────────────────────────────────────────────────────────────────────────
def _uid() -> str:
    return uuid.uuid4().hex


def _rect(x, y, w, h, bg=PALETTE["card_bg"], border=PALETTE["card_bd"],
          stroke_w=1, radius=10, opacity=100) -> dict:
    return {
        "id": _uid(), "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h,
        "fillStyle": "solid", "backgroundColor": bg,
        "strokeColor": border, "strokeWidth": stroke_w,
        "roughness": 0, "roundness": {"type": 3, "value": radius},
        "opacity": opacity, "angle": 0,
        "groupIds": [], "frameId": None, "isDeleted": False,
        "boundElements": [], "updated": 1700000000, "link": None,
        "locked": False,
    }


def _text(x, y, text, size=12, color=PALETTE["text"], align="left", bold=False) -> dict:
    return {
        "id": _uid(), "type": "text",
        "x": x, "y": y, "width": max(len(text) * size * 0.6, 40), "height": size + 6,
        "text": text, "fontSize": size,
        "fontFamily": 3,   # monospace — clean for URLs
        "textAlign": align, "verticalAlign": "middle",
        "strokeColor": color, "backgroundColor": "transparent",
        "roughness": 0, "opacity": 100, "angle": 0,
        "groupIds": [], "frameId": None, "isDeleted": False,
        "boundElements": [], "updated": 1700000000, "link": None, "locked": False,
    }


def _arrow(x1, y1, x2, y2, color=PALETTE["arrow_d"], dashed=False) -> dict:
    w = x2 - x1
    h = y2 - y1
    return {
        "id": _uid(), "type": "arrow",
        "x": x1, "y": y1, "width": abs(w), "height": abs(h),
        "points": [[0, 0], [w, h]],
        "startArrowhead": None, "endArrowhead": "arrow",
        "strokeColor": color, "strokeWidth": 2,
        "strokeStyle": "dashed" if dashed else "solid",
        "roughness": 0, "opacity": 80, "angle": 0,
        "fillStyle": "solid", "backgroundColor": "transparent",
        "groupIds": [], "frameId": None, "isDeleted": False,
        "boundElements": [], "updated": 1700000000, "link": None, "locked": False,
    }


def _badge(x, y, label, color=PALETTE["accent"]) -> list[dict]:
    w = max(len(label) * 7 + 16, 50)
    return [
        {**_rect(x, y, w, 22, bg=color, border="transparent", radius=4), "strokeWidth": 0},
        {**_text(x + w // 2, y + 11, label, size=9, color="#ffffff", align="center")},
    ]


def _embed_image(path: str, x: int, y: int, w: int, h: int) -> tuple[list[dict], dict | None]:
    """Returns (elements, file_entry) for Excalidraw image embedding."""
    if not path or not os.path.exists(path):
        # Placeholder rectangle when screenshot missing
        return [_rect(x, y, w, h, bg="#1c2128", border=PALETTE["border"])], None
    fid = f"img_{_uid()[:12]}"
    try:
        with open(path, "rb") as f:
            data_url = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        return [_rect(x, y, w, h, bg="#1c2128", border=PALETTE["border"])], None

    el = {
        "id": _uid(), "type": "image",
        "x": x, "y": y, "width": w, "height": h,
        "fileId": fid, "status": "saved",
        "roughness": 0, "opacity": 100, "angle": 0,
        "groupIds": [], "frameId": None, "isDeleted": False,
        "boundElements": [], "updated": 1700000000, "link": None, "locked": False,
        "scale": [1, 1],
    }
    file_entry = {"id": fid, "mimeType": "image/png", "dataURL": data_url, "created": 1700000000}
    return [el], file_entry


def build_excalidraw(steps: list[dict], start_url: str, goal: str,
                     viewports_used: list[str]) -> dict:
    """
    Build a professional dual-viewport Excalidraw whiteboard.

    Layout:
      ┌─── HEADER ────────────────────────────────────┐
      │  Flow: domain  │  Goal  │  Stats  │  Legend   │
      └───────────────────────────────────────────────┘

      ┌─ DESKTOP ROW ─┐  ┌─ DESKTOP ROW ─┐  ...
      │  screenshot   │→ │  screenshot   │  ...
      └───────────────┘  └───────────────┘

      ┌─ MOBILE ROW ──┐  ┌─ MOBILE ROW ──┐  ...
      │  screenshot   │→ │  screenshot   │  ...
      └───────────────┘  └───────────────┘
    """
    elements: list[dict] = []
    files: dict = {}

    # ── Card geometry ────────────────────────────────────────────────────────
    D_W, D_H = 300, 520   # desktop card: width, screenshot height
    M_W, M_H = 180, 380   # mobile card: width, screenshot height
    CARD_PAD  = 24         # bottom text area per card
    D_CARD_H  = D_H + CARD_PAD + 64   # total desktop card height
    M_CARD_H  = M_H + CARD_PAD + 64   # total mobile card height
    GAP_X     = 60         # horizontal gap between cards
    ROW_GAP   = 80         # vertical gap between desktop and mobile rows
    HEADER_H  = 140
    HEADER_Y  = -HEADER_H - 30

    n = len(steps)
    total_w = n * (D_W + GAP_X) - GAP_X
    start_x = -(total_w // 2)

    domain = urlparse(start_url).netloc
    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dual = "desktop" in viewports_used and "mobile" in viewports_used

    # ── HEADER ───────────────────────────────────────────────────────────────
    hx = start_x
    hw = total_w if total_w > 900 else 900
    elements.append(_rect(hx, HEADER_Y, hw, HEADER_H,
                          bg=PALETTE["surface"], border=PALETTE["accent"], stroke_w=2, radius=12))
    elements.append(_text(hx + 24, HEADER_Y + 20, f"🗺  User Flow Map — {domain}", 22, PALETTE["text"]))
    elements.append(_text(hx + 24, HEADER_Y + 52, f"Goal: {goal[:120]}", 12, PALETTE["muted"]))
    elements.append(_text(hx + 24, HEADER_Y + 74, f"Pages: {n}  ·  Generated: {ts_str}", 10, PALETTE["muted"]))
    elements.append(_text(hx + 24, HEADER_Y + 94, f"Viewports: {', '.join(viewports_used)}", 10, PALETTE["muted"]))

    # Legend
    lx = hx + hw - 220
    elements.extend(_badge(lx,       HEADER_Y + 20, "● Desktop 1440px", PALETTE["accent"]))
    if dual:
        elements.extend(_badge(lx,   HEADER_Y + 50, "● Mobile 390px",   PALETTE["accent2"]))
    elements.extend(_badge(lx,       HEADER_Y + 80, "🚧 Auth/Gate",      PALETTE["danger"]))
    elements.extend(_badge(lx + 130, HEADER_Y + 80, "⚡ Diff",           PALETTE["diff_badge"]))

    # ── CARDS ────────────────────────────────────────────────────────────────
    desktop_y = 0
    mobile_y  = D_CARD_H + ROW_GAP

    for idx, step in enumerate(steps):
        cx = start_x + idx * (D_W + GAP_X)
        is_gate = step.get("auth_detected", False)
        short_url = "/" + "/".join(urlparse(step["url"]).path.strip("/").split("/")[:3]) or "/"
        step_label = f"Step {step['step']}"
        page_name = step.get("name", short_url)[:40]

        d_shot = step.get("screenshot_desktop")
        m_shot = step.get("screenshot_mobile")
        diff   = has_responsive_diff(d_shot, m_shot)

        # ── Desktop Card ─────────────────────────────────────────────────────
        if "desktop" in viewports_used:
            dy = desktop_y
            # Card background
            card_border = PALETTE["danger"] if is_gate else PALETTE["border"]
            elements.append(_rect(cx, dy, D_W, D_CARD_H, border=card_border, stroke_w=2 if is_gate else 1))

            # Screenshot
            imgs, fentry = _embed_image(d_shot, cx + 6, dy + 6, D_W - 12, D_H)
            elements.extend(imgs)
            if fentry:
                files[fentry["id"]] = fentry

            # Step badge
            badge_color = PALETTE["danger"] if is_gate else PALETTE["accent"]
            elements.extend(_badge(cx + 10, dy + 10, step_label, badge_color))

            # Responsive diff badge
            if diff:
                elements.extend(_badge(cx + D_W - 60, dy + 10, "⚡ diff", PALETTE["diff_badge"]))

            # Labels
            elements.append(_text(cx + D_W // 2, dy + D_H + 14, short_url[:38], 9, PALETTE["muted"], "center"))
            elements.append(_text(cx + D_W // 2, dy + D_H + 32, page_name,      12, PALETTE["text"],  "center"))

            if is_gate:
                elements.append(_text(cx + D_W // 2, dy + D_H + 52, "🚧 " + step.get("gate_type", "Gate"), 10, PALETTE["danger"], "center"))

            # Arrow to next desktop card
            if idx < n - 1:
                elements.append(_arrow(
                    cx + D_W, dy + D_CARD_H // 2,
                    cx + D_W + GAP_X, dy + D_CARD_H // 2,
                    color=PALETTE["arrow_d"]
                ))

        # ── Mobile Card ───────────────────────────────────────────────────────
        if "mobile" in viewports_used and dual:
            # Centre mobile card under desktop card
            mx = cx + (D_W - M_W) // 2
            my = mobile_y

            card_border = PALETTE["danger"] if is_gate else PALETTE["border"]
            elements.append(_rect(mx, my, M_W, M_CARD_H, border=card_border, stroke_w=2 if is_gate else 1))

            imgs_m, fentry_m = _embed_image(m_shot, mx + 4, my + 4, M_W - 8, M_H)
            elements.extend(imgs_m)
            if fentry_m:
                files[fentry_m["id"]] = fentry_m

            elements.extend(_badge(mx + 6, my + 6, step_label, PALETTE["accent2"] if not is_gate else PALETTE["danger"]))
            elements.append(_text(mx + M_W // 2, my + M_H + 14, short_url[:22], 8, PALETTE["muted"], "center"))
            elements.append(_text(mx + M_W // 2, my + M_H + 30, page_name[:22], 10, PALETTE["text"],  "center"))

            if idx < n - 1:
                next_mx = start_x + (idx + 1) * (D_W + GAP_X) + (D_W - M_W) // 2
                elements.append(_arrow(
                    mx + M_W, my + M_CARD_H // 2,
                    next_mx, my + M_CARD_H // 2,
                    color=PALETTE["arrow_m"]
                ))

            # Vertical connector: desktop ↕ mobile
            if "desktop" in viewports_used:
                elements.append(_arrow(
                    cx + D_W // 2, desktop_y + D_CARD_H,
                    mx + M_W // 2, my,
                    color=PALETTE["muted"], dashed=True
                ))

    return {
        "type": "excalidraw",
        "version": 2,
        "source": f"https://open-design.ai/userflow",
        "elements": elements,
        "appState": {
            "name": f"User Flow: {domain}",
            "viewBackgroundColor": PALETTE["bg"],
            "zoom": {"value": 0.5},
            "scrollX": 0, "scrollY": 0,
            "currentItemFontFamily": 3,
            "gridSize": None,
        },
        "files": files,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Single-Viewport Crawl Run
# ─────────────────────────────────────────────────────────────────────────────
async def crawl_viewport(
    pw,
    start_url: str,
    goal: str,
    output_dir: str,
    model: str,
    viewport_name: str,
    full_page: bool,
    use_ai: bool,
    shared_nav_queue: list,
) -> list[dict]:
    """
    Crawl a website with one viewport config. Returns list of step dicts.
    Shares the nav_queue with the caller so both viewports hit same pages.
    """
    log(f"{'─'*60}", "INFO")
    log(f"Starting {viewport_name.upper()} crawl ({VIEWPORTS[viewport_name]['width']}×{VIEWPORTS[viewport_name]['height']})", "STEP")
    t0 = time.monotonic()

    flow_steps: list[dict] = []
    current_url = start_url.strip()
    if not current_url.startswith(("http://", "https://")):
        current_url = "https://" + current_url

    nav_queue = list(shared_nav_queue)  # local copy for this viewport
    local_visited: set = set()

    browser, context = await make_context(pw, viewport_name)
    page = await context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)

    try:
        # ── Initial navigation ────────────────────────────────────────────────
        step = 1
        log(f"[{step}] Navigating to {current_url}", "NAV")
        try:
            await page.goto(current_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            log(f"Initial nav warning: {e}", "WARN")
            try:
                await page.goto(current_url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            except Exception:
                pass

        await settle_page(page, full_scroll=full_page)

        # Extract nav links on homepage (only once, first viewport)
        if not shared_nav_queue:
            nav_links = await extract_nav_links(page, current_url)
            shared_nav_queue.extend(nav_links)
            nav_queue = list(shared_nav_queue)
            log(f"Seeded nav queue with {len(nav_queue)} links", "NAV")

        # Screenshot step 1
        shot_path = os.path.join(output_dir, f"{viewport_name}_step_{step:02d}_initial.png")
        await take_screenshot(page, shot_path, full_page)
        local_visited.add(page.url.split("?")[0].rstrip("/"))

        flow_steps.append({
            "step": step, "name": "Homepage",
            "url": page.url,
            f"screenshot_{viewport_name}": shot_path,
        })

        # ── Agent loop ─────────────────────────────────────────────────────────
        while step < MAX_STEPS:
            is_gate, gate_type = await detect_gate(page)
            if is_gate:
                log(f"Gate detected: {gate_type} at {page.url}", "GATE")
                gate_path = os.path.join(output_dir, f"{viewport_name}_step_{step+1:02d}_gate.png")
                await take_screenshot(page, gate_path, full_page)
                flow_steps.append({
                    "step": step + 1, "name": gate_type, "gate_type": gate_type,
                    "url": page.url,
                    f"screenshot_{viewport_name}": gate_path,
                    "auth_detected": True,
                })
                break

            elements = await extract_elements(page)
            if not elements:
                log("No interactive elements — backtracking to homepage", "WARN")
                try:
                    await page.goto(start_url, wait_until="domcontentloaded")
                    await settle_page(page)
                    elements = await extract_elements(page)
                except Exception:
                    pass
                if not elements:
                    break

            # ── Action decision ──────────────────────────────────────────────
            action_choice = None

            if use_ai and await check_opencode():
                prompt = (
                    f"Enterprise web crawler — goal: '{goal}'\n"
                    f"Current URL: {page.url}\nStep: {step}\n"
                    f"Remaining nav queue: {[l['text'] for l in nav_queue[:5]]}\n"
                    f"Already visited URLs: {list(local_visited)}\n"
                    f"Interactive elements (first 60):\n{json.dumps(elements[:60])}\n\n"
                    f"Select the BEST next action to map all major website sections.\n"
                    f"Prefer navigating to unvisited nav sections over clicking random elements. DO NOT navigate to an already visited URL.\n"
                    f"Respond ONLY with JSON:\n"
                    f'{{"thought":"...", "action":"click"|"navigate"|"done", "id":<int>, "value":"<url if navigate>"}}'
                )
                try:
                    raw = await ask_opencode(prompt, model)
                    if raw:
                        action_choice = json.loads(extract_json_block(raw))
                        log(f"[AI] {action_choice.get('thought', '')[:80]}", "AI")
                except Exception:
                    pass

            if not action_choice:
                action_choice = await heuristic_next_action(elements, page, goal, step, nav_queue)
                log(f"[Heuristic] {action_choice.get('thought', '')[:80]}", "INFO")

            action = action_choice.get("action", "done")
            if action == "done":
                log("Crawler declared done.", "DONE")
                break

            step += 1
            target_el = None
            if action_choice.get("id") is not None:
                target_el = next((e for e in elements if e["id"] == action_choice["id"]), None)

            # ── Execute action ───────────────────────────────────────────────
            prev_url = page.url
            try:
                if action == "navigate":
                    dest = action_choice.get("value", "")
                    if not dest:
                        log("Invalid navigate destination, defaulting to done.", "WARN")
                        break
                    clean = dest.split("?")[0].rstrip("/")
                    if clean in local_visited:
                        log(f"AI chose already visited URL: {dest}, forcing heuristic or done.", "WARN")
                        break
                    log(f"[{step}] Navigate → {dest}", "NAV")
                    await page.goto(dest, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    await settle_page(page, full_scroll=full_page)

                elif action == "click" and target_el:
                    log(f"[{step}] Click: {target_el.get('text') or target_el['selector']}", "INFO")
                    try:
                        await page.click(target_el["selector"], timeout=5000)
                    except Exception:
                        await dismiss_all_overlays(page)
                        try:
                            await page.click(target_el["selector"], force=True, timeout=4000)
                        except Exception:
                            await page.evaluate(
                                "(s) => { const e = document.querySelector(s); if(e) e.click(); }",
                                target_el["selector"]
                            )
                    await settle_page(page, full_scroll=full_page)
                    # Track failed selectors to avoid retry
                    _failed_selectors.add(target_el["selector"])

                    # Handle new tab
                    if len(context.pages) > 1 and context.pages[-1] != page:
                        page = context.pages[-1]
                        await page.bring_to_front()

                else:
                    log("AI chose invalid action, forcing done to prevent infinite loop.", "WARN")
                    break

                new_url = page.url
                new_clean = new_url.split("?")[0].rstrip("/")

                # Skip if same page or already visited
                if new_clean in local_visited and new_url == prev_url:
                    step -= 1
                    continue

                local_visited.add(new_clean)

                shot_path = os.path.join(output_dir, f"{viewport_name}_step_{step:02d}_{action}.png")
                await take_screenshot(page, shot_path, full_page)

                path_label = urlparse(new_url).path or "/"
                name = (target_el.get("text") or path_label)[:50] if target_el else path_label[:50]

                flow_steps.append({
                    "step": step,
                    "name": name,
                    "url": new_url,
                    f"screenshot_{viewport_name}": shot_path,
                })

            except Exception as err:
                log(f"Action error: {err}", "ERR")
                if target_el:
                    _failed_selectors.add(target_el.get("selector", ""))
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                except Exception:
                    pass
                step -= 1
                continue

    finally:
        await browser.close()

    elapsed = time.monotonic() - t0
    log(f"{viewport_name.upper()} crawl complete: {len(flow_steps)} steps in {elapsed:.1f}s", "DONE")
    return flow_steps


# ─────────────────────────────────────────────────────────────────────────────
# Merge dual-viewport step lists into unified flow
# ─────────────────────────────────────────────────────────────────────────────
def merge_flow_steps(desktop_steps: list[dict], mobile_steps: list[dict]) -> list[dict]:
    """
    Merge desktop and mobile steps by URL (step index order).
    Desktop steps are the master; mobile screenshots are stitched in.
    """
    merged = []
    mobile_by_path = {}
    for s in mobile_steps:
        path = s["url"].split("?")[0].rstrip("/") or "/"
        mobile_by_path[path] = s

    for d in desktop_steps:
        path = d["url"].split("?")[0].rstrip("/") or "/"
        m = mobile_by_path.get(path, {})
        merged.append({
            **d,
            "screenshot_mobile": m.get("screenshot_mobile"),
        })

    # Append any mobile-only pages (not in desktop)
    desktop_paths = {s["url"].split("?")[0].rstrip("/") for s in desktop_steps}
    for m in mobile_steps:
        path = m["url"].split("?")[0].rstrip("/") or "/"
        if path not in desktop_paths:
            merged.append(m)

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Main Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
async def run(
    start_url: str,
    goal: str,
    output_dir: str,
    model: str,
    full_page: bool,
    use_ai: bool,
    desktop_only: bool,
    mobile_only: bool,
):
    t0 = time.monotonic()
    os.makedirs(output_dir, exist_ok=True)

    log("=" * 60)
    log(f"Enterprise Web Flow Mapper v{VERSION}")
    log(f"URL    : {start_url}")
    log(f"Goal   : {goal}")
    log(f"Model  : {model}")
    log(f"AI     : {'enabled' if use_ai else 'disabled (heuristic only)'}")
    log(f"Output : {output_dir}/")
    log("=" * 60)

    viewports_used = []
    if not mobile_only:
        viewports_used.append("desktop")
    if not desktop_only:
        viewports_used.append("mobile")

    shared_nav_queue: list = []
    desktop_steps: list[dict] = []
    mobile_steps:  list[dict] = []

    async with async_playwright() as pw:
        if "desktop" in viewports_used:
            desktop_steps = await crawl_viewport(
                pw, start_url, goal, output_dir, model,
                "desktop", full_page, use_ai, shared_nav_queue
            )

        if "mobile" in viewports_used:
            mobile_steps = await crawl_viewport(
                pw, start_url, goal, output_dir, model,
                "mobile", full_page, use_ai, shared_nav_queue
            )

    # Merge
    if desktop_steps and mobile_steps:
        flow_steps = merge_flow_steps(desktop_steps, mobile_steps)
    elif desktop_steps:
        flow_steps = desktop_steps
    else:
        flow_steps = mobile_steps

    # ── Build Excalidraw whiteboard ─────────────────────────────────────────
    log("Building Excalidraw whiteboard...", "INFO")
    sketch = build_excalidraw(flow_steps, start_url, goal, viewports_used)

    sketch_path = os.path.join(output_dir, "userflow.sketch.json")
    with open(sketch_path, "w", encoding="utf-8") as f:
        json.dump(sketch, f, separators=(",", ":"))

    # ── Sitemap JSON ─────────────────────────────────────────────────────────
    sitemap = {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_url": start_url,
        "goal": goal,
        "viewports": viewports_used,
        "total_steps": len(flow_steps),
        "pages": [
            {
                "step": s["step"],
                "name": s.get("name", ""),
                "url": s["url"],
                "auth_gate": s.get("auth_detected", False),
                "screenshot_desktop": s.get("screenshot_desktop"),
                "screenshot_mobile": s.get("screenshot_mobile"),
                "has_responsive_diff": has_responsive_diff(
                    s.get("screenshot_desktop"), s.get("screenshot_mobile")
                ),
            }
            for s in flow_steps
        ],
    }
    sitemap_path = os.path.join(output_dir, "sitemap.json")
    with open(sitemap_path, "w", encoding="utf-8") as f:
        json.dump(sitemap, f, indent=2)

    elapsed = time.monotonic() - t0
    log("=" * 60, "INFO")
    log(f"COMPLETE in {elapsed:.1f}s", "DONE")
    log(f"  Pages mapped    : {len(flow_steps)}", "INFO")
    log(f"  Viewports       : {', '.join(viewports_used)}", "INFO")
    log(f"  Whiteboard      : {sketch_path}", "INFO")
    log(f"  Sitemap JSON    : {sitemap_path}", "INFO")
    log("=" * 60, "INFO")
    log(f"Open {sketch_path} in Open Design to view your flow map.", "INFO")


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description=f"Enterprise Web Flow Mapper v{VERSION} — Dual-viewport screenshot + Excalidraw user flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full dual-viewport map (default)
  python crawl_map_ai.py --url https://stripe.com --goal "Map all sections"

  # Desktop only, full-page screenshots
  python crawl_map_ai.py --url https://linear.app --goal "Map features and pricing" --desktop-only --full-page

  # Mobile only, no AI (pure heuristic nav)
  python crawl_map_ai.py --url https://notion.so --goal "Map all nav pages" --mobile-only --no-ai

  # Custom output dir + model
  python crawl_map_ai.py --url https://figma.com --goal "Map site" --output-dir figma_flow --model google/gemini-2.0-flash
        """
    )
    parser.add_argument("--url",          required=True,                   help="Target website URL")
    parser.add_argument("--goal",         required=True,                   help="Navigation goal / what to map")
    parser.add_argument("--output-dir",   default="screenshots_ai",        help="Output directory (default: screenshots_ai)")
    parser.add_argument("--model",        default=DEFAULT_MODEL,            help=f"OpenCode model (default: {DEFAULT_MODEL})")
    parser.add_argument("--full-page",    action="store_true",              help="Capture full scrolled-page height (default: viewport only)")
    parser.add_argument("--desktop-only", action="store_true",              help="Desktop viewport only (1440×900)")
    parser.add_argument("--mobile-only",  action="store_true",              help="Mobile viewport only (390×844)")
    parser.add_argument("--no-ai",        action="store_true",              help="Disable AI reasoning, use heuristic nav only")
    parser.add_argument("--max-steps",    type=int, default=MAX_STEPS,      help=f"Max steps per viewport (default: {MAX_STEPS})")
    args = parser.parse_args()

    if args.desktop_only and args.mobile_only:
        parser.error("--desktop-only and --mobile-only cannot be used together")

    # Apply max steps override
    globals()['MAX_STEPS'] = args.max_steps

    asyncio.run(run(
        start_url    = args.url,
        goal         = args.goal,
        output_dir   = args.output_dir,
        model        = args.model,
        full_page    = args.full_page,
        use_ai       = not args.no_ai,
        desktop_only = args.desktop_only,
        mobile_only  = args.mobile_only,
    ))


if __name__ == "__main__":
    main()
