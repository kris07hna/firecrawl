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
        current_url = await queue.get()
            
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
            
            # Organized Visual Hierarchy Extraction
            categorized_links = await page.evaluate("""() => {
                const getHeadingText = (el) => {
                    let current = el;
                    while (current && current !== document.body) {
                        let prev = current.previousElementSibling;
                        while(prev) {
                            if (['H1','H2','H3','H4','H5','H6'].includes(prev.tagName) || prev.getAttribute('role') === 'heading') {
                                return prev.innerText.trim();
                            }
                            prev = prev.previousElementSibling;
                        }
                        const heading = current.querySelector('h1, h2, h3, h4, h5, h6, strong, [role="heading"], .title, .heading');
                        if (heading && heading !== el) return heading.innerText.trim();
                        current = current.parentElement;
                    }
                    return "General Links";
                };

                const getLoc = (el) => {
                    if (el.closest('header, nav, [role="navigation"]')) return 'Header';
                    if (el.closest('footer, [role="contentinfo"]')) return 'Footer';
                    return 'Body';
                };

                const isButton = (el) => {
                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' || el.classList.contains('button') || el.classList.contains('btn')) return true;
                    const style = window.getComputedStyle(el);
                    if (style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.padding !== '0px' && style.borderRadius !== '0px') return true;
                    return false;
                };

                const elements = Array.from(document.querySelectorAll('a, button'));
                
                let structure = {
                    Header: { Buttons: [], Dropdowns: {} },
                    Body: { Buttons: [], Sections: {} },
                    Footer: { Columns: {} }
                };

                elements.forEach(el => {
                    const text = (el.innerText || el.textContent || '').trim().substring(0, 40);
                    if (!text) return;
                    
                    const loc = getLoc(el);
                    const href = el.getAttribute('href') || null;
                    if (href && (href.startsWith('javascript:') || href.startsWith('#'))) return;

                    const isBtn = isButton(el);
                    const item = { text, href };

                    if (loc === 'Header') {
                        if (isBtn) {
                            structure.Header.Buttons.push(item);
                        } else {
                            const dropdownParent = el.closest('ul, ol, [role="menu"], [role="listbox"], .dropdown, .menu, nav div');
                            const dropdownName = dropdownParent ? getHeadingText(dropdownParent) : "Main Nav";
                            if (!structure.Header.Dropdowns[dropdownName]) structure.Header.Dropdowns[dropdownName] = [];
                            if (!structure.Header.Dropdowns[dropdownName].find(i => i.text === text)) {
                                structure.Header.Dropdowns[dropdownName].push(item);
                            }
                        }
                    } else if (loc === 'Footer') {
                        const colParent = el.closest('div, section, ul');
                        const colName = colParent ? getHeadingText(colParent) : "Legal/Utility";
                        if (!structure.Footer.Columns[colName]) structure.Footer.Columns[colName] = [];
                        if (!structure.Footer.Columns[colName].find(i => i.text === text)) {
                            structure.Footer.Columns[colName].push(item);
                        }
                    } else {
                        if (isBtn) {
                            structure.Body.Buttons.push(item);
                        } else {
                            const secParent = el.closest('section, article, div.section');
                            const secName = secParent ? getHeadingText(secParent) : "In-Page Links";
                            if (!structure.Body.Sections[secName]) structure.Body.Sections[secName] = [];
                            if (!structure.Body.Sections[secName].find(i => i.text === text)) {
                                structure.Body.Sections[secName].push(item);
                            }
                        }
                    }
                });
                
                if (structure.Header.Buttons.length === 0) delete structure.Header.Buttons;
                if (structure.Body.Buttons.length === 0) delete structure.Body.Buttons;

                return structure;
            }""")
            
            # Extract URLs to continue crawling
            def get_hrefs(d):
                urls = []
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k == 'href' and isinstance(v, str):
                            urls.append(v)
                        else:
                            urls.extend(get_hrefs(v))
                elif isinstance(d, list):
                    for i in d:
                        urls.extend(get_hrefs(i))
                return urls
            
            hrefs = get_hrefs(categorized_links)
            for href in hrefs:
                full_url = urljoin(current_url, href).split('#')[0]
                if is_valid_link(start_url, full_url) and full_url not in visited:
                    queue.put_nowait(full_url)
                        
            title = await page.title()
            site_graph[current_url] = {
                "title": title,
                "ia": ia_data,
                "visual_hierarchy": categorized_links
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
        join_task = asyncio.create_task(queue.join())
        
        while True:
            if len(visited) >= max_pages:
                break
            if join_task.done():
                break
            await asyncio.sleep(0.5)

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
Below is the highly structured Semantic Visual Hierarchy extracted from the website.
Instead of raw links, the data is grouped visually by Header Dropdowns, Footer Columns, and Body Sections for each page.

Synthesize this hierarchical data into a clean, professional Information Architecture overview in Markdown format.
You must include:
1. Executive UX Overview: A written analysis of the site's structure based on the visually grouped Dropdowns and Columns.
2. Mermaid Diagram: A perfectly formatted Mermaid `mindmap` or `flowchart TD` representing the logical hierarchy of the site's Dropdowns and Categories (do not list raw URLs).
3. Section Map: A breakdown of the primary Header and Footer categories and what they contain.

Raw Data:
{json.dumps(site_graph)}
"""
    
    ai_response = await ask_opencode(prompt, model=model)
    
    md_path = os.path.join(output_dir, "ux_ia_overview.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(ai_response)
        
    log(f"Senior UX Overview generated at {md_path}", "INFO")
