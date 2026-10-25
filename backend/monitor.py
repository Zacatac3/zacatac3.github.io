"""Local-only backend. Expose it via an HTTPS tunnel; require admin bearer token."""
import json
import math
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from pywebpush import webpush, WebPushException

BASE = Path(__file__).resolve().parent
SUB_FILE = BASE / 'subscription.json'
KEY_FILE = BASE / 'private_key.pem'
TOKEN_FILE = BASE / 'admin_token.txt'
COOLDOWN_FILE = BASE / 'cooldown.json'
POLLING_FILE = BASE / 'polling.json'
# Set this to your exact GitHub Pages origin if using a custom domain.
ALLOWED_ORIGIN = os.environ.get('DUO_ALLOWED_ORIGIN', 'https://zacatac3.github.io').rstrip('/')
VAPID_SUBJECT = os.environ.get('DUO_VAPID_SUBJECT', 'https://zacatac3.github.io')
ZIP = os.environ.get('DUO_ZIP', '43623')
SKUS = {'Star White 256GB': 'MK1E4LL/A', 'Night Sky 256GB': 'MK1F4LL/A'}
TEST_SKUS = {'iPhone 18 Pro 256GB Burgundy': 'MJQ54LL/A'}
LEGACY_COLORS = {'White 256GB': 'Star White 256GB', 'Blue 256GB (verify)': 'Night Sky 256GB'}
STORES = {'R299': 'Franklin Park', 'R230': 'Briarwood'}
CHECK_INTERVAL_SECONDS = 30
POLLING_MIN_SECONDS = 30
POLLING_MAX_SECONDS = 300
POLLING_STEP_SECONDS = 15
STATUS_INTERVAL_SECONDS = 4 * 60 * 60
EASTERN = ZoneInfo('America/New_York')
HISTORY_LIMIT = 1440
BLOCK_STATUSES = {403, 429, 541}
COOLDOWN_SECONDS = (15 * 60, 30 * 60, 60 * 60)

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': [ALLOWED_ORIGIN], 'allow_headers': ['Authorization', 'Content-Type'], 'methods': ['GET', 'POST', 'OPTIONS']}})
lock = threading.Lock()
polling_save_lock = threading.Lock()
state = {'running': False, 'completed': 0, 'last_check': None, 'last_error': None,
         'started_at': None, 'last_inventory': None, 'last_notification_at': None,
         'next_status_at': None, 'last_notification_error': None, 'stopped_at': None,
         'test_mode': False, 'mode_revision': 0, 'last_push': None,
         'cooldown_until': None, 'cooldown_attempts': 0, 'cooldown_status': None,
         'cooldown_persistence_error': None, 'check_interval_seconds': CHECK_INTERVAL_SECONDS,
         'polling_fallback': False, 'polling_persistence_error': None}
stop_event = threading.Event()
check_history = deque(maxlen=HISTORY_LIMIT)
activity = {'in_flight': None}

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def mode_details(test_mode):
    return {'test_mode': test_mode, 'mode_label': 'iPhone 18 Pro test' if test_mode else 'Duo',
            'skus': TEST_SKUS if test_mode else SKUS}

def polling_info(snapshot=None):
    if snapshot is None:
        with lock:
            snapshot = dict(state)
    return {key: snapshot[key] for key in ('check_interval_seconds', 'polling_fallback', 'polling_persistence_error')}

def valid_polling_interval(value):
    return type(value) is int and POLLING_MIN_SECONDS <= value <= POLLING_MAX_SECONDS and value % POLLING_STEP_SECONDS == 0

def persist_polling():
    with polling_save_lock:
        return save_polling_snapshot()

def save_polling_snapshot():
    data = polling_info()
    temporary = POLLING_FILE.with_suffix('.tmp')
    try:
        with open(temporary, 'w') as handle:
            os.chmod(temporary, 0o600)
            json.dump(data, handle)
        temporary.replace(POLLING_FILE)
        return True
    except OSError as exc:
        with lock:
            state['polling_persistence_error'] = str(exc)
        print('Could not persist polling rate:', type(exc).__name__, flush=True)
        return False

def load_polling():
    if not POLLING_FILE.exists():
        return
    try:
        data = json.loads(POLLING_FILE.read_text())
        if not valid_polling_interval(data['check_interval_seconds']) or type(data['polling_fallback']) is not bool:
            raise ValueError('Invalid saved polling rate')
        with lock:
            state.update(check_interval_seconds=data['check_interval_seconds'], polling_fallback=data['polling_fallback'])
    except (OSError, ValueError, KeyError, TypeError):
        with lock:
            state.update(check_interval_seconds=60, polling_fallback=True,
                         polling_persistence_error='Saved polling rate could not be loaded; using 60 seconds.')

