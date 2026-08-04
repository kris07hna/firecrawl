import asyncio
import os
import json
import re
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright
from crawler.config import log, MAX_STEPS, VERSION, DEFAULT_MODEL
from crawler.engine import make_context, settle_page
from crawler.extractor import extract_information_architecture
from crawler.agent import ask_opencode

def is_valid_link(start_url, full_url):
    if not full_url or full_url.startswith(('javascript:', 'mailto:', 'tel:', '#')):
        return False
        
    # 1. Must be same domain
    if urlparse(start_url).netloc != urlparse(full_url).netloc:
        return False
        
    # 2. Locale Isolation (Path prefix locking)
    start_path = urlparse(start_url).path
    if not start_path.endswith('/'):
        # Just in case start_url is /in, treat as /in/
        # But if it's /pricing, this would restrict to /pricing/. We assume start_url is a root or locale root.
        parts = start_path.split('/')
        if len(parts) > 1 and len(parts[1]) <= 5: # likely a locale like /in or /en-us
            start_path = f"/{parts[1]}/"
        else:
            start_path = "/"
            
    if start_path != '/':
        target_path = urlparse(full_url).path
        if not target_path.startswith(start_path) and target_path not in ['/', '']:
            return False
            
    # 3. I18n block (If base is root, block other locales like /fr/, /de-be/)
    if start_path == '/':
        target_path = urlparse(full_url).path
        if len(target_path) > 1:
            first_segment = target_path.strip('/').split('/')[0]
            # Match 2 chars (fr), 3 chars (fra), or 5 chars with hyphen (en-gb)
            if re.match(r'^([a-z]{2,3}|[a-z]{2}-[a-z]{2})$', first_segment, re.IGNORECASE):
                # It's a locale path, skip it since start_url had no locale
                return False

    # 4. Auth loop prevention
    skip_keywords = ['login', 'signup', 'register', 'auth', 'signin', 'checkout', 'cart', 'password', 'account']
    if any(kw in full_url.lower() for kw in skip_keywords):
        return False
        
    return True

async def worker(worker_id: int, queue: asyncio.Queue, visited: set, site_graph: dict, context, start_url: str, max_pages: int):
    page = await context.new_page()
    
    # Block network requests for extreme speed
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "media", "font"] else route.continue_())
    
    while True:
        try:
            current_url = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
            
        if len(visited) >= max_pages:
            queue.task_done()
            continue
            
        log(f"[W{worker_id}] Scraping Page {len(visited)+1}/{max_pages} - {current_url}", "INFO")
        visited.add(current_url)
        
        try:
            # Extremely fast load, wait for domcontentloaded only
            await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            
            # Aggressive JS to open all dropdowns for Mega-Menu extraction
            await page.evaluate("""() => {
                const evts = ['mouseenter', 'mouseover', 'focus', 'click'];
                document.querySelectorAll('header, nav, [role="navigation"], .menu, .hamburger, [aria-haspopup="true"], [aria-expanded="false"], header li, nav li').forEach(el => {
                    try {
                        evts.forEach(e => el.dispatchEvent(new MouseEvent(e, {bubbles: true})));
                        if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') el.click();
                    } catch(err) {}
                });
            }""")
            
            # Minimal wait for JS framework hydration
            await asyncio.sleep(0.5)
            
            ia_data = await extract_information_architecture(page)
            
            link_objects = await page.evaluate("""() => {
                const getLoc = (el) => {
                    if (el.closest('header, nav, [role="navigation"]')) return 'header';
                    if (el.closest('footer, [role="contentinfo"]')) return 'footer';
                    return 'body';
                };
                return Array.from(document.querySelectorAll('a')).map(a => ({
                    href: a.getAttribute('href'),
                    text: (a.innerText || a.textContent || '').trim().substring(0, 50),
                    location: getLoc(a)
                })).filter(a => a.href);
            }""")
            
            categorized_links = {"header": [], "footer": [], "body": []}
            
            for obj in link_objects:
                full_url = urljoin(current_url, obj.get("href", "")).split('#')[0]
                if is_valid_link(start_url, full_url):
                    # Add to graph
                    if not any(l["url"] == full_url for l in categorized_links[obj["location"]]):
                        categorized_links[obj["location"]].append({"url": full_url, "text": obj["text"]})
                    # Add to queue
                    if full_url not in visited:
                        # Only add if not already in queue (basic check)
                        queue.put_nowait(full_url)
                        
            title = await page.title()
            site_graph[current_url] = {
                "title": title,
                "ia": ia_data,
                "links": categorized_links
            }
            
        except Exception as e:
            log(f"[W{worker_id}] Error on {current_url}: {e}", "WARN")
            
        queue.task_done()
        
    await page.close()


