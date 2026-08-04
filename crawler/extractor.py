import json

async def extract_information_architecture(page):
    """
    Extracts the semantic structure of the page to build an Information Architecture (IA) map.
    This runs in the browser context via page.evaluate.
    """
    ia_data = await page.evaluate("""() => {
        function getElementText(el) {
            return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().substring(0, 100);
        }
        
        function extractHierarchy(root) {
            const structure = { headings: [], links: [], semantics: {} };
            
            // Extract headings
            const headings = root.querySelectorAll('h1, h2, h3');
            headings.forEach(h => {
                structure.headings.push({
                    level: h.tagName.toLowerCase(),
                    text: getElementText(h)
                });
            });
            
            // Extract semantic regions
            ['nav', 'main', 'header', 'footer', 'aside'].forEach(tag => {
                const els = root.querySelectorAll(tag);
                if (els.length > 0) {
                    structure.semantics[tag] = true;
                }
            });
            
            // Extract main navigation links (if nav exists)
            const navs = root.querySelectorAll('nav');
            navs.forEach(nav => {
                const links = nav.querySelectorAll('a');
                links.forEach(a => {
                    const text = getElementText(a);
                    if (text && text.length > 0) {
                        structure.links.push({
                            text: text,
                            href: a.getAttribute('href')
                        });
                    }
                });
            });
            
            return structure;
        }
        
        return extractHierarchy(document);
    }""")
    return ia_data
