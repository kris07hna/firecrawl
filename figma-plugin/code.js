// This plugin imports the AI-generated User Flow JSON and places the screenshots onto the Figma canvas

figma.showUI(__html__, { width: 450, height: 350 });

figma.ui.onmessage = async (msg) => {
  if (msg.type === 'import') {
    const { sitemap, images } = msg;
    
    // 1. Analyze the JSON structure to find nodes
    let nodes = [];
    let edges = [];

    // Heuristic: Check if it's the Excalidraw format
    if (sitemap.elements) {
      nodes = sitemap.elements.filter(e => e.type === 'image' || e.type === 'rectangle');
    } 
    // Heuristic: Check if it's the Sitemap JSON format (dict or array)
    else if (Array.isArray(sitemap)) {
      nodes = sitemap;
    } else if (typeof sitemap === 'object') {
      if (sitemap.nodes) nodes = sitemap.nodes;
      else if (sitemap.pages) nodes = sitemap.pages;
      else nodes = Object.values(sitemap); // Assume dictionary of pages
      
      if (sitemap.edges) edges = sitemap.edges;
      else if (sitemap.links) edges = sitemap.links;
    }

    if (nodes.length === 0) {
      figma.notify("Could not find any pages in the JSON file. Generating default layout.");
      // Fallback: Just place all images
      nodes = Object.keys(images).map(filename => ({ url: filename, screenshot: filename }));
    }

    const createdNodes = {};
    let xOffset = 0;
    
    // 2. Load images into Figma and create rectangles
    for (const page of nodes) {
      // Find matching image buffer
      let imgBuffer = null;
      
      // Look for explicit screenshot filename in the JSON
      if (page.screenshot && images[page.screenshot]) {
         imgBuffer = images[page.screenshot];
      } else {
         // Fuzzy match based on URL or ID
         const searchKey = (page.url || page.id || page.title || '').replace(/[^a-z0-9]/gi, '_').substring(0, 30);
         for (const [filename, buffer] of Object.entries(images)) {
           if (filename.includes(searchKey)) {
             imgBuffer = buffer;
             break;
           }
         }
         // Ultimate fallback: Just grab the first image if this is a fallback node
         if (!imgBuffer && page.screenshot && images[page.screenshot]) {
            imgBuffer = images[page.screenshot];
         }
      }

      // Create the visual node in Figma
      const rect = figma.createRectangle();
      
      // Use Excalidraw coordinates if they exist, otherwise arrange horizontally
      rect.x = page.x !== undefined ? page.x : xOffset;
      rect.y = page.y !== undefined ? page.y : 0;
      
      // Set size
      rect.resize(page.width || 400, page.height || 800);
      
      // Set the image fill
      if (imgBuffer) {
        const imageHash = figma.createImage(imgBuffer).hash;
        rect.fills = [{ type: 'IMAGE', scaleMode: 'FIT', imageHash }];
      } else {
        // Fallback color if image is missing
        rect.fills = [{ type: 'SOLID', color: { r: 0.9, g: 0.9, b: 0.9 } }];
      }
      
      // Add a label above the screenshot
      await figma.loadFontAsync({ family: "Inter", style: "Regular" });
      const text = figma.createText();
      text.characters = page.url || page.title || page.id || "Unknown Page";
      text.fontSize = 16;
      text.x = rect.x;
      text.y = rect.y - 24;
      
      // Group them together
      const group = figma.group([rect, text], figma.currentPage);
      group.name = text.characters;
      
      figma.currentPage.appendChild(group);
      
      // Store reference for connecting edges later
      const nodeId = page.id || page.url || page.title;
      if (nodeId) createdNodes[nodeId] = rect;
      
      xOffset += (page.width || 400) + 200;
    }
    
    // 3. Draw Edges (Interconnections)
    if (edges.length > 0) {
      for (const edge of edges) {
        const sourceId = edge.source || edge.from;
        const targetId = edge.target || edge.to;
        
        const sourceNode = createdNodes[sourceId];
        const targetNode = createdNodes[targetId];
        
        if (sourceNode && targetNode) {
          const connector = figma.createConnector();
          connector.connectorStart = { endpointNodeId: sourceNode.id, magnetic: 'AUTO' };
          connector.connectorEnd = { endpointNodeId: targetNode.id, magnetic: 'AUTO' };
          figma.currentPage.appendChild(connector);
        }
      }
    }
    
    figma.viewport.scrollAndZoomIntoView(figma.currentPage.children);
    figma.closePlugin("Flow imported successfully!");
  }
};