async def run_ux_ia(start_url: str, output_dir: str, model: str = DEFAULT_MODEL, max_pages: int = MAX_STEPS):
    os.makedirs(output_dir, exist_ok=True)
    log(f"Senior UX Engineer AI - Parallel Deep Crawler v{VERSION}", "INFO")
    log(f"URL       : {start_url}", "INFO")
    log(f"Max Pages : {max_pages}", "INFO")
    log(f"Output    : {output_dir}/", "INFO")
    
    site_graph = {}
    visited = set()
    queue = asyncio.Queue()
    queue.put_nowait(start_url)
    
    CONCURRENCY = 5
    
    async with async_playwright() as pw:
        log(f"Starting {CONCURRENCY} parallel workers with Locale Isolation and Asset Blocking...", "INFO")
        browser, context = await make_context(pw, "desktop")
        
        workers = [
            asyncio.create_task(worker(i, queue, visited, site_graph, context, start_url, max_pages))
            for i in range(CONCURRENCY)
        ]
        
        # We must wait for the queue to be fully processed or visited hits max_pages
        while not queue.empty() and len(visited) < max_pages:
            await asyncio.sleep(1)
            # Wake up dead workers if queue has items (since they exit when queue empty)
            for i in range(CONCURRENCY):
                if workers[i].done() and not queue.empty() and len(visited) < max_pages:
                    workers[i] = asyncio.create_task(worker(i, queue, visited, site_graph, context, start_url, max_pages))

        # Cancel remaining workers once done
        for w in workers:
            if not w.done():
                w.cancel()
                
        await browser.close()
        
    # 1. Export Raw Graph
    raw_path = os.path.join(output_dir, "raw_ia_graph.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(site_graph, f, indent=2)
    log(f"Raw IA Graph exported to {raw_path} ({len(site_graph)} pages)", "INFO")
    
    # 2. AI UX Synthesis
    log(f"Feeding {len(site_graph)} pages to Senior UX Engineer AI ({model}) for synthesis...", "AI")
    
    prompt = f"""
You are a Senior UX Engineer analyzing the Information Architecture of a website.
We performed an exhaustive deep-crawl utilizing Mega-Menu extraction on every page.
Below is the raw JSON data containing the Title, Semantic Headings, and all internal links found inside the Header (Mega-Menu), Body, and Footer for each page.

Synthesize this raw data into a clean, professional Information Architecture overview in Markdown format.
You must include:
1. Executive UX Overview: A written analysis of the site's structure based on the Mega-Menu and deep page hierarchy. Highlight UX patterns and groupings.
2. Mermaid Diagram: A perfectly formatted Mermaid `mindmap` or `flowchart TD` representing the site's hierarchy (do not include every single utility/footer link, summarize logically).
3. Categorized Page List: Group the core pages found in the crawl into logical UX categories (e.g. Primary Navigation, Utilities, Auth Flows, Content Hubs).

Raw Data:
{json.dumps(site_graph)}
"""
    
    ai_response = await ask_opencode(prompt, model=model)
    
    md_path = os.path.join(output_dir, "ux_ia_overview.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(ai_response)
        
    log(f"Senior UX Overview generated at {md_path}", "INFO")
