import argparse
import asyncio
from crawler.config import VERSION, DEFAULT_MODEL, MAX_STEPS
from crawler.runner import run_crawler
from crawler.spider import run_spider

def main():
    parser = argparse.ArgumentParser(
        description=f"Enterprise Web Flow Mapper v{VERSION} — Dual-viewport screenshot + UX Information Architecture Extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url",          required=True,                   help="Target website URL")
    parser.add_argument("--mode",         choices=["flow", "spider"], default="flow", help="Crawler mode: 'flow' (AI-driven path) or 'spider' (BFS site mapping)")
    parser.add_argument("--goal",         default="Explore website",       help="Navigation goal / what to map")
    parser.add_argument("--output-dir",   default="screenshots_ai",        help="Output directory (default: screenshots_ai)")
    parser.add_argument("--model",        default=DEFAULT_MODEL,           help=f"OpenCode model (default: {DEFAULT_MODEL})")
    parser.add_argument("--full-page",    action="store_true",             help="Capture full scrolled-page height (default: viewport only)")
    parser.add_argument("--desktop-only", action="store_true",             help="Desktop viewport only")
    parser.add_argument("--mobile-only",  action="store_true",             help="Mobile viewport only")
    parser.add_argument("--no-ai",        action="store_true",             help="Disable AI reasoning, use heuristic nav only")
    parser.add_argument("--max-steps",    type=int, default=MAX_STEPS,     help=f"Max steps per viewport (default: {MAX_STEPS})")
    args = parser.parse_args()

    if args.desktop_only and args.mobile_only:
        parser.error("--desktop-only and --mobile-only cannot be used together")

    if args.mode == "flow":
        asyncio.run(run_crawler(
            start_url    = args.url,
            goal         = args.goal,
            output_dir   = args.output_dir,
            model        = args.model,
            full_page    = args.full_page,
            desktop_only = args.desktop_only,
            mobile_only  = args.mobile_only,
        ))
    else:
        asyncio.run(run_spider(
            start_url    = args.url,
            output_dir   = args.output_dir,
            full_page    = args.full_page,
            desktop_only = args.desktop_only,
            mobile_only  = args.mobile_only,
            max_pages    = args.max_steps
        ))

if __name__ == "__main__":
    main()
