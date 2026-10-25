# Duo stock monitor backend

This runs locally on the Mac, behind your existing HTTPS tunnel. Stock checks
and pushes need the Mac awake, the backend running, and internet access. The
tunnel lets the website reach the backend for controls, status, and the log;
once subscribed, the monitoring loop sends pushes directly to the push service.
The iPhone Home Screen app does not need to remain open or in memory. Its saved
push subscription is used by the Mac backend independently. This is a local Mac
service; monitoring must be started again after restarting the backend or Mac.

- Checks Franklin Park and Briarwood every 60 seconds until stopped.
- Sends a dedicated push when monitoring starts and when it finishes stopping.
  Stopping waits for any active request to finish; repeated stop requests do
  not send duplicate stop pushes. Push delivery failures appear in backend
  status as `last_notification_error` and do not prevent starting or stopping.
- Sends an initial stock status on start, then alerts when any monitored store or
  variant changes availability.
- Every stock notification and activity entry lists both colors at both stores.
  Available combinations appear first and an in-stock push title identifies
  the first available color and store. Check failures show unknown availability.
- Sends a fresh status every four hours whether stock is available or not.
  Stock-change alerts do not postpone the four-hour status check.
- Reports failed checks without claiming stock is unavailable, retries every
  minute, and sends a recovery notification when inventory checks work again.
- Notification bodies and website activity times use `America/New_York` to
  show Eastern Time to the second: EDT during daylight saving time and EST
  during standard time. Start/stop pushes show the time of the event.
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

Apple's pickup response identifies the monitored SKUs as Star White 256GB
(`MK1E4LL/A`) and Night Sky 256GB (`MK1F4LL/A`, the blue option). The website
also recognizes legacy color names in retained history. Store and SKU
configuration is in `monitor.py`.

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
