# Duo stock monitor backend

This runs locally on the Mac, behind your existing HTTPS tunnel. Stock checks
and pushes need the Mac awake, the backend running, and internet access. The
tunnel lets the website reach the backend for controls, status, and the log;
once subscribed, the monitoring loop sends pushes directly to the push service.
The iPhone Home Screen app does not need to remain open or in memory. Its saved
push subscription is used by the Mac backend independently. This is a local Mac
service; monitoring must be started again after restarting the backend or Mac.

- The PWA's **−15s / +15s** controls adjust polling from 30 through 300
  seconds in 15-second steps. Changes save on the Mac and do not trigger an
  extra inventory request. Increasing the rate interval extends a pending wait
  as needed; decreases take effect after the pending scheduled check.
- Any failed inventory check raises a selected interval below 60 seconds to 60.
  Rates of 60 or higher are preserved on failure and after cooldown recovery.
  The existing 15/30/60-minute block backoff is unchanged. During cooldown you
  may select 60 seconds or slower without changing the deadline; faster rates
  remain unavailable until successful recovery. The selected interval persists
  across restarts in ignored `backend/polling.json`.
- Sends a dedicated push when monitoring starts and when it finishes stopping.
  Stopping waits for any active request to finish; repeated stop requests do
  not send duplicate stop pushes. Push delivery failures appear in backend
  status as `last_notification_error` and do not prevent starting or stopping.
- Sends an initial stock status on start, then a distinct push on every successful
  check while any monitored variant is in stock, even if availability is unchanged.
  Also sends stock-change alerts, including when the last available option sells
  out. This applies in both Duo and test mode. Repeated in-stock pushes do not
  postpone the four-hour status schedule. The selected-interval samples and request log
  show the observed availability window, rather than its exact start/end seconds.
- Stock notifications show one compact line per store, followed by the Eastern
  timestamp: `Franklin Park: White IN STOCK, Blue OOS`. Both available colors
  share the compact label `White+Blue IN STOCK`. Storage is omitted, `OOS`
  means out of stock, and `?` means the check failed. Only real Duo availability
  gets a `🚨 DUO IN STOCK` title; startup, OOS, error, and test pushes do not. Test mode
  uses Burgundy and a `TEST 18 Pro` title. The website log retains full labels.
  An in-stock push title identifies the first available color and store.
- Sends a fresh status every four hours whether stock is available or not.
  Stock-change alerts do not postpone the four-hour status check.
- Requests `Urgency: high` for every positive stock push, including test-mode
  positives. Other pushes use normal urgency. High urgency asks the push service
  to attempt immediate delivery; it cannot guarantee arrival time or override
  iPhone Focus, notification permissions, or Scheduled Summary settings.
- The website and status endpoint show the last successful push's submission
  duration and actual acceptance time. Acceptance means the push service
  accepted the request, not that the phone displayed it. Older log timestamps
  marked `Push sent` were taken before submission completed and do not measure
  phone delivery latency. The polling interval is 30–300 seconds, which can
  add up to that interval of detection delay before push delivery begins.
- HTTP 541, 403, and 429 trigger a quiet period with no Apple inventory requests:
  15 minutes after the first blocked response, then 30 minutes and 60 minutes
  after subsequent failed recovery probes, including timeouts or invalid
  responses that do not prove recovery. Longer `Retry-After` values are
  respected. Only one check is made after each quiet period. A successful check
  clears the backoff and resumes at least 60-second polling, preserving a slower selected rate, with a recovery push.
- Cooldown deadlines and consecutive block counts are saved in the ignored
  `backend/cooldown.json`. Stop/start, test-mode changes, and backend restarts
  cannot reset or bypass the wait. Stopping interrupts the wait immediately,
  without deleting the saved cooldown. Other tools on the same IP are outside
  this monitor's control.
- The website shows Cooling down, a countdown, the next permitted retry, and
  the block that started each quiet period in the log. It sends one cooldown
  push per blocked attempt; it does not make inventory requests just to refresh
  the page. The timing policy is conservative: Apple's exact first-versus-last
  request block expiry is not known or guaranteed.
- Other failed checks remain unknown, normally retry at the selected rate, and send a
  recovery notification when inventory checks work again.
- Notification bodies and website activity times use `America/New_York` to
  show Eastern Time to the second: EDT during daylight saving time and EST
  during standard time. Start/stop pushes show the time of the event.
- The website's Apple request activity log refreshes every 15 seconds while
  visible. It records actual Apple requests on the backend while the page is
  closed, including failures, HTTP status, duration, stock results, and gaps
  between requests. Refreshing the page log never triggers an Apple request.
- Only the latest 1,440 requests (about 12–120 hours at the selected rate) are retained in memory. The
  website can show the latest 120, 360, or 1,440. History survives stopping and
  starting monitoring but clears when the backend process restarts. The log
  starts with this update; earlier requests are not reconstructed.
- Monitoring and its four-hour schedule restart when you restart the backend
  and start monitoring again. Missed checks while the Mac sleeps are not replayed.

Apple's pickup response identifies the monitored SKUs as Star White 256GB
(`MK1E4LL/A`) and Night Sky 256GB (`MK1F4LL/A`, the blue option). The website
also recognizes legacy color names in retained history. Store and SKU
configuration is in `monitor.py`.

The **Test with iPhone 18 Pro** switch replaces the Duo request with SKU
`MJQ54LL/A` (iPhone 18 Pro 256GB Burgundy), at the same two stores. While
monitoring is running, the next scheduled check (at the selected polling rate) sends a
fresh status push for the selected mode even if stock has not changed. Test
notifications say `TEST`, and each history entry keeps its own mode label.
Turning the switch off restores both Duo colors. This switch affects the Mac
backend globally, rather than just the open browser. The backend defaults to
Duo mode after a restart. Switching modes while stopped does not start checks;
tap **Start monitoring** when ready.

Run from the repository root using the existing environment:

```sh
backend/.venv/bin/python backend/monitor.py
```

On the iPhone, reopen the updated Home Screen app, connect with the same tunnel
URL and admin token, and tap **Start monitoring**. **Stop monitoring** pauses
checks and notifications. The status endpoint includes the most recent stock
result, notification time, and the next four-hour status time in UTC.

The website saves the tunnel URL and admin token together in this device's
local storage when you save or use the connection. It restores them and loads
the log when you reopen the app. Edit/save the fields to change computers or
tunnels; **Forget saved connection** removes the local pair without stopping
the backend or unsubscribing push. Clearing website data also clears the saved
connection. If device storage is unavailable, the page reports that the
connection can only be used for the current session.

The private key, admin token, subscription, and virtual environment remain
ignored by Git. Only backend source, requirements, documentation, and tests
are allowed into version control. Keep backend secrets off GitHub Pages.

Run the scheduling and notification checks without network access or pushes:

```sh
cd backend
.venv/bin/python -m unittest -v
```
