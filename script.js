const http = require('http');
const https = require('https');
const url = require('url');

// !!! WARNING: API Key is hardcoded for temporary testing.
// !!! DO NOT use this in production or share this file publicly.
const BRAWL_STARS_API_KEY = 'YOUR_API_KEY_HERE'; // <-- REPLACE WITH YOUR ACTUAL API KEY

if (BRAWL_STARS_API_KEY === 'YOUR_API_KEY_HERE' || !BRAWL_STARS_API_KEY) {
    console.error("!!! WARNING: Please replace 'YOUR_API_KEY_HERE' with your actual Brawl Stars API Key.");
    console.error("!!! This method of storing the key is INSECURE and for local testing only.");
}

const port = process.env.PORT || 3000;
const BASE_URL = 'https://api.brawlstars.com/v1';
const API_HOSTNAME = 'api.brawlstars.com';

// Helper function to make HTTPS requests (replaces axios.get)
function makeApiRequest(apiPath) {
    return new Promise((resolve, reject) => {
        const options = {
            hostname: API_HOSTNAME,
            path: apiPath,
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${BRAWL_STARS_API_KEY}`,
                'User-Agent': 'BrawlStarsRankingsApp/1.0 (Single Script Example)'
            }
        };

        const req = https.request(options, (res) => {
            let data = '';

            res.on('data', (chunk) => {
                data += chunk;
            });

            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    try {
                        resolve(JSON.parse(data));
                    } catch (parseError) {
                        reject(new Error(`Failed to parse JSON from ${apiPath}: ${parseError.message}`));
                    }
                } else {
                     let errorMsg = `HTTP Error ${res.statusCode} fetching ${apiPath}`;
                     try {
                         const errorJson = JSON.parse(data);
                         if(errorJson.message) errorMsg += `: ${errorJson.message}`;
                     } catch {} // Ignore parsing errors on error responses
                     const error = new Error(errorMsg);
                     error.statusCode = res.statusCode; // Attach status code
                     reject(error);
                }
            });
        });

        req.on('error', (e) => {
            reject(new Error(`Request Error fetching ${apiPath}: ${e.message}`));
        });

        req.end();
    });
}

// Create the HTTP server
const server = http.createServer(async (req, res) => {
    const parsedUrl = url.parse(req.url, true);
    const pathname = parsedUrl.pathname;

    console.log(`${req.method} ${pathname}`);

    // We only handle the API endpoint in this simplified version
    if (req.method === 'GET' && pathname === '/api/brawler-rankings') {
        try {
            // 1. Get list of all brawlers
            const brawlersData = await makeApiRequest('/v1/brawlers');
            const brawlers = brawlersData.list;

            const results = {};
            const targetRank = 200;

             // Fetch rankings for each brawler sequentially to be safer with rate limits
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

            // Send the compiled results back as JSON
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(results));

        } catch (error) {
            console.error("Error in API endpoint:", error.message);
            // Send an error response
            res.writeHead(error.statusCode || 500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: `Failed to fetch Brawl Stars data: ${error.message}` }));
        }
        return; // Stop further processing
    }

    // Handle requests for anything else (like '/' or non-API paths)
    res.writeHead(200, { 'Content-Type': 'text/plain' }); // Just send plain text for simplicity
    res.end('Visit /api/brawler-rankings in your browser or a tool like Postman to see the data.');

});

// Start the server
server.listen(port, () => {
    console.log(`Server running at http://localhost:${port}`);
    console.log('API endpoint available at http://localhost:3000/api/brawler-rankings');
    console.log('!!! REMINDER: API Key is hardcoded - for local testing only!');
});