def cooldown_info(snapshot=None):
    if snapshot is None:
        with lock:
            snapshot = dict(state)
    until = snapshot.get('cooldown_until')
    remaining = max(0, math.ceil((datetime.fromisoformat(until) -
                                 datetime.fromisoformat(utc_now())).total_seconds())) if until else 0
    return {'cooldown_until': until, 'cooldown_remaining_seconds': remaining,
            'cooldown_active': remaining > 0, 'cooldown_attempts': snapshot.get('cooldown_attempts', 0),
            'cooldown_status': snapshot.get('cooldown_status'),
            'cooldown_persistence_error': snapshot.get('cooldown_persistence_error')}

def persist_cooldown(data):
    temporary = COOLDOWN_FILE.with_suffix('.tmp')
    try:
        with open(temporary, 'w') as handle:
            os.chmod(temporary, 0o600)
            json.dump(data, handle)
        temporary.replace(COOLDOWN_FILE)
    except OSError as exc:
        with lock:
            state['cooldown_persistence_error'] = str(exc)
        print('Could not persist Apple cooldown:', type(exc).__name__, flush=True)

def begin_cooldown(status_code, retry_after=None):
    now = datetime.fromisoformat(utc_now())
    with lock:
        attempt = state['cooldown_attempts']
    seconds = COOLDOWN_SECONDS[min(attempt, len(COOLDOWN_SECONDS) - 1)]
    if retry_after:
        try:
            seconds = max(seconds, int(retry_after))
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = max(seconds, math.ceil((date - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    data = {'cooldown_until': (now + timedelta(seconds=seconds)).isoformat(timespec='milliseconds'),
            'cooldown_attempts': attempt + 1, 'cooldown_status': status_code}
    with lock:
        fallback = state['check_interval_seconds'] < 60
        state.update(**data, cooldown_persistence_error=None,
                     check_interval_seconds=max(60, state['check_interval_seconds']),
                     polling_fallback=state['polling_fallback'] or fallback, polling_persistence_error=None)
        if check_history:
            check_history[-1] = {**check_history[-1], 'cooldown_until': data['cooldown_until']}
    persist_cooldown(data)
    persist_polling()
    return cooldown_info()

def clear_cooldown():
    with lock:
        state.update(cooldown_until=None, cooldown_attempts=0, cooldown_status=None,
                     cooldown_persistence_error=None)
    if not persist_polling():
        return
    try:
        COOLDOWN_FILE.unlink(missing_ok=True)
    except OSError as exc:
        with lock:
            state['cooldown_persistence_error'] = str(exc)

def load_cooldown():
    if not COOLDOWN_FILE.exists():
        return
    try:
        data = json.loads(COOLDOWN_FILE.read_text())
        deadline = datetime.fromisoformat(data['cooldown_until'])
        if deadline.tzinfo is None or not isinstance(data['cooldown_attempts'], int) or data['cooldown_attempts'] < 1 or data['cooldown_status'] not in BLOCK_STATUSES:
            raise ValueError('Invalid cooldown data')
        with lock:
            state.update({key: data[key] for key in ('cooldown_until', 'cooldown_attempts', 'cooldown_status')})
            fallback = state['check_interval_seconds'] < 60
            state.update(check_interval_seconds=max(60, state['check_interval_seconds']),
                         polling_fallback=state['polling_fallback'] or fallback)
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt saved block must not silently permit immediate retries.
        begin_cooldown(541)

load_polling()
load_cooldown()

def get_token():
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(36) + '\n')
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()

ADMIN_TOKEN = get_token()

@app.before_request
def guard():
    if request.method == 'OPTIONS':
        return '', 204

    if request.path.startswith('/api/'):
        provided = request.headers.get('Authorization', '')
        if not secrets.compare_digest(provided, 'Bearer ' + ADMIN_TOKEN):
            return jsonify(error='Unauthorized'), 401

@app.after_request
def no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.post('/api/subscribe')
def subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '')
    parsed = urlparse(endpoint)
    keys = data.get('keys') or {}
    if parsed.scheme != 'https' or not parsed.hostname or not all(isinstance(keys.get(k), str) and keys[k] for k in ('p256dh', 'auth')):
        return jsonify(error='Invalid push subscription'), 400
    SUB_FILE.write_text(json.dumps({'endpoint': endpoint, 'keys': keys}, indent=2))
    SUB_FILE.chmod(0o600)
    return jsonify(ok=True, subscribed=True)

@app.get('/api/status')
def status():
    with lock: result = dict(state)
    result['subscribed'] = SUB_FILE.exists()
    result.update(status_interval_seconds=STATUS_INTERVAL_SECONDS)
    result.update(polling_info(result))
    result.update(mode_details(result['test_mode']))
    result.update(cooldown_info(result))
    return jsonify(result)

@app.get('/api/activity')
def recent_activity():
    try:
        limit = int(request.args.get('limit', '120'))
    except ValueError:
        return jsonify(error='Log limit must be a number'), 400
    if not 1 <= limit <= HISTORY_LIMIT:
        return jsonify(error=f'Log limit must be between 1 and {HISTORY_LIMIT}'), 400
    with lock:
        retained = list(check_history)
        result = {'entries': retained[-limit:][::-1], 'retained_count': len(retained),
                  'capacity': HISTORY_LIMIT, 'success_count': sum(row['ok'] for row in retained),
                  'failure_count': sum(not row['ok'] for row in retained),
                  'in_flight': activity['in_flight'], 'running': state['running'],
                  'started_at': state['started_at'], 'last_push': state['last_push'],
                  **polling_info(dict(state)),
                  **cooldown_info(dict(state)),
                  **mode_details(state['test_mode'])}
    result.update(server_time=utc_now())
    return jsonify(result)

@app.post('/api/polling')
def set_polling():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or ('interval_seconds' in data) == ('delta_seconds' in data):
        return jsonify(error='Provide interval_seconds or delta_seconds'), 400
    if 'delta_seconds' in data and (type(data['delta_seconds']) is not int or data['delta_seconds'] not in (-15, 15)):
        return jsonify(error='delta_seconds must be -15 or 15'), 400
    with lock:
        interval = state['check_interval_seconds'] + data['delta_seconds'] if 'delta_seconds' in data else data['interval_seconds']
        if not valid_polling_interval(interval):
            return jsonify(error='Polling must be 30–300 seconds in 15-second increments'), 400
        if state['cooldown_attempts'] and interval < 60:
            return jsonify(error='Wait for a successful cooldown recovery check before selecting a rate below 60 seconds.',
                           **polling_info(dict(state)), **cooldown_info(dict(state))), 409
        state.update(check_interval_seconds=interval, polling_fallback=False,
                     polling_persistence_error=None)
    persist_polling()
    return jsonify(ok=True, **polling_info(), **cooldown_info(),
                   message='Rate saved for upcoming scheduled checks; no extra inventory check was triggered.')

@app.post('/api/mode')
def set_mode():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('test_mode'), bool):
        return jsonify(error='test_mode must be true or false'), 400
    with lock:
        changed = state['test_mode'] != data['test_mode']
        if changed:
            state['test_mode'] = data['test_mode']
            state['mode_revision'] += 1
            state['last_inventory'] = None
        result = mode_details(state['test_mode'])
    return jsonify(ok=True, changed=changed, **result,
                   **cooldown_info(), message='Mode applies on the next permitted stock check; it does not bypass an Apple cooldown.')

