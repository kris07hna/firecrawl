import asyncio
import os
import json
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from playwright.async_api import async_playwright
from crawler.config import log, MAX_STEPS, VERSION
from crawler.engine import make_context, take_screenshot, settle_page
from crawler.extractor import extract_information_architecture

def get_same_domain_links(base_url, hrefs):
    base_domain = urlparse(base_url).netloc
    valid_links = set()
    
    # Skip pages that ask for details to enter (forms, logins, checkouts)
    skip_keywords = ['login', 'signup', 'register', 'auth', 'signin', 'checkout', 'cart', 'password', 'account']
    
    for href in hrefs:
        if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
            continue
        full_url = urljoin(base_url, href)
        if urlparse(full_url).netloc == base_domain:
            # strip fragments
            full_url = full_url.split('#')[0]
            
            # Skip if it matches a form/auth keyword
            if any(kw in full_url.lower() for kw in skip_keywords):
                continue
                
            valid_links.add(full_url)
    return list(valid_links)

async def run_spider(start_url: str, output_dir: str, full_page: bool = True, desktop_only: bool = False, mobile_only: bool = False, max_pages: int = MAX_STEPS):
    os.makedirs(output_dir, exist_ok=True)
    log(f"Enterprise Site Spider Mapper v{VERSION}", "INFO")
    log(f"URL       : {start_url}", "INFO")
    log(f"Max Pages : {max_pages}", "INFO")
    log(f"Output    : {output_dir}/", "INFO")
    
    viewports = []
    if not mobile_only: viewports.append("desktop")
    if not desktop_only: viewports.append("mobile")
    
    # We will run the spider for each viewport separately.
    # A more advanced spider could do them in parallel, but sequential is safer for resources.
    
    flow_steps = []
    
    async with async_playwright() as pw:
        for vp_name in viewports:
            log(f"Starting {vp_name.upper()} spider crawl...", "INFO")
            browser, context = await make_context(pw, vp_name)
            page = await context.new_page()
            
            queue = [start_url]
            visited = set()
            
            step = 1
            while queue and step <= max_pages:
                current_url = queue.pop(0)
                if current_url in visited:
                    continue
                
                visited.add(current_url)
                log(f"Page {step}/{max_pages} - {current_url}", "INFO")
                
                try:
                    await page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log(f"Navigation timeout/error (ignoring): {e}", "WARN")
                    
                await settle_page(page)
                
                # 1. Take Screenshot
                shot_name = f"{vp_name}_page_{step:02d}.png"
                shot_path = os.path.join(output_dir, shot_name)
                await take_screenshot(page, shot_path, full_page)
                
                # 2. Extract UX Information Architecture
                ia_data = await extract_information_architecture(page)
                
                # 3. Extract Links for Queue
                hrefs = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a')).map(a => a.getAttribute('href')).filter(Boolean);
                }""")
                
                new_links = get_same_domain_links(start_url, hrefs)
                for link in new_links:
                    if link not in visited and link not in queue:
                        queue.append(link)
                
                # Record step
                step_data = {
                    "step": step,
                    "url": current_url,
                    "viewport": vp_name,
                    "screenshot": os.path.join(output_dir, shot_name),
                    "ia": ia_data,
                    "title": await page.title()
                }
                flow_steps.append(step_data)
                
                step += 1
                
            await browser.close()
            
    # Export Sitemap
    sitemap_path = os.path.join(output_dir, "sitemap.json")
    with open(sitemap_path, "w", encoding="utf-8") as f:
        # Group by step for output
        grouped = {}
        for s in flow_steps:
            st = s["step"]
            if st not in grouped:
                grouped[st] = {"step": st, "url": s["url"], "name": s["title"], "ia": s["ia"]}
            
            # Map screenshots
            if s["viewport"] == "desktop":
                grouped[st]["screenshot_desktop"] = s["screenshot"]
            else:
                grouped[st]["screenshot_mobile"] = s["screenshot"]
                
        sitemap = {
            "version": VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "start_url": start_url,
            "mode": "spider",
            "pages": list(grouped.values())
        }
        json.dump(sitemap, f, indent=2)
        log(f"Sitemap exported to {sitemap_path}", "INFO")
