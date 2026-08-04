import asyncio
import os
from datetime import datetime, timezone
from playwright.async_api import async_playwright
from crawler.config import log, MAX_STEPS, DEFAULT_MODEL, VERSION
from crawler.engine import make_context, take_screenshot, settle_page
from crawler.agent import synthesize_complex_goal
from crawler.extractor import extract_information_architecture
import json

async def run_crawler(start_url: str, goal: str, output_dir: str, model: str = DEFAULT_MODEL, full_page: bool = True, desktop_only: bool = False, mobile_only: bool = False):
    os.makedirs(output_dir, exist_ok=True)
    log(f"Enterprise Web Flow Mapper v{VERSION}")
    log(f"URL    : {start_url}")
    log(f"Goal   : {goal}")
    log(f"Output : {output_dir}/")
    
    viewports = []
    if not mobile_only: viewports.append("desktop")
    if not desktop_only: viewports.append("mobile")
    
    flow_steps = []
    
    async with async_playwright() as pw:
        for vp_name in viewports:
            log(f"Starting {vp_name.upper()} crawl...", "INFO")
            browser, context = await make_context(pw, vp_name)
            page = await context.new_page()
            
            try:
                # Increase timeout to 60s and ignore timeout errors. 
                # Many enterprise sites hang on 3rd party scripts, but the DOM is already loaded.
                await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"Navigation timeout/error (ignoring): {e}", "WARN")
                
            await settle_page(page)
            
            history = []
            
            for step in range(1, MAX_STEPS + 1):
                log(f"Step {step}/{MAX_STEPS}", "INFO")
                
                # 1. Take Screenshot
                shot_name = f"{vp_name}_step_{step:02d}.png"
                shot_path = os.path.join(output_dir, shot_name)
                await take_screenshot(page, shot_path, full_page)
                
                # 2. Extract UX Information Architecture
                ia_data = await extract_information_architecture(page)
                current_url = page.url
                
                # Record step
                step_data = {
                    "step": step,
                    "url": current_url,
                    "viewport": vp_name,
                    "screenshot": os.path.join(output_dir, shot_name), # Full path for now
                    "ia": ia_data,
                    "title": await page.title()
                }
                flow_steps.append(step_data)
                
                # 3. AI Reasoning for next action
                page_state = {
                    "url": current_url,
                    "ia": ia_data,
                    # We would add interactive elements extraction here for the AI to click
                }
                
                action = await synthesize_complex_goal(goal, page_state, history)
                history.append({"url": current_url, "action": action})
                
                log(f"AI Action: {action.get('action')} - {action.get('reasoning')}", "AI")
                
                # Execute Action
                if action.get("action") == "DONE":
                    log("Goal reached!", "INFO")
                    break
                elif action.get("action") == "CLICK" and action.get("target_selector"):
                    try:
                        await page.click(action["target_selector"], timeout=3000)
                        await settle_page(page)
                    except Exception as e:
                        log(f"Failed to click: {e}", "WARN")
                else:
                    log("Unsupported or no action taken, stopping viewport crawl.", "INFO")
                    break
                    
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
            "goal": goal,
            "pages": list(grouped.values())
        }
        json.dump(sitemap, f, indent=2)
        log(f"Sitemap exported to {sitemap_path}", "INFO")
