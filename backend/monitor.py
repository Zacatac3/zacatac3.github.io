"""Local-only backend. Expose it via an HTTPS tunnel; require admin bearer token."""
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from pywebpush import webpush, WebPushException

BASE = Path(__file__).resolve().parent
SUB_FILE = BASE / 'subscription.json'
KEY_FILE = BASE / 'private_key.pem'
TOKEN_FILE = BASE / 'admin_token.txt'
# Set this to your exact GitHub Pages origin if using a custom domain.
ALLOWED_ORIGIN = os.environ.get('DUO_ALLOWED_ORIGIN', 'https://zacatac3.github.io').rstrip('/')
VAPID_SUBJECT = os.environ.get('DUO_VAPID_SUBJECT', 'https://zacatac3.github.io')
ZIP = os.environ.get('DUO_ZIP', '43623')
SKUS = {'White 256GB': 'MK1E4LL/A', 'Blue 256GB (verify)': 'MK1F4LL/A'}
STORES = {'R299': 'Franklin Park', 'R230': 'Briarwood'}
CHECK_INTERVAL_SECONDS = 60
STATUS_INTERVAL_SECONDS = 4 * 60 * 60
EST = timezone(timedelta(hours=-5), 'EST')
HISTORY_LIMIT = 1440

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': [ALLOWED_ORIGIN], 'allow_headers': ['Authorization', 'Content-Type'], 'methods': ['GET', 'POST', 'OPTIONS']}})
lock = threading.Lock()
state = {'running': False, 'completed': 0, 'last_check': None, 'last_error': None,
         'started_at': None, 'last_inventory': None, 'last_notification_at': None,
         'next_status_at': None}
stop_event = threading.Event()
check_history = deque(maxlen=HISTORY_LIMIT)
activity = {'in_flight': None}

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

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
    result.update(check_interval_seconds=CHECK_INTERVAL_SECONDS,
                  status_interval_seconds=STATUS_INTERVAL_SECONDS)
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
                  'started_at': state['started_at']}
    result.update(server_time=utc_now(), check_interval_seconds=CHECK_INTERVAL_SECONDS)
    return jsonify(result)

@app.post('/api/start')
def start():
    if not SUB_FILE.exists(): return jsonify(error='Subscribe your iPhone first'), 400
    if not KEY_FILE.exists(): return jsonify(error='Missing backend/private_key.pem'), 500
    with lock:
        if state['running']: return jsonify(error='Monitor already running', **state), 409
        state.update(running=True, completed=0, last_check=None, last_error=None,
                     started_at=utc_now(), last_inventory=None,
                     last_notification_at=None, next_status_at=None)
        stop_event.clear()
    threading.Thread(target=run_monitor, daemon=True).start()
    return jsonify(ok=True, continuous=True, interval_seconds=CHECK_INTERVAL_SECONDS,
                   status_interval_seconds=STATUS_INTERVAL_SECONDS)

@app.post('/api/stop')
def stop():
    stop_event.set()
    return jsonify(ok=True, stopping=True)

