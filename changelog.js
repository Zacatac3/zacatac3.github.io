async function loadChangelog() {
  try {
    const response = await fetch('/changelog.txt');
    const text = await response.text();
    const container = document.getElementById('changelog-container');
    
    if (!container) return;
    
    // Split by === to get individual update blocks
    const blocks = text.split('===').map(b => b.trim()).filter(b => b);
    
    blocks.forEach(block => {
      const lines = block.split('\n').map(l => l.trim()).filter(l => l);
      
      const section = document.createElement('section');
      section.className = 'update-container';
      
      let html = '<div class="policy-section">';
      let currentHeading = null;
      let items = [];
      
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        
        if (line.startsWith('Title:')) {
          const title = line.substring(6).trim();
          html += `<h2>${title}</h2>`;
        } else if (line.startsWith('Description:')) {
          const desc = line.substring(12).trim();
          html += `<p><strong>${desc}</strong></p>`;
        } else if (line.startsWith('Heading:')) {
          // Output previous heading's items if any
          if (currentHeading && items.length > 0) {
            html += `<h3>${currentHeading}</h3><ul>`;
            items.forEach(item => {
              html += `<li>${item}</li>`;
            });
            html += '</ul>';
          }
          currentHeading = line.substring(8).trim();
          items = [];
        } else if (line.startsWith('-')) {
          items.push(line.substring(1).trim());
        }
      }
      
      // Output final heading's items
      if (currentHeading && items.length > 0) {
        html += `<h3>${currentHeading}</h3><ul>`;
        items.forEach(item => {
          html += `<li>${item}</li>`;
        });
        html += '</ul>';
      }
      
      html += '</div>';
      section.innerHTML = html;
      container.appendChild(section);
    });
  } catch (error) {
    console.error('Failed to load changelog:', error);
  }
}

document.addEventListener('DOMContentLoaded', loadChangelog);
