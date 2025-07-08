const http = require('http');
const https = require('https'); // Use https for the Brawl Stars API
const fs = require('fs');
const path = require('path');
const url = require('url');

const port = process.env.PORT || 3000;
const API_KEY = process.env.BRAWL_STARS_API_KEY; // Read from environment variable
const BASE_URL = 'https://api.brawlstars.com/v1';
const API_HOSTNAME = 'api.brawlstars.com';

if (!API_KEY) {
    console.error("Error: BRAWL_STARS_API_KEY environment variable not set.");
    console.error("Please run the server like: BRAWL_STARS_API_KEY=YOUR_API_KEY_HERE node server.js");
    process.exit(1);
}

// Helper function to make HTTPS requests (replaces axios.get)
function makeApiRequest(apiPath) {
    return new Promise((resolve, reject) => {
        const options = {
            hostname: API_HOSTNAME,
            path: apiPath,
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${API_KEY}`,
                'User-Agent': 'BrawlStarsRankingsApp/1.0 (No Dependencies Example)' // Good practice to include User-Agent
            }
        };

        const req = https.request(options, (res) => {
            let data = '';

            // A chunk of data has been received.
            res.on('data', (chunk) => {
                data += chunk;
            });

            // The whole response has been received. Parse the data.
            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    try {
                        resolve(JSON.parse(data));
                    } catch (parseError) {
                        reject(new Error(`Failed to parse JSON from ${apiPath}: ${parseError.message}`));
                    }
                } else {
                    // Handle HTTP errors
                     let errorMsg = `HTTP Error ${res.statusCode} fetching ${apiPath}`;
                     try {
                         const errorJson = JSON.parse(data);
                         if(errorJson.message) errorMsg += `: ${errorJson.message}`;
                     } catch {} // Ignore parsing errors on error responses
                     reject(new Error(errorMsg));
                }
            });
        });

        // Handle request errors
        req.on('error', (e) => {
            reject(new Error(`Request Error fetching ${apiPath}: ${e.message}`));
        });

        // End the request (for GET, no body to send)
        req.end();
    });
}

// Create the HTTP server
const server = http.createServer(async (req, res) => {
    const parsedUrl = url.parse(req.url, true);
    const pathname = parsedUrl.pathname;

    console.log(`${req.method} ${pathname}`);

    // Serve static files from the 'public' directory
    if (req.method === 'GET' && pathname.startsWith('/') && pathname !== '/api/brawler-rankings') {
        const filePath = path.join(__dirname, 'public', pathname === '/' ? 'index.html' : pathname);
        const ext = path.extname(filePath);
        const contentType = {
            '.html': 'text/html',
            '.css': 'text/css',
            '.js': 'text/javascript',
            // Add more content types as needed (e.g., for images)
        }[ext] || 'application/octet-stream'; // Default to binary if unknown

        fs.readFile(filePath, (err, data) => {
            if (err) {
                console.error(`Error serving static file ${filePath}:`, err.message);
                res.writeHead(404, { 'Content-Type': 'text/plain' });
                res.end('Not Found');
            } else {
                res.writeHead(200, { 'Content-Type': contentType });
                res.end(data);
            }
        });
        return; // Stop further processing for static files
    }

    // Handle the API endpoint
    if (req.method === 'GET' && pathname === '/api/brawler-rankings') {
        try {
            // 1. Get list of all brawlers
            const brawlersData = await makeApiRequest('/v1/brawlers');
            const brawlers = brawlersData.list;

            const results = {};
            const targetRank = 200;

            // 2. Fetch rankings for each brawler sequentially (to be safer with rate limits)
            // Concurrency with native http/s is more complex, sequential is simpler here.
            // If you need concurrency, you'd manage multiple promises.
            for (const brawler of brawlers) {
                try {
                    console.log(`Fetching rankings for ${brawler.name}...`);
                    const rankingsData = await makeApiRequest(`/v1/rankings/global/brawlers/${brawler.id}`);
                    const rankingList = rankingsData.list;

                    if (rankingList && rankingList.length >= targetRank) {
                        const player = rankingList[targetRank - 1]; // 200th player is at index 199
                         results[brawler.name] = {
                            rank: targetRank,
                            playerTag: player.tag,
                            playerName: player.name,
                            trophies: player.trophies,
                        };
                    } else if (rankingList) {
                         results[brawler.name] = {
                            rank: 'N/A',
                            playerTag: 'N/A',
                            playerName: `Less than ${targetRank} ranked players`,
                            trophies: 'N/A',
                        };
                    } else {
                         results[brawler.name] = {
                            rank: 'Error',
                            playerTag: 'Error',
                            playerName: 'Empty ranking list',
                            trophies: 'Error',
                        };
                    }

                    // Optional: Add a small delay between requests to mitigate rate limits
                    // await new Promise(resolve => setTimeout(resolve, 200)); // Add 200ms delay per brawler

                } catch (rankingError) {
                     console.error(`Error fetching ranking for brawler ${brawler.name} (${brawler.id}):`, rankingError.message);
                     results[brawler.name] = {
                        rank: 'Error',
                        playerTag: 'Error',
                        playerName: `Could not fetch ranking: ${rankingError.message}`,
                        trophies: 'Error',
                    };
                }
            }

            // Send the compiled results back to the frontend
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(results));

        } catch (error) {
            console.error("Error in API endpoint:", error.message);
             // Send an error response to the frontend
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: `Failed to fetch Brawl Stars data: ${error.message}` }));
        }
        return; // Stop further processing
    }

    // Handle 404 for other paths
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not Found');
});

// Start the server
server.listen(port, () => {
    console.log(`Server running at http://localhost:${port}`);
    console.log(`Serving static files from ${path.join(__dirname, 'public')}`);
    console.log(`API Key Status: ${API_KEY ? 'Loaded' : 'Missing - Server will not fetch data!'}`);
});