def check_inventory():
    params = [('pl','true'),('mts.0','regular'),('location',ZIP)]
    params += [(f'parts.{i}', sku) for i, sku in enumerate(SKUS.values())]
    started_at = utc_now()
    started = time.monotonic()
    with lock:
        previous_at = check_history[-1]['started_at'] if check_history else None
        activity['in_flight'] = {'started_at': started_at}
    response = None
    inventory = None
    error = None
    try:
        response = requests.get('https://www.apple.com/shop/retail/pickup-message', params=params, timeout=20,
                                headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
        response.raise_for_status()
        inventory = parse_inventory(response.json())
        return inventory
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'[:500]
        raise
    finally:
        entry = {'started_at': started_at, 'completed_at': utc_now(),
                 'duration_ms': round((time.monotonic() - started) * 1000),
                 'http_status': response.status_code if response is not None else None,
                 'ok': inventory is not None, 'inventory': inventory, 'error': error,
                 'gap_seconds': round((datetime.fromisoformat(started_at) -
                                       datetime.fromisoformat(previous_at)).total_seconds(), 1)
                                if previous_at else None}
        with lock:
            check_history.append(entry)
            activity['in_flight'] = None

def parse_inventory(payload):
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
        for color, sku in SKUS.items():
            item=availability.get(sku)
            if not isinstance(item, dict) or not item:
                raise ValueError(f'Apple response missing SKU {sku} at {name}')
            display=item.get('pickupDisplay')
            if display not in ('available','unavailable'):
                raise ValueError(f'Unexpected pickupDisplay {display!r} at {name}/{sku}')
            outcomes[color] = display == 'available'
        inventory[name] = outcomes
    return inventory

def notification_payload(inventory, reason, checked_at):
    checked = datetime.fromisoformat(checked_at).astimezone(EST)
    timestamp = checked.strftime('%Y-%m-%d %I:%M:%S %p EST')
    if inventory is None:
        title = 'Duo monitor needs attention'
        lines = ['Stock could not be verified. Retrying every minute.']
    else:
        in_stock = any(available for items in inventory.values() for available in items.values())
        title = 'Duo is in stock' if in_stock else 'Duo is out of stock'
        lines = []
        for store, items in inventory.items():
            available = [name for name, in_stock in items.items() if in_stock]
            lines.append(f'{store}: ' + (', '.join(available) + ' in stock' if available else 'Out of stock'))
    captions = {'started': 'Monitoring started · Checking every minute',
                'stock_change': 'Stock availability changed',
                'heartbeat': '4-hour status check · Monitoring is active',
                'recovered': 'Inventory checks are working again',
                'error': 'Inventory check failed'}
    lines = [f'Checked: {timestamp}'] + lines + [captions[reason]]
    return {'title': title, 'body': '\n'.join(lines), 'checked_at': checked_at,
            'sent_at': utc_now(), 'reason': reason,
            'check_id': secrets.token_hex(8), 'url': './'}

def send_notification(inventory, reason, checked_at):
    subscription = json.loads(SUB_FILE.read_text())
    payload = notification_payload(inventory, reason, checked_at)
    webpush(subscription_info=subscription, data=json.dumps(payload),
            vapid_private_key=str(KEY_FILE), vapid_claims={'sub': VAPID_SUBJECT}, timeout=20)
    print(f'[{payload["sent_at"]}] Push sent ({reason}): {payload["body"]}', flush=True)
    return payload['sent_at']

def run_monitor():
    last_notified_inventory = None
    last_status_sent = None
    inventory_failed = False
    error_notification_pending = False
    recovery_pending = False
    index = 0
    try:
        while not stop_event.is_set():
            index += 1
            next_check = time.monotonic() + CHECK_INTERVAL_SECONDS
            inventory = None
            error = None
            try:
                inventory = check_inventory()
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                error = f'{type(exc).__name__}: {exc}'
                print(f'[{utc_now()}] Check {index} failed: {error}', flush=True)
            checked_at = utc_now()
            if stop_event.is_set(): break
            now = time.monotonic()
            status_due = last_status_sent is None or now - last_status_sent >= STATUS_INTERVAL_SECONDS
            if inventory is None:
                if not inventory_failed:
                    error_notification_pending = True
                reason = 'error' if error_notification_pending or status_due else None
                inventory_failed = True
                recovery_pending = True
            else:
                inventory_failed = False
                if recovery_pending:
                    reason = 'recovered'
                elif last_notified_inventory is None:
                    reason = 'started'
                elif inventory != last_notified_inventory:
                    reason = 'stock_change'
                else:
                    reason = 'heartbeat' if status_due else None
            if reason:
                try:
                    sent_at = send_notification(inventory, reason, checked_at)
                    if inventory is not None:
                        last_notified_inventory = inventory
                        recovery_pending = False
                    else:
                        error_notification_pending = False
                    with lock:
                        state['last_notification_at'] = sent_at
                        if status_due:
                            last_status_sent = time.monotonic()
                            state['next_status_at'] = (
                                datetime.fromisoformat(sent_at) + timedelta(seconds=STATUS_INTERVAL_SECONDS)
                            ).isoformat(timespec='milliseconds')
                except (WebPushException, OSError, ValueError, requests.RequestException) as exc:
                    push_error = f'{type(exc).__name__}: {exc}'
                    error = f'{error}; push failed: {push_error}' if error else f'Push failed: {push_error}'
                    print(f'[{utc_now()}] {error}', flush=True)
            with lock:
                state.update(completed=index, last_check=checked_at, last_error=error,
                             last_inventory=inventory)
            if stop_event.wait(max(0, next_check - time.monotonic())): break
    finally:
        with lock: state['running']=False
        print('Monitor stopped', flush=True)

if __name__ == '__main__':
    print('Allowed origin:',ALLOWED_ORIGIN)
    print('Admin token (keep private):',ADMIN_TOKEN)
    print('Listening only on 127.0.0.1:8766')
    app.run(host='127.0.0.1',port=8766,debug=False,threaded=True)
