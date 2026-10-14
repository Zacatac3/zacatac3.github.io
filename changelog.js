function getLatestChangelogDate(text) {
  let latestDate = null;
  const titles = text.matchAll(/^Title:.*?[-–—]\s*(\d{1,2})\/(\d{1,2})\/(\d{4}|\d{2})\s*$/gm);

  for (const match of titles) {
    const month = Number(match[1]);
    const day = Number(match[2]);
    const year = match[3].length === 2 ? 2000 + Number(match[3]) : Number(match[3]);
    const date = new Date(Date.UTC(year, month - 1, day));

    // Ignore invalid dates instead of allowing Date to roll them into another month.
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) continue;
    if (!latestDate || date > latestDate) latestDate = date;
  }

  return latestDate;
}

async function loadChangelog() {
  try {
    const response = await fetch('/changelog.txt');
    if (!response.ok) throw new Error(`Failed to fetch changelog: ${response.status}`);
    const text = await response.text();
    const lastUpdated = document.getElementById('changelog-last-updated');
    const latestDate = getLatestChangelogDate(text);

    if (lastUpdated && latestDate) {
      const formattedDate = latestDate.toLocaleDateString('en-US', {
        month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC'
      });
      lastUpdated.textContent = `Last Updated: ${formattedDate}`;
      lastUpdated.hidden = false;
    }

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
