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
    
    // Notify the user how many images were received
    const imageCount = Object.keys(images).length;
    figma.notify(`Importing ${nodes.length} pages and ${imageCount} screenshots...`);
    
    function findImgData(targetName) {
        if (!targetName) return null;
        if (images[targetName]) return images[targetName];
        for (const [filename, data] of Object.entries(images)) {
            if (filename.includes(targetName) || targetName.includes(filename)) {
                return data;
            }
        }
        return null;
    }

    for (const page of nodes) {
      let targetDesktop = page.screenshot_desktop ? page.screenshot_desktop.split('/').pop().split('\\').pop() : null;
      let targetMobile = page.screenshot_mobile ? page.screenshot_mobile.split('/').pop().split('\\').pop() : null;
      
      let desktopData = findImgData(targetDesktop);
      let mobileData = findImgData(targetMobile);
      
      // Ultimate fallback for missing sitemap references
      if (!desktopData && !mobileData) {
          const searchKey = (page.url || page.id || page.title || '').replace(/[^a-z0-9]/gi, '_').substring(0, 30);
          desktopData = findImgData(searchKey);
      }
      
      const elementsToGroup = [];
      let currentX = xOffset;
      
      // Draw Desktop
      if (desktopData) {
          const frame = figma.createFrame();
          frame.name = "Desktop";
          frame.x = currentX;
          frame.y = 0;
          frame.resize(desktopData.width, desktopData.height);
          frame.layoutMode = "VERTICAL";
          frame.itemSpacing = 0;
          frame.fills = []; // Transparent
          
          for (let i = 0; i < desktopData.chunks.length; i++) {
              const chunkHeight = Math.min(4000, desktopData.height - (i * 4000));
              const rect = figma.createRectangle();
              rect.resize(desktopData.width, chunkHeight);
              try {
                  const imageHash = figma.createImage(new Uint8Array(desktopData.chunks[i])).hash;
                  rect.fills = [{ type: 'IMAGE', scaleMode: 'FILL', imageHash }];
              } catch (e) {
                  rect.fills = [{ type: 'SOLID', color: { r: 1, g: 0, b: 0 } }];
              }
              frame.appendChild(rect);
          }
          elementsToGroup.push(frame);
          currentX += desktopData.width + 100;
      }
      
      // Draw Mobile
      if (mobileData) {
          const frame = figma.createFrame();
          frame.name = "Mobile";
          frame.x = currentX;
          frame.y = 0;
          frame.resize(mobileData.width, mobileData.height);
          frame.layoutMode = "VERTICAL";
          frame.itemSpacing = 0;
          frame.fills = [];
          
          for (let i = 0; i < mobileData.chunks.length; i++) {
              const chunkHeight = Math.min(4000, mobileData.height - (i * 4000));
              const rect = figma.createRectangle();
              rect.resize(mobileData.width, chunkHeight);
              try {
                  const imageHash = figma.createImage(new Uint8Array(mobileData.chunks[i])).hash;
                  rect.fills = [{ type: 'IMAGE', scaleMode: 'FILL', imageHash }];
              } catch (e) {
                  rect.fills = [{ type: 'SOLID', color: { r: 1, g: 0, b: 0 } }];
              }
              frame.appendChild(rect);
          }
          elementsToGroup.push(frame);
          currentX += mobileData.width + 100;
      }
      
      // Fallback if no images found
      if (!desktopData && !mobileData) {
          const fallback = figma.createRectangle();
          fallback.x = currentX;
          fallback.y = 0;
          fallback.resize(400, 800);
          fallback.fills = [{ type: 'SOLID', color: { r: 0.9, g: 0.9, b: 0.9 } }];
          elementsToGroup.push(fallback);
          currentX += 400 + 100;
      }
      
      // Add label above the group
      await figma.loadFontAsync({ family: "Inter", style: "Regular" });
      const text = figma.createText();
      text.characters = page.url || page.title || page.id || "Unknown Page";
      text.fontSize = 32;
      text.x = xOffset;
      text.y = -60;
      elementsToGroup.push(text);
      
      // Group them together
      const group = figma.group(elementsToGroup, figma.currentPage);
      group.name = text.characters;
      figma.currentPage.appendChild(group);
      
      // Store reference for connecting edges later
      const nodeId = page.id || page.url || page.title;
      if (nodeId) createdNodes[nodeId] = group;
      
      xOffset = currentX + 300; // Spacing between different pages
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
