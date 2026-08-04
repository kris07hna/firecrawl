import asyncio
import os
import json
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright
from crawler.config import log, MAX_STEPS, VERSION, DEFAULT_MODEL
from crawler.engine import make_context, settle_page
from crawler.extractor import extract_information_architecture
from crawler.agent import ask_opencode

def get_same_domain_links(base_url, link_objects):
    base_domain = urlparse(base_url).netloc
    
    skip_keywords = ['login', 'signup', 'register', 'auth', 'signin', 'checkout', 'cart', 'password', 'account']
    
    valid_links = []
    seen = set()
    
    for obj in link_objects:
        href = obj.get("href")
        if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
            continue
            
        full_url = urljoin(base_url, href)
        if urlparse(full_url).netloc == base_domain:
            full_url = full_url.split('#')[0]
            
            if full_url in seen:
                continue
                
            seen.add(full_url)
            
            obj["full_url"] = full_url
            valid_links.append(obj)
            
    return valid_links

async def run_ux_ia(start_url: str, output_dir: str, model: str = DEFAULT_MODEL, max_pages: int = MAX_STEPS):
    os.makedirs(output_dir, exist_ok=True)
    log(f"Senior UX Engineer AI - IA Mapper v{VERSION}", "INFO")
    log(f"URL       : {start_url}", "INFO")
    log(f"Max Pages : {max_pages}", "INFO")
    
    site_graph = {}
    queue = [start_url]
    visited = set()
    
    async with async_playwright() as pw:
        log("Starting ultra-fast structural spider crawl...", "INFO")
        # Only use desktop, no screenshots needed
        browser, context = await make_context(pw, "desktop")
        page = await context.new_page()
        
        step = 1
        while queue and step <= max_pages:
            current_url = queue.pop(0)
            if current_url in visited:
                continue
            
            visited.add(current_url)
            log(f"Scraping Page {step}/{max_pages} - {current_url}", "INFO")
            
            try:
                await page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"Navigation error (ignoring): {e}", "WARN")
                
            await settle_page(page, full_scroll=False) # skip heavy full scroll
            
            ia_data = await extract_information_architecture(page)
            
            # Extract Links categorized by location
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
            
            valid_links = get_same_domain_links(start_url, link_objects)
            
            # Group links by location for the graph
            categorized_links = {"header": [], "footer": [], "body": []}
            
            # Auth loop prevention
            skip_keywords = ['login', 'signup', 'register', 'auth', 'signin', 'checkout', 'cart', 'password', 'account']
            is_auth_page = any(kw in current_url.lower() for kw in skip_keywords)
            
            for obj in valid_links:
                categorized_links[obj["location"]].append({"url": obj["full_url"], "text": obj["text"]})
                
                if not is_auth_page:
                    if obj["full_url"] not in visited and obj["full_url"] not in queue:
                        queue.append(obj["full_url"])
            
            title = await page.title()
            
            site_graph[current_url] = {
                "title": title,
                "ia": ia_data,
                "links": categorized_links
            }
            
            step += 1
            
        await browser.close()
        
    # 1. Export Raw Graph
    raw_path = os.path.join(output_dir, "raw_ia_graph.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(site_graph, f, indent=2)
    log(f"Raw IA Graph exported to {raw_path}", "INFO")
    
    # 2. AI UX Synthesis
    log(f"Feeding {len(site_graph)} pages to Senior UX Engineer AI ({model}) for synthesis...", "AI")
    
    prompt = f"""
You are a Senior UX Engineer analyzing the raw structural crawl of a website.
Below is the raw JSON data containing the URL, Title, Semantic Headings (IA), and Links (categorized by Header, Footer, Body) for each page visited on the site.

Synthesize this raw data into a clean, professional Information Architecture overview in Markdown format.
You must include:
1. Executive UX Overview: A written analysis of the site's structure, highlighting UX patterns, redundancies, or interesting architectural choices.
2. Mermaid Diagram: A perfectly formatted Mermaid `mindmap` or `flowchart TD` representing the site's hierarchy (do not include every single footer link, summarize logically).
3. Categorized Page List: Group the pages found into logical UX categories (e.g. Primary Navigation, Utilities, Auth Flows, Content Hubs).

Raw Data:
{json.dumps(site_graph)}
"""
    
    ai_response = await ask_opencode(prompt, model=model)
    
    md_path = os.path.join(output_dir, "ux_ia_overview.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(ai_response)
        
    log(f"Senior UX Overview generated at {md_path}", "INFO")
