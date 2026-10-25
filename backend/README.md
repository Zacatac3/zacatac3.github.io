# Duo stock monitor backend

This runs locally on the Mac, behind your existing HTTPS tunnel. Keep the Mac
awake, the backend running, and the tunnel connected for notifications to work.

- Checks Franklin Park and Briarwood every 60 seconds until stopped.
- Sends a status immediately on start, then alerts when any monitored store or
  variant changes availability.
- Sends a fresh status every four hours whether stock is available or not.
  Stock-change alerts do not postpone the four-hour status check.
- Reports failed checks without claiming stock is unavailable, retries every
  minute, and sends a recovery notification when inventory checks work again.
- Notification bodies include the inventory check time to the second in fixed
  EST (UTC−5), including during daylight saving time.
- The website's Apple request activity log refreshes every 15 seconds while
  visible. It records actual Apple requests on the backend while the page is
  closed, including failures, HTTP status, duration, stock results, and gaps
  between requests. Refreshing the page log never triggers an Apple request.
- Only the latest 1,440 requests (about 24 hours) are retained in memory. The
  website can show the latest 120, 360, or 1,440. History survives stopping and
  starting monitoring but clears when the backend process restarts. The log
  starts with this update; earlier requests are not reconstructed.
- Monitoring and its four-hour schedule restart when you restart the backend
  and start monitoring again. Missed checks while the Mac sleeps are not replayed.

The existing blue SKU is still labeled `(verify)`; confirm it before relying on
blue availability. Store and SKU configuration is in `monitor.py`.

Run from the repository root using the existing environment:

```sh
backend/.venv/bin/python backend/monitor.py
```

On the iPhone, reopen the updated Home Screen app, connect with the same tunnel
URL and admin token, and tap **Start monitoring**. **Stop monitoring** pauses
checks and notifications. The status endpoint includes the most recent stock
result, notification time, and the next four-hour status time in UTC.

The private key, admin token, subscription, and virtual environment remain
ignored by Git. Only backend source, requirements, documentation, and tests
are allowed into version control. Keep backend secrets off GitHub Pages.

Run the scheduling and notification checks without network access or pushes:

```sh
cd backend
.venv/bin/python -m unittest -v
```