@app.post('/api/start')
def start():
    if not SUB_FILE.exists(): return jsonify(error='Subscribe your iPhone first'), 400
    if not KEY_FILE.exists(): return jsonify(error='Missing backend/private_key.pem'), 500
    with lock:
        if state['running']: return jsonify(error='Monitor already running', **state), 409
        state.update(running=True, completed=0, last_check=None, last_error=None,
                     started_at=utc_now(), last_inventory=None,
                     last_notification_at=None, next_status_at=None,
                     last_notification_error=None, stopped_at=None)
        stop_event.clear()
    threading.Thread(target=run_monitor, daemon=True).start()
    return jsonify(ok=True, continuous=True, interval_seconds=polling_info()['check_interval_seconds'],
                   status_interval_seconds=STATUS_INTERVAL_SECONDS, **cooldown_info())

@app.post('/api/stop')
def stop():
    with lock:
        if not state['running']:
            return jsonify(ok=True, stopping=False, already_stopped=True)
        already_stopping = stop_event.is_set()
        stop_event.set()
    return jsonify(ok=True, stopping=True, already_stopping=already_stopping)

def check_inventory(test_mode=False):
    skus = TEST_SKUS if test_mode else SKUS
    params = [('pl','true'),('mts.0','regular'),('location',ZIP)]
    params += [(f'parts.{i}', sku) for i, sku in enumerate(skus.values())]
    started_at = utc_now()
    started = time.monotonic()
    with lock:
        previous_at = check_history[-1]['started_at'] if check_history else None
        activity['in_flight'] = {'started_at': started_at, 'test_mode': test_mode}
    response = None
    inventory = None
    error = None
    try:
        response = requests.get('https://www.apple.com/shop/retail/pickup-message', params=params, timeout=20,
                                headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
        response.raise_for_status()
        inventory = parse_inventory(response.json(), skus)
        return inventory
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'[:500]
        raise
    finally:
        entry = {'started_at': started_at, 'completed_at': utc_now(),
                 'duration_ms': round((time.monotonic() - started) * 1000),
                 'http_status': response.status_code if response is not None else None,
                 'ok': inventory is not None, 'inventory': inventory, 'error': error,
                 'test_mode': test_mode, 'skus': skus,
                 'gap_seconds': round((datetime.fromisoformat(started_at) -
                                       datetime.fromisoformat(previous_at)).total_seconds(), 1)
                                if previous_at else None}
        with lock:
            check_history.append(entry)
            activity['in_flight'] = None

def parse_inventory(payload, skus=None):
    if skus is None:
        skus = SKUS
    if not isinstance(payload, dict) or not isinstance(payload.get('head'), dict):
        raise ValueError('Invalid Apple inventory response')
    if str(payload['head'].get('status')) != '200':
        raise ValueError('Unexpected Apple API status: '+str(payload.get('head')))
    body = payload.get('body')
    if not isinstance(body, dict) or not isinstance(body.get('stores'), list):
        raise ValueError('Apple response missing stores')
    if not all(isinstance(store, dict) for store in body['stores']):
        raise ValueError('Invalid Apple store response')
    stores = {s.get('storeNumber'): s for s in body['stores']}
    inventory = {}
    for store_id, name in STORES.items():
        if store_id not in stores: raise ValueError(f'Apple response missing {name}')
        availability = stores[store_id].get('partsAvailability',{})
        if not isinstance(availability, dict):
            raise ValueError(f'Invalid Apple availability at {name}')
        outcomes = {}
        for color, sku in skus.items():
            item=availability.get(sku)
            if not isinstance(item, dict) or not item:
                raise ValueError(f'Apple response missing SKU {sku} at {name}')
            display=item.get('pickupDisplay')
            if display not in ('available','unavailable'):
                raise ValueError(f'Unexpected pickupDisplay {display!r} at {name}/{sku}')
            outcomes[color] = display == 'available'
        inventory[name] = outcomes
    return inventory

def stock_options(inventory, test_mode=False):
    normalized = {store: {LEGACY_COLORS.get(color, color): available
                          for color, available in items.items()}
                  for store, items in (inventory or {}).items()}
    options = [{'store': store, 'color': color,
                'available': normalized.get(store, {}).get(color)}
               for store in STORES.values() for color in (TEST_SKUS if test_mode else SKUS)]
    return sorted(options, key=lambda option: option['available'] is not True)

SHORT_COLORS = {'Star White 256GB': 'White', 'Night Sky 256GB': 'Blue',
                'iPhone 18 Pro 256GB Burgundy': 'Burgundy'}

def compact_stock_lines(inventory, test_mode=False):
    options = stock_options(inventory, test_mode)
    stores = sorted(STORES.values(), key=lambda store: not any(
        option['store'] == store and option['available'] is True for option in options))
    lines = []
    for store in stores:
        by_color = {option['color']: option['available'] for option in options if option['store'] == store}
        if all(value is True for value in by_color.values()):
            colors = '+'.join(SHORT_COLORS[color] for color in by_color)
            lines.append(f'{store}: {colors} IN STOCK')
            continue
        statuses = []
        for color in (TEST_SKUS if test_mode else SKUS):
            available = by_color[color]
            status = 'IN STOCK' if available is True else 'OOS' if available is False else '?'
            statuses.append(f'{SHORT_COLORS[color]} {status}')
        lines.append(f'{store}: ' + ', '.join(statuses))
    return lines

def notification_payload(inventory, reason, checked_at, test_mode=False):
    checked = datetime.fromisoformat(checked_at).astimezone(EASTERN)
    timestamp = checked.strftime('%Y-%m-%d %I:%M:%S %p %Z')
    if reason in ('started', 'stopped'):
        title = f'Duo monitoring {reason}'
        lines = [timestamp]
        if reason == 'started' and state.get('cooldown_until'):
            info = cooldown_info()
            if info['cooldown_active']:
                retry = datetime.fromisoformat(info['cooldown_until']).astimezone(EASTERN)
                lines.append(retry.strftime('Inventory paused until %I:%M:%S %p %Z'))
    elif reason == 'cooldown':
        info = cooldown_info()
        retry = datetime.fromisoformat(info['cooldown_until']).astimezone(EASTERN)
        title = 'Duo · Apple cooldown'
        lines = [f'HTTP {info["cooldown_status"]} · Stock unknown',
                 retry.strftime('Retry: %Y-%m-%d %I:%M:%S %p %Z'), timestamp]
    elif inventory is None:
        title = 'Duo · Check failed'
        lines = compact_stock_lines(None, test_mode) + [timestamp]
    else:
        options = stock_options(inventory, test_mode)
        available = [option for option in options if option['available'] is True]
        title = 'Duo · 4h status · OOS' if reason == 'heartbeat' else 'Duo · Out of stock'
        if available:
            first = available[0]
            prefix = 'IN STOCK' if test_mode else '🚨 DUO IN STOCK'
            title = f'{prefix}: {SHORT_COLORS[first["color"]]} · {first["store"]}'
        lines = compact_stock_lines(inventory, test_mode) + [timestamp]
    if test_mode:
        title = 'TEST 18 Pro · ' + title.removeprefix('Duo · ').removeprefix('Duo ')
    return {'title': title, 'body': '\n'.join(lines), 'checked_at': checked_at,
            'sent_at': utc_now(), 'reason': reason,
            'check_id': secrets.token_hex(8), 'url': './', **mode_details(test_mode)}

def send_notification(inventory, reason, checked_at, test_mode=False):
    subscription = json.loads(SUB_FILE.read_text())
    payload = notification_payload(inventory, reason, checked_at, test_mode)
    in_stock = inventory is not None and any(
        available for items in inventory.values() for available in items.values())
    urgency = 'high' if in_stock else 'normal'
    started = time.monotonic()
    response = webpush(subscription_info=subscription, data=json.dumps(payload),
                       vapid_private_key=str(KEY_FILE), vapid_claims={'sub': VAPID_SUBJECT},
                       timeout=20, headers={'Urgency': urgency})
    accepted_at = utc_now()
    duration_ms = round((time.monotonic() - started) * 1000)
    timing = {'checked_at': checked_at, 'submitted_at': payload['sent_at'],
              'accepted_at': accepted_at, 'submission_ms': duration_ms,
              'urgency': urgency, 'http_status': response.status_code,
              'reason': reason, 'test_mode': test_mode}
    with lock:
        state['last_push'] = timing
        if (reason not in ('started', 'stopped') and check_history
                and check_history[-1].get('completed_at', '') <= checked_at
                and check_history[-1].get('test_mode', False) == test_mode):
            check_history[-1] = {**check_history[-1], 'push': timing}
    print(f'[{accepted_at}] Push accepted ({reason}, {urgency}, {duration_ms} ms): {payload["body"]}', flush=True)
    return accepted_at

def send_lifecycle_notification(reason, event_at, test_mode=False):
    try:
        sent_at = send_notification(None, reason, event_at, test_mode)
    except (WebPushException, OSError, ValueError, requests.RequestException) as exc:
        error = f'{type(exc).__name__}: {exc}'
        with lock:
            state['last_notification_error'] = error
        print(f'[{utc_now()}] {reason.capitalize()} push failed: {error}', flush=True)
    else:
        with lock:
            state.update(last_notification_at=sent_at, last_notification_error=None)

def run_monitor():
    last_notified_inventory = None
    last_status_sent = None
    inventory_failed = False
    error_notification_pending = False
    recovery_pending = False
    index = 0
    last_mode = None
    last_revision = None
    last_request_started = None
    try:
        with lock:
            test_mode = state['test_mode']
        send_lifecycle_notification('started', utc_now(), test_mode)
        while not stop_event.is_set():
            remaining = cooldown_info()['cooldown_remaining_seconds']
            if remaining:
                if stop_event.wait(remaining): break
                continue
            with lock:
                test_mode = state['test_mode']
                revision = state['mode_revision']
                interval = state['check_interval_seconds']
            if last_request_started is not None:
                wait_remaining = last_request_started + interval - time.monotonic()
                if wait_remaining > 0:
                    if stop_event.wait(wait_remaining): break
                    continue
            if last_mode != test_mode or last_revision != revision:
                last_notified_inventory = None
                inventory_failed = False
                error_notification_pending = False
                recovery_pending = False
                last_mode = test_mode
                last_revision = revision
            index += 1
            check_started = time.monotonic()
            last_request_started = check_started
            next_check = check_started + interval
            inventory = None
            error = None
            cooldown_started = False
            try:
                inventory = check_inventory(test_mode)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                error = f'{type(exc).__name__}: {exc}'
                with lock:
                    fallback = state['check_interval_seconds'] < 60
                    if fallback:
                        state.update(check_interval_seconds=60, polling_fallback=True, polling_persistence_error=None)
                if fallback:
                    persist_polling()
                    next_check = check_started + 60
                response = getattr(exc, 'response', None)
                with lock:
                    previous_block = state['cooldown_status'] if state['cooldown_attempts'] else None
                blocked_status = response.status_code if response is not None and response.status_code in BLOCK_STATUSES else previous_block
                if blocked_status:
                    begin_cooldown(blocked_status, response.headers.get('Retry-After') if response is not None else None)
                    cooldown_started = True
                print(f'[{utc_now()}] Check {index} failed: {error}', flush=True)
            else:
                if state['cooldown_attempts']:
                    clear_cooldown()
                    next_check = check_started + polling_info()['check_interval_seconds']
            checked_at = utc_now()
            if stop_event.is_set(): break
            with lock:
                mode_changed = revision != state['mode_revision']
            if mode_changed and not cooldown_started:
                # Keep the old request in history under its original mode, then
                # check the new mode without sending a stale-mode stock alert.
                last_mode = None
                continue
            now = time.monotonic()
            status_due = last_status_sent is None or now - last_status_sent >= STATUS_INTERVAL_SECONDS
            if inventory is None:
                if not inventory_failed:
                    error_notification_pending = True
                reason = 'cooldown' if cooldown_started else 'error' if error_notification_pending or status_due else None
                inventory_failed = True
                recovery_pending = True
            else:
                inventory_failed = False
                if recovery_pending:
                    reason = 'recovered'
                elif last_notified_inventory is None:
                    reason = 'initial_status'
                elif inventory != last_notified_inventory:
                    reason = 'stock_change'
                elif any(available for items in inventory.values() for available in items.values()):
                    reason = 'in_stock'
                else:
                    reason = 'heartbeat' if status_due else None
            if reason:
                try:
                    sent_at = send_notification(inventory, reason, checked_at, test_mode)
                    if inventory is not None:
                        last_notified_inventory = inventory
                        recovery_pending = False
                    else:
                        error_notification_pending = False
                    with lock:
                        state['last_notification_at'] = sent_at
                        state['last_notification_error'] = None
                        if status_due:
                            last_status_sent = time.monotonic()
                            state['next_status_at'] = (
                                datetime.fromisoformat(sent_at) + timedelta(seconds=STATUS_INTERVAL_SECONDS)
                            ).isoformat(timespec='milliseconds')
                except (WebPushException, OSError, ValueError, requests.RequestException) as exc:
                    push_error = f'{type(exc).__name__}: {exc}'
                    error = f'{error}; push failed: {push_error}' if error else f'Push failed: {push_error}'
                    with lock:
                        state['last_notification_error'] = push_error
                    print(f'[{utc_now()}] {error}', flush=True)
            with lock:
                if revision == state['mode_revision'] or cooldown_started:
                    state.update(completed=index, last_check=checked_at, last_error=error,
                                 last_inventory=inventory)
            wait_seconds = cooldown_info()['cooldown_remaining_seconds'] or max(0, next_check - time.monotonic())
            if stop_event.wait(wait_seconds): break
    finally:
        stopped_at = utc_now()
        with lock:
            test_mode = state['test_mode']
        send_lifecycle_notification('stopped', stopped_at, test_mode)
        with lock:
            state.update(running=False, stopped_at=stopped_at, next_status_at=None)
        print('Monitor stopped', flush=True)

if __name__ == '__main__':
    print('Allowed origin:',ALLOWED_ORIGIN)
    print('Admin token (keep private):',ADMIN_TOKEN)
    print('Listening only on 127.0.0.1:8766')
    app.run(host='127.0.0.1',port=8766,debug=False,threaded=True)
