"""Exercise scheduling without waiting four hours or sending real pushes."""
import copy
import io
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import monitor


OUT = {name: {color: False for color in monitor.SKUS} for name in monitor.STORES.values()}
IN = copy.deepcopy(OUT)
IN['Franklin Park']['Star White 256GB'] = True
STAMP = '2026-10-24T20:34:56.789+00:00'
TEST_IN = {store: {label: True for label in monitor.TEST_SKUS} for store in monitor.STORES.values()}


class FakeClock:
    def __init__(self, checks):
        self.seconds = 0
        self.checks = checks
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.checks

    def wait(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds
        return self.is_set()

    def utc_now(self):
        return (datetime(2026, 10, 24, tzinfo=timezone.utc) +
                timedelta(seconds=self.seconds)).isoformat(timespec='milliseconds')


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.original_state = copy.deepcopy(monitor.state)
        self.original_history = list(monitor.check_history)
        self.original_activity = dict(monitor.activity)
        self.cooldown_directory = tempfile.TemporaryDirectory()
        self.cooldown_file_patch = patch.object(monitor, 'COOLDOWN_FILE', Path(self.cooldown_directory.name) / 'cooldown.json')
        self.cooldown_file_patch.start()
        self.polling_file_patch = patch.object(monitor, 'POLLING_FILE', Path(self.cooldown_directory.name) / 'polling.json')
        self.polling_file_patch.start()
        monitor.state.update(cooldown_until=None, cooldown_attempts=0, cooldown_status=None,
                             cooldown_persistence_error=None, check_interval_seconds=30,
                             polling_fallback=False, polling_persistence_error=None)
        monitor.check_history.clear()
        monitor.activity['in_flight'] = None

    def tearDown(self):
        monitor.state.clear()
        monitor.state.update(self.original_state)
        monitor.check_history.clear()
        monitor.check_history.extend(self.original_history)
        monitor.activity.update(self.original_activity)
        self.cooldown_file_patch.stop()
        self.polling_file_patch.stop()
        self.cooldown_directory.cleanup()

    def run_checks(self, checks, inventory=OUT, send_failure=None, include_lifecycle=False,
                   lifecycle_failure=None, interval=60):
        clock = FakeClock(checks)
        sent = []
        attempts = []

        def send(result, reason, checked_at, test_mode=False):
            if reason not in ('started', 'stopped'):
                attempts.append((clock.seconds, reason))
            if reason == lifecycle_failure or (reason not in ('started', 'stopped') and
                                                send_failure and len(attempts) == send_failure):
                raise OSError('Simulated push outage')
            sent.append((clock.seconds, reason, result))
            return clock.utc_now()

        check = Mock(side_effect=inventory) if isinstance(inventory, list) else Mock(return_value=inventory)
        monitor.state['running'] = True
        monitor.state['check_interval_seconds'] = interval
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor, 'CHECK_INTERVAL_SECONDS', interval), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'check_inventory', check), \
             patch.object(monitor, 'send_notification', send), redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertFalse(monitor.state['running'])
        self.assertEqual(check.call_count, checks)
        self.assertTrue(all(wait == interval for wait in clock.waits))
        return (sent if include_lifecycle else [row for row in sent if row[1] not in ('started', 'stopped')]), attempts

    def test_unchanged_stock_has_initial_and_four_hour_statuses(self):
        sent, _ = self.run_checks(481)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status'), (14400, 'heartbeat'), (28800, 'heartbeat')])
        self.assertEqual(monitor.state['completed'], 481)
        self.assertIsNone(monitor.state['next_status_at'])

    def test_unchanged_in_stock_pushes_every_check_including_four_hours(self):
        sent, _ = self.run_checks(241, IN)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status')] + [(index*60, 'in_stock') for index in range(1, 241)])
        self.assertEqual(sent[-1][2], IN)

    def test_stock_changes_do_not_postpone_periodic_status(self):
        results = [IN]*3 + [OUT]*238
        sent, _ = self.run_checks(len(results), results)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status'), (60, 'in_stock'), (120, 'in_stock'),
                          (180, 'stock_change'), (14400, 'heartbeat')])

    def test_store_and_variant_changes_are_detected(self):
        other_store = copy.deepcopy(IN)
        other_store['Briarwood']['Night Sky 256GB'] = True
        sent, _ = self.run_checks(3, [OUT, IN, other_store])
        self.assertEqual([reason for _, reason, _ in sent], ['initial_status', 'stock_change', 'stock_change'])

    def test_check_errors_notify_once_then_report_recovery(self):
        sent, _ = self.run_checks(5, [OUT, ValueError('bad response'),
                                    ValueError('bad response'), OUT, OUT])
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status'), (60, 'error'), (180, 'recovered')])
        self.assertIsNone(sent[1][2])
        self.assertIsNone(monitor.state['last_error'])

    def test_outage_still_gets_four_hour_status(self):
        sent, _ = self.run_checks(241, [requests.Timeout('offline')] * 241)
        self.assertEqual([(t, r) for t, r, _ in sent], [(0, 'error'), (14400, 'error')])
        self.assertTrue(all(result is None for _, _, result in sent))
        self.assertIn('offline', monitor.state['last_error'])

    def test_failed_initial_push_is_retried(self):
        sent, attempts = self.run_checks(3, send_failure=1)
        self.assertEqual(attempts, [(0, 'initial_status'), (60, 'initial_status')])
        self.assertEqual([(t, r) for t, r, _ in sent], [(60, 'initial_status')])

    def test_failed_periodic_push_is_retried(self):
        sent, attempts = self.run_checks(242, send_failure=2)
        self.assertEqual(attempts, [(0, 'initial_status'), (14400, 'heartbeat'), (14460, 'heartbeat')])
        self.assertEqual(sent[-1][0], 14460)

    def test_failed_error_push_is_retried(self):
        sent, attempts = self.run_checks(4, [OUT] + [ValueError('offline')] * 3, send_failure=2)
        self.assertEqual(attempts, [(0, 'initial_status'), (60, 'error'), (120, 'error')])
        self.assertEqual(sent[-1][1], 'error')

    def test_notification_time_tracks_eastern_daylight_saving_to_the_second(self):
        for stamp, expected in [(STAMP, '2026-10-24 04:34:56 PM EDT'),
                                ('2026-07-01T02:03:04.999+00:00', '2026-06-30 10:03:04 PM EDT'),
                                ('2026-01-01T02:03:04.999+00:00', '2025-12-31 09:03:04 PM EST'),
                                ('2026-03-08T06:59:59+00:00', '2026-03-08 01:59:59 AM EST'),
                                ('2026-03-08T07:00:00+00:00', '2026-03-08 03:00:00 AM EDT'),
                                ('2026-11-01T05:59:59+00:00', '2026-11-01 01:59:59 AM EDT'),
                                ('2026-11-01T06:00:00+00:00', '2026-11-01 01:00:00 AM EST')]:
            payload = monitor.notification_payload(OUT, 'heartbeat', stamp)
            self.assertIn(expected, payload['body'])
            self.assertEqual(payload['title'], 'Duo · 4h status · OOS')
            self.assertEqual(len(payload['body'].splitlines()), 3)

    def test_lifecycle_notifications_have_explicit_titles_and_event_times(self):
        for reason in ('started', 'stopped'):
            payload = monitor.notification_payload(None, reason, STAMP)
            self.assertEqual(payload['title'], 'Duo monitoring '+reason)
            self.assertEqual(payload['body'], '2026-10-24 04:34:56 PM EDT')
            self.assertNotIn('Stock could not be verified', payload['body'])

    def test_start_and_stop_notifications_surround_the_inventory_checks(self):
        sent, _ = self.run_checks(2, include_lifecycle=True)
        self.assertEqual([reason for _, reason, _ in sent], ['started', 'initial_status', 'stopped'])
        self.assertIsNotNone(monitor.state['stopped_at'])

    def test_start_notification_is_sent_even_if_apple_check_fails(self):
        sent, _ = self.run_checks(1, [requests.Timeout('offline')], include_lifecycle=True)
        self.assertEqual([reason for _, reason, _ in sent], ['started', 'error', 'stopped'])

    def test_start_push_failure_does_not_prevent_monitoring(self):
        sent, _ = self.run_checks(2, include_lifecycle=True, lifecycle_failure='started')
        self.assertEqual([reason for _, reason, _ in sent], ['initial_status', 'stopped'])

    def test_stop_push_failure_still_stops_monitoring_and_exposes_error(self):
        self.run_checks(2, lifecycle_failure='stopped')
        self.assertFalse(monitor.state['running'])
        self.assertIn('Simulated push outage', monitor.state['last_notification_error'])

    def test_repeated_stop_requests_do_not_send_extra_pushes(self):
        event = threading.Event()
        monitor.state['running'] = True
        with patch.object(monitor, 'stop_event', event), patch.object(monitor, 'send_notification') as send:
            client = monitor.app.test_client()
            headers = {'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}
            for index in range(3):
                response = client.post('/api/stop', headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['already_stopping'], index > 0)
            monitor.state['running'] = False
            self.assertTrue(client.post('/api/stop', headers=headers).json['already_stopped'])
            send.assert_not_called()

    def test_stop_notification_follows_in_flight_request_without_a_stock_push(self):
        event = threading.Event()
        events = []
        def check(test_mode=False):
            events.append('apple_request')
            event.set()
            return OUT
        def send(inventory, reason, checked_at, test_mode=False):
            events.append(reason)
            return STAMP
        with patch.object(monitor, 'stop_event', event), \
             patch.object(monitor, 'check_inventory', side_effect=check), \
             patch.object(monitor, 'send_notification', side_effect=send), redirect_stdout(io.StringIO()):
            monitor.state['running'] = True
            monitor.run_monitor()
        self.assertEqual(events, ['started', 'apple_request', 'stopped'])
        self.assertFalse(monitor.state['running'])

    def test_notification_shows_available_store_and_variant(self):
        payload = monitor.notification_payload(IN, 'stock_change', STAMP)
        self.assertEqual(payload['title'], '🚨 DUO IN STOCK: White · Franklin Park')
        self.assertEqual(payload['body'].splitlines()[:2],
                         ['Franklin Park: White IN STOCK, Blue OOS', 'Briarwood: White OOS, Blue OOS'])
        self.assertNotIn('256', payload['body'])

    def test_all_four_combinations_are_explicit_for_every_stock_pattern(self):
        pairs = [(store, color) for store in monitor.STORES.values() for color in monitor.SKUS]
        for pattern in range(16):
            inventory = copy.deepcopy(OUT)
            for index, (store, color) in enumerate(pairs):
                inventory[store][color] = bool(pattern & (1 << index))
            payload = monitor.notification_payload(inventory, 'heartbeat', STAMP)
            self.assertEqual(len(payload['body'].splitlines()), 3)
            for store in monitor.STORES.values():
                white, blue = inventory[store].values()
                summary = {(False, False): 'White OOS, Blue OOS',
                           (True, False): 'White IN STOCK, Blue OOS',
                           (False, True): 'White OOS, Blue IN STOCK',
                           (True, True): 'White+Blue IN STOCK'}[(white, blue)]
                self.assertIn(store+': '+summary, payload['body'].splitlines()[:2])
            self.assertNotIn('256', payload['body'])
            self.assertTrue(all(len(line) <= 40 for line in payload['body'].splitlines()[:2]))
            options = monitor.stock_options(inventory)
            if pattern:
                self.assertTrue(options[0]['available'])
                self.assertIn(options[0]['store'], payload['title'])
                self.assertIn(monitor.SHORT_COLORS[options[0]['color']], payload['title'])

    def test_briarwood_blue_stock_is_immediately_identifiable(self):
        inventory = copy.deepcopy(OUT)
        inventory['Briarwood']['Night Sky 256GB'] = True
        payload = monitor.notification_payload(inventory, 'stock_change', STAMP)
        self.assertEqual(payload['title'], '🚨 DUO IN STOCK: Blue · Briarwood')
        self.assertEqual(payload['body'].splitlines()[0], 'Briarwood: White OOS, Blue IN STOCK')

    def test_legacy_color_names_are_supported_without_losing_stock_status(self):
        inventory = {store: {'White 256GB': False, 'Blue 256GB (verify)': store == 'Briarwood'}
                     for store in monitor.STORES.values()}
        options = monitor.stock_options(inventory)
        self.assertEqual(len(options), 4)
        self.assertEqual(options[0], {'store': 'Briarwood', 'color': 'Night Sky 256GB', 'available': True})

    def test_error_notification_does_not_claim_out_of_stock(self):
        payload = monitor.notification_payload(None, 'error', STAMP)
        self.assertIn('Check failed', payload['title'])
        self.assertNotIn('OOS', payload['body'])
        self.assertEqual(payload['body'].count('?'), 4)

    def test_inventory_parsing_and_incomplete_response(self):
        stores = [{'storeNumber': store_id, 'partsAvailability': {
            sku: {'pickupDisplay': 'available' if (store_id, color) == ('R299', 'Star White 256GB')
                  else 'unavailable'} for color, sku in monitor.SKUS.items()}}
                  for store_id in monitor.STORES]
        response = Mock()
        response.status_code = 200
        response.json.return_value = {'head': {'status': '200'}, 'body': {'stores': stores}}
        with patch.object(monitor.requests, 'get', return_value=response):
            self.assertEqual(monitor.check_inventory(), IN)
            stores.pop()
            with self.assertRaisesRegex(ValueError, 'missing Briarwood'):
                monitor.check_inventory()

    def test_api_requires_token_and_reports_continuous_mode(self):
        client = monitor.app.test_client()
        self.assertEqual(client.get('/api/status').status_code, 401)
        headers = {'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}
        monitor.state['running'] = False
        with patch.object(monitor, 'SUB_FILE') as sub, patch.object(monitor, 'KEY_FILE') as key, \
             patch.object(monitor.threading, 'Thread') as thread:
            sub.exists.return_value = key.exists.return_value = True
            response = client.post('/api/start', headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['continuous'])
            self.assertEqual(response.json['status_interval_seconds'], 14400)
            thread.return_value.start.assert_called_once()
            self.assertEqual(client.post('/api/start', headers=headers).status_code, 409)
        self.assertEqual(client.get('/api/status', headers=headers).json['check_interval_seconds'], 30)

    def test_invalid_inventory_responses_are_reported_as_check_failures(self):
        response = Mock()
        response.status_code = 200
        for payload in [None, [], {'head': None}, {'head': {'status': '200'}, 'body': None},
                        {'head': {'status': '200'}, 'body': {'stores': [None]}}]:
            response.json.return_value = payload
            with self.subTest(payload=payload), patch.object(monitor.requests, 'get', return_value=response):
                with self.assertRaises(ValueError):
                    monitor.check_inventory()

    def test_activity_records_success_duration_and_gap(self):
        response = Mock(status_code=200)
        monitor.check_history.append({'started_at': '2026-10-24T20:33:56.789+00:00'})
        with patch.object(monitor.requests, 'get', return_value=response), \
             patch.object(monitor, 'parse_inventory', return_value=OUT), \
             patch.object(monitor, 'utc_now', side_effect=[STAMP, '2026-10-24T20:34:56.914+00:00']), \
             patch.object(monitor.time, 'monotonic', side_effect=[10, 10.125]):
            self.assertEqual(monitor.check_inventory(), OUT)
        entry = monitor.check_history[-1]
        self.assertTrue(entry['ok'])
        self.assertEqual(entry['http_status'], 200)
        self.assertEqual(entry['duration_ms'], 125)
        self.assertEqual(entry['gap_seconds'], 60)
        self.assertIsNone(entry['error'])
        self.assertIsNone(monitor.activity['in_flight'])

    def test_activity_records_http_failures_and_timeouts(self):
        response = Mock(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError('Apple unavailable')
        with patch.object(monitor.requests, 'get', return_value=response):
            with self.assertRaises(requests.HTTPError):
                monitor.check_inventory()
        self.assertEqual(monitor.check_history[-1]['http_status'], 503)
        self.assertFalse(monitor.check_history[-1]['ok'])
        with patch.object(monitor.requests, 'get', side_effect=requests.Timeout('No response')):
            with self.assertRaises(requests.Timeout):
                monitor.check_inventory()
        self.assertEqual(len(monitor.check_history), 2)
        self.assertIsNone(monitor.check_history[-1]['http_status'])
        self.assertIn('No response', monitor.check_history[-1]['error'])
        self.assertIsNone(monitor.activity['in_flight'])

    def test_activity_records_invalid_inventory_as_failure_even_with_http_200(self):
        response = Mock(status_code=200)
        response.json.return_value = {'head': {'status': '500'}}
        with patch.object(monitor.requests, 'get', return_value=response):
            with self.assertRaises(ValueError):
                monitor.check_inventory()
        self.assertEqual(monitor.check_history[-1]['http_status'], 200)
        self.assertFalse(monitor.check_history[-1]['ok'])
        self.assertIsNone(monitor.check_history[-1]['inventory'])

    def test_activity_shows_in_flight_request(self):
        response = Mock(status_code=200)
        def request_in_progress(*args, **kwargs):
            data = monitor.app.test_client().get('/api/activity', headers={
                'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}).json
            self.assertIsNotNone(data['in_flight'])
            self.assertEqual(data['retained_count'], 0)
            return response
        with patch.object(monitor.requests, 'get', side_effect=request_in_progress), \
             patch.object(monitor, 'parse_inventory', return_value=OUT):
            monitor.check_inventory()
        self.assertIsNone(monitor.activity['in_flight'])

    def test_activity_api_limits_history_and_never_triggers_apple_request(self):
        for index in range(1450):
            stamp = (datetime(2026, 10, 24, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()
            monitor.check_history.append({'started_at': stamp, 'ok': index % 2 == 0})
        client = monitor.app.test_client()
        headers = {'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}
        self.assertEqual(client.get('/api/activity').status_code, 401)
        with patch.object(monitor.requests, 'get') as apple_request:
            data = client.get('/api/activity?limit=2', headers=headers).json
            apple_request.assert_not_called()
        self.assertEqual(data['retained_count'], 1440)
        self.assertEqual(data['success_count'], 720)
        self.assertEqual(data['failure_count'], 720)
        self.assertEqual(len(data['entries']), 2)
        self.assertGreater(data['entries'][0]['started_at'], data['entries'][1]['started_at'])
        self.assertEqual(data['entries'][0], monitor.check_history[-1])
        for limit in ['0', '1441', 'invalid']:
            self.assertEqual(client.get('/api/activity?limit='+limit, headers=headers).status_code, 400)

    def test_mode_api_requires_authentication_and_real_boolean(self):
        client = monitor.app.test_client()
        headers = {'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}
        self.assertEqual(client.post('/api/mode', json={'test_mode': True}).status_code, 401)
        for value in ['true', 1, None, []]:
            self.assertEqual(client.post('/api/mode', json={'test_mode': value}, headers=headers).status_code, 400)
        with patch.object(monitor.requests, 'get') as apple:
            result = client.post('/api/mode', json={'test_mode': True}, headers=headers).json
            self.assertTrue(result['changed'])
            self.assertTrue(result['test_mode'])
            self.assertEqual(set(result['skus'].values()), {'MJQ54LL/A'})
            revision = monitor.state['mode_revision']
            self.assertFalse(client.post('/api/mode', json={'test_mode': True}, headers=headers).json['changed'])
            self.assertEqual(monitor.state['mode_revision'], revision)
            self.assertFalse(client.post('/api/mode', json={'test_mode': False}, headers=headers).json['test_mode'])
            apple.assert_not_called()

    def test_test_mode_requests_only_test_sku_and_labels_history(self):
        stores = [{'storeNumber': store_id, 'partsAvailability': {
            'MJQ54LL/A': {'pickupDisplay': 'available'}}} for store_id in monitor.STORES]
        response = Mock(status_code=200)
        response.json.return_value = {'head': {'status': '200'}, 'body': {'stores': stores}}
        with patch.object(monitor.requests, 'get', return_value=response) as apple:
            self.assertEqual(monitor.check_inventory(True), TEST_IN)
        params = apple.call_args.kwargs['params']
        self.assertEqual([(key, value) for key, value in params if key.startswith('parts.')],
                         [('parts.0', 'MJQ54LL/A')])
        self.assertTrue(monitor.check_history[-1]['test_mode'])
        self.assertEqual(monitor.check_history[-1]['skus'], monitor.TEST_SKUS)

    def test_test_push_clearly_identifies_product_and_both_stores(self):
        payload = monitor.notification_payload(TEST_IN, 'initial_status', STAMP, True)
        self.assertTrue(payload['title'].startswith('TEST 18 Pro · IN STOCK: Burgundy'))
        self.assertIn('MJQ54LL/A', payload['skus'].values())
        for store in monitor.STORES.values():
            self.assertIn(store+': Burgundy IN STOCK', payload['body'])
        self.assertNotIn('Star White', payload['body'])
        self.assertNotIn('Night Sky', payload['body'])
        self.assertTrue(payload['test_mode'])
        failed = monitor.notification_payload(None, 'error', STAMP, True)
        self.assertEqual(failed['body'].count('?'), 2)
        self.assertEqual(len(payload['body'].splitlines()), 3)
        self.assertNotIn('256', payload['body'])

    def test_attention_marker_only_appears_for_real_duo_stock(self):
        for reason in ['initial_status', 'stock_change', 'in_stock', 'heartbeat', 'recovered']:
            self.assertTrue(monitor.notification_payload(IN, reason, STAMP)['title'].startswith('🚨 DUO IN STOCK'))
        for inventory, reason, test_mode in [(None, 'started', False), (None, 'stopped', False),
                                             (None, 'error', False), (OUT, 'heartbeat', False),
                                             (OUT, 'initial_status', False), (TEST_IN, 'in_stock', True)]:
            self.assertNotIn('🚨', monitor.notification_payload(inventory, reason, STAMP, test_mode)['title'])

    def test_positive_stock_pushes_request_high_urgency_and_record_acceptance(self):
        accepted = '2026-10-24T20:34:56.999+00:00'
        for inventory, test_mode in [(IN, False), (TEST_IN, True), (OUT, False), (None, False)]:
            with patch.object(monitor, 'SUB_FILE') as subscription, \
                 patch.object(monitor, 'webpush', return_value=Mock(status_code=201)) as push, \
                 patch.object(monitor, 'utc_now', side_effect=[STAMP, accepted]), \
                 patch.object(monitor.time, 'monotonic', side_effect=[1, 1.21]), redirect_stdout(io.StringIO()):
                subscription.read_text.return_value = '{}'
                returned = monitor.send_notification(inventory, 'in_stock' if inventory else 'error', STAMP, test_mode)
            expected = 'high' if inventory in (IN, TEST_IN) else 'normal'
            self.assertEqual(push.call_args.kwargs['headers'], {'Urgency': expected})
            self.assertEqual(returned, accepted)
            self.assertEqual(monitor.state['last_push']['submitted_at'], STAMP)
            self.assertEqual(monitor.state['last_push']['accepted_at'], accepted)
            self.assertEqual(monitor.state['last_push']['submission_ms'], 210)

    def test_failed_push_does_not_claim_acceptance(self):
        with patch.object(monitor, 'SUB_FILE') as subscription, \
             patch.object(monitor, 'webpush', side_effect=OSError('offline')):
            subscription.read_text.return_value = '{}'
            with self.assertRaises(OSError):
                monitor.send_notification(IN, 'in_stock', STAMP)
        self.assertIsNone(monitor.state['last_push'])

    def test_stock_push_timing_is_retained_with_its_request(self):
        monitor.check_history.append({'completed_at': STAMP, 'test_mode': False})
        with patch.object(monitor, 'SUB_FILE') as subscription, \
             patch.object(monitor, 'webpush', return_value=Mock(status_code=201)), redirect_stdout(io.StringIO()):
            subscription.read_text.return_value = '{}'
            monitor.send_notification(IN, 'in_stock', STAMP)
        self.assertEqual(monitor.check_history[-1]['push']['urgency'], 'high')
        recorded = monitor.check_history[-1]['push']
        with patch.object(monitor, 'SUB_FILE') as subscription, \
             patch.object(monitor, 'webpush', return_value=Mock(status_code=201)), redirect_stdout(io.StringIO()):
            subscription.read_text.return_value = '{}'
            monitor.send_notification(None, 'stopped', STAMP)
        self.assertEqual(monitor.check_history[-1]['push'], recorded)

    def test_stock_window_sends_each_positive_check_and_one_sold_out_update(self):
        sent, _ = self.run_checks(6, [OUT, IN, IN, IN, OUT, OUT])
        self.assertEqual([(t, reason) for t, reason, _ in sent],
                         [(0, 'initial_status'), (60, 'stock_change'), (120, 'in_stock'),
                          (180, 'in_stock'), (240, 'stock_change')])

    def test_failed_checks_do_not_repeat_stale_in_stock_results(self):
        sent, _ = self.run_checks(5, [IN, requests.Timeout('offline'), requests.Timeout('offline'), IN, OUT])
        self.assertEqual([(t, reason) for t, reason, _ in sent],
                         [(0, 'initial_status'), (60, 'error'), (180, 'recovered'), (240, 'stock_change')])

    def test_each_in_stock_payload_has_a_distinct_notification_id(self):
        payloads = [monitor.notification_payload(IN, 'in_stock', STAMP) for _ in range(3)]
        self.assertEqual(len({payload['check_id'] for payload in payloads}), 3)

    def test_test_mode_repeats_in_stock_notifications_every_minute(self):
        monitor.state['test_mode'] = True
        sent, _ = self.run_checks(3, TEST_IN)
        self.assertEqual([(t, reason) for t, reason, _ in sent],
                         [(0, 'initial_status'), (60, 'in_stock'), (120, 'in_stock')])

    def test_switching_modes_sends_fresh_status_and_restores_duo(self):
        class SwitchingClock(FakeClock):
            def wait(self, seconds):
                done = super().wait(seconds)
                if len(self.waits) < 3:
                    monitor.state['test_mode'] = len(self.waits) == 1
                    monitor.state['mode_revision'] += 1
                return done
        clock = SwitchingClock(3)
        sent = []
        def send(inventory, reason, checked_at, test_mode=False):
            sent.append((reason, test_mode))
            return clock.utc_now()
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'send_notification', send), \
             patch.object(monitor, 'check_inventory', side_effect=lambda mode: TEST_IN if mode else OUT), \
             redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(sent, [('started', False), ('initial_status', False),
                                ('initial_status', True), ('initial_status', False), ('stopped', False)])

    def test_mode_switch_during_request_does_not_send_old_mode_stock_alert(self):
        clock = FakeClock(2)
        sent = []
        def check(test_mode=False):
            if not test_mode:
                monitor.state['test_mode'] = True
                monitor.state['mode_revision'] += 1
                return OUT
            return TEST_IN
        def send(inventory, reason, checked_at, test_mode=False):
            sent.append((reason, test_mode))
            return clock.utc_now()
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'send_notification', send), \
             patch.object(monitor, 'check_inventory', side_effect=check), redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(sent, [('started', False), ('initial_status', True), ('stopped', True)])

    def test_cooldown_escalates_20_30_60_minutes_and_resets_on_success(self):
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            for expected in (1200, 1800, 3600, 3600):
                info = monitor.begin_cooldown(541)
                self.assertEqual(info['cooldown_remaining_seconds'], expected)
                self.assertTrue(info['cooldown_active'])
            self.assertTrue(monitor.COOLDOWN_FILE.exists())
            monitor.clear_cooldown()
            self.assertFalse(monitor.COOLDOWN_FILE.exists())
            self.assertEqual(monitor.state['cooldown_attempts'], 0)
            self.assertEqual(monitor.begin_cooldown(541)['cooldown_remaining_seconds'], 1200)

    def test_cooldown_respects_longer_retry_after_seconds_and_date(self):
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            self.assertEqual(monitor.begin_cooldown(429, '7200')['cooldown_remaining_seconds'], 7200)
            monitor.clear_cooldown()
            self.assertEqual(monitor.begin_cooldown(541, 'Sat, 24 Oct 2026 23:34:56 GMT')['cooldown_remaining_seconds'], 10800)
            monitor.clear_cooldown()
            self.assertEqual(monitor.begin_cooldown(541, 'not-a-date')['cooldown_remaining_seconds'], 1200)

    def test_saved_cooldown_survives_reload_start_stop_and_mode_changes(self):
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            original = monitor.begin_cooldown(541)
            monitor.state.update(cooldown_until=None, cooldown_attempts=0, cooldown_status=None)
            monitor.load_cooldown()
            self.assertEqual(monitor.cooldown_info()['cooldown_until'], original['cooldown_until'])
            client = monitor.app.test_client()
            headers = {'Authorization': 'Bearer ' + monitor.ADMIN_TOKEN}
            monitor.state['running'] = False
            with patch.object(monitor, 'SUB_FILE') as subscription, patch.object(monitor, 'KEY_FILE') as key, \
                 patch.object(monitor.threading, 'Thread'), patch.object(monitor, 'stop_event', threading.Event()), \
                 patch.object(monitor.requests, 'get') as apple:
                subscription.exists.return_value = key.exists.return_value = True
                response = client.post('/api/start', headers=headers)
                self.assertTrue(response.json['cooldown_active'])
                client.post('/api/stop', headers=headers)
                client.post('/api/mode', json={'test_mode': True}, headers=headers)
                apple.assert_not_called()
            self.assertEqual(monitor.cooldown_info()['cooldown_until'], original['cooldown_until'])

    def test_cooldown_makes_only_one_probe_after_each_quiet_period(self):
        clock = FakeClock(4)
        times = []
        sent = []
        def check(test_mode=False):
            times.append(clock.seconds)
            if len(times) <= 2:
                response = requests.Response()
                response.status_code = 541
                raise requests.HTTPError('blocked', response=response)
            return IN
        def send(inventory, reason, checked_at, test_mode=False):
            sent.append((clock.seconds, reason))
            return clock.utc_now()
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'check_inventory', side_effect=check), \
             patch.object(monitor, 'send_notification', send), redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(times, [0, 1200, 3000, 3060])
        self.assertEqual(clock.waits, [1200, 1800, 60, 60])
        self.assertEqual([row for row in sent if row[1] not in ('started', 'stopped')],
                         [(0, 'cooldown'), (1200, 'cooldown'), (3000, 'recovered'), (3060, 'in_stock')])
        self.assertEqual(monitor.state['cooldown_attempts'], 0)

    def test_stop_interrupts_cooldown_without_querying_apple_or_clearing_guard(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            monitor.begin_cooldown(541)
            with patch.object(monitor, 'stop_event', event), \
                 patch.object(monitor, 'check_inventory') as check, \
                 patch.object(monitor, 'send_lifecycle_notification'), redirect_stdout(io.StringIO()):
                monitor.run_monitor()
            event.wait.assert_called_once_with(1200)
            check.assert_not_called()
            self.assertTrue(monitor.cooldown_info()['cooldown_active'])
            self.assertTrue(monitor.COOLDOWN_FILE.exists())

    def test_failed_recovery_probe_does_not_resume_minute_retries(self):
        clock = FakeClock(4)
        response = requests.Response()
        response.status_code = 541
        check = Mock(side_effect=[requests.HTTPError('blocked', response=response),
                                  requests.Timeout('recovery not confirmed'), OUT, OUT])
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'check_inventory', check), \
             patch.object(monitor, 'send_notification', side_effect=lambda *args: clock.utc_now()), \
             redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(clock.waits, [1200, 1800, 60, 60])
        self.assertEqual(check.call_count, 4)
        self.assertEqual(monitor.state['cooldown_attempts'], 0)

    def test_cooldown_status_is_visible_in_activity_and_push(self):
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            monitor.begin_cooldown(541)
            client = monitor.app.test_client()
            data = client.get('/api/activity', headers={'Authorization': 'Bearer '+monitor.ADMIN_TOKEN}).json
            self.assertEqual(data['cooldown_remaining_seconds'], 1200)
            payload = monitor.notification_payload(None, 'cooldown', STAMP)
            self.assertEqual(payload['title'], 'Duo · Apple cooldown')
            self.assertIn('HTTP 541', payload['body'])
            self.assertIn('Stock unknown', payload['body'])
            self.assertIn('Retry:', payload['body'])
            self.assertNotIn('OOS', payload['body'])

    def test_403_and_429_use_the_same_quiet_period(self):
        for status in (403, 429):
            monitor.clear_cooldown()
            clock = FakeClock(1)
            response = requests.Response()
            response.status_code = status
            with patch.object(monitor, 'stop_event', clock), \
                 patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
                 patch.object(monitor, 'utc_now', clock.utc_now), \
                 patch.object(monitor, 'check_inventory', side_effect=requests.HTTPError('blocked', response=response)) as check, \
                 patch.object(monitor, 'send_notification', return_value=clock.utc_now()), redirect_stdout(io.StringIO()):
                monitor.run_monitor()
            check.assert_called_once()
            self.assertEqual(clock.waits, [1200])

    def test_production_30_second_cadence_keeps_four_hour_status_schedule(self):
        self.assertEqual(monitor.CHECK_INTERVAL_SECONDS, 30)
        sent, _ = self.run_checks(481, interval=30)
        self.assertEqual([(t, reason) for t, reason, _ in sent],
                         [(0, 'initial_status'), (14400, 'heartbeat')])
        sent, _ = self.run_checks(4, IN, interval=30)
        self.assertEqual([(t, reason) for t, reason, _ in sent],
                         [(0, 'initial_status'), (30, 'in_stock'), (60, 'in_stock'), (90, 'in_stock')])

    def test_cooldown_fallback_is_60_seconds_and_persists_after_recovery(self):
        with patch.object(monitor, 'utc_now', return_value=STAMP):
            monitor.begin_cooldown(541)
            self.assertEqual(monitor.state['check_interval_seconds'], 60)
            self.assertTrue(monitor.state['polling_fallback'])
            monitor.clear_cooldown()
            self.assertEqual(monitor.state['check_interval_seconds'], 60)
            monitor.state.update(check_interval_seconds=30, polling_fallback=False)
            monitor.load_polling()
            self.assertEqual(monitor.state['check_interval_seconds'], 60)
            self.assertTrue(monitor.state['polling_fallback'])

    def test_fast_override_requires_healthy_recovery_and_preserves_cooldown(self):
        client = monitor.app.test_client()
        headers = {'Authorization': 'Bearer '+monitor.ADMIN_TOKEN}
        self.assertEqual(client.post('/api/polling', json={'interval_seconds':30}).status_code, 401)
        for invalid in (15, True, '30', 30.0):
            self.assertEqual(client.post('/api/polling', json={'interval_seconds':invalid}, headers=headers).status_code, 400)
        with patch.object(monitor, 'utc_now', return_value=STAMP), patch.object(monitor.requests, 'get') as apple:
            guard = monitor.begin_cooldown(541)
            response = client.post('/api/polling', json={'interval_seconds':30}, headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(monitor.state['cooldown_until'], guard['cooldown_until'])
            with patch.object(monitor, 'utc_now', return_value='2026-10-25T00:00:00+00:00'):
                self.assertEqual(client.post('/api/polling', json={'interval_seconds':30}, headers=headers).status_code, 409)
            monitor.clear_cooldown()
            response = client.post('/api/polling', json={'interval_seconds':30}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['check_interval_seconds'], 30)
            self.assertFalse(response.json['polling_fallback'])
            monitor.state.update(check_interval_seconds=60, polling_fallback=True)
            monitor.load_polling()
            self.assertEqual(monitor.state['check_interval_seconds'], 30)
            monitor.begin_cooldown(541)
            self.assertEqual(monitor.state['check_interval_seconds'], 60)
            apple.assert_not_called()

    def test_fallback_then_pwa_override_changes_future_check_cadence(self):
        clock = FakeClock(4)
        response = requests.Response()
        response.status_code = 541
        times = []
        def check(test_mode=False):
            times.append(clock.seconds)
            if len(times) == 1:
                raise requests.HTTPError('blocked', response=response)
            return IN
        original_wait = clock.wait
        def wait(seconds):
            done = original_wait(seconds)
            if len(clock.waits) == 2:
                result = monitor.app.test_client().post('/api/polling', json={'interval_seconds':30},
                    headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN})
                self.assertEqual(result.status_code, 200)
            return done
        clock.wait = wait
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'check_inventory', side_effect=check), \
             patch.object(monitor, 'send_notification', side_effect=lambda *args: clock.utc_now()), \
             redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(times, [0, 1200, 1260, 1290])
        self.assertEqual(clock.waits, [1200, 60, 30, 30])

    def test_activity_exposes_the_selected_rate_and_fallback(self):
        monitor.state.update(check_interval_seconds=60, polling_fallback=True)
        data=monitor.app.test_client().get('/api/activity', headers={
            'Authorization':'Bearer '+monitor.ADMIN_TOKEN}).json
        self.assertEqual(data['check_interval_seconds'], 60)
        self.assertTrue(data['polling_fallback'])

    def test_concurrent_rate_controls_keep_saved_settings_valid(self):
        def choose(interval):
            return monitor.app.test_client().post('/api/polling', json={'interval_seconds':interval},
                headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(choose, [30, 60]*12))
        self.assertTrue(all(status == 200 for status in statuses))
        self.assertIsNone(monitor.state['polling_persistence_error'])
        import json
        saved = json.loads(monitor.POLLING_FILE.read_text())
        self.assertEqual(saved['check_interval_seconds'], monitor.state['check_interval_seconds'])

    def test_granular_polling_range_and_atomic_steps(self):
        client=monitor.app.test_client()
        headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN}
        with patch.object(monitor.requests, 'get') as apple:
            for interval in range(30,301,15):
                result=client.post('/api/polling',json={'interval_seconds':interval},headers=headers)
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json['check_interval_seconds'],interval)
            for invalid in (0,29,31,299,301,315,True,45.0,'75'):
                self.assertEqual(client.post('/api/polling',json={'interval_seconds':invalid},headers=headers).status_code,400)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':15},headers=headers).status_code,400)
            result=client.post('/api/polling',json={'delta_seconds':-15},headers=headers)
            self.assertEqual(result.json['check_interval_seconds'],285)
            client.post('/api/polling',json={'interval_seconds':30},headers=headers)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':-15},headers=headers).status_code,400)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':30},headers=headers).status_code,400)
            apple.assert_not_called()

    def test_slower_adjustments_during_cooldown_preserve_the_deadline(self):
        client=monitor.app.test_client()
        headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN}
        with patch.object(monitor,'utc_now',return_value=STAMP):
            guard=monitor.begin_cooldown(541)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':15},headers=headers).json['check_interval_seconds'],75)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':-15},headers=headers).json['check_interval_seconds'],60)
            self.assertEqual(client.post('/api/polling',json={'delta_seconds':-15},headers=headers).status_code,409)
            self.assertEqual(monitor.state['cooldown_until'],guard['cooldown_until'])

    def test_failure_only_raises_rates_below_60(self):
        for interval in (30,45,60,75,120,300):
            monitor.state.update(check_interval_seconds=interval,polling_fallback=False)
            clock=FakeClock(1)
            with patch.object(monitor,'stop_event',clock), \
                 patch.object(monitor.time,'monotonic',lambda:clock.seconds), \
                 patch.object(monitor,'utc_now',clock.utc_now), \
                 patch.object(monitor,'check_inventory',side_effect=requests.Timeout('offline')), \
                 patch.object(monitor,'send_notification',side_effect=lambda *args:clock.utc_now()), \
                 redirect_stdout(io.StringIO()):
                monitor.run_monitor()
            self.assertEqual(monitor.state['check_interval_seconds'],max(60,interval))
            self.assertEqual(clock.waits,[max(60,interval)])
            self.assertEqual(monitor.state['polling_fallback'],interval<60)

    def test_slow_rate_survives_block_recovery_and_backend_reload(self):
        monitor.state['check_interval_seconds']=90
        clock=FakeClock(3)
        response=requests.Response();response.status_code=541
        with patch.object(monitor,'stop_event',clock), \
             patch.object(monitor.time,'monotonic',lambda:clock.seconds), \
             patch.object(monitor,'utc_now',clock.utc_now), \
             patch.object(monitor,'check_inventory',side_effect=[requests.HTTPError('blocked',response=response),OUT,OUT]), \
             patch.object(monitor,'send_notification',side_effect=lambda *args:clock.utc_now()), \
             redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(clock.waits,[1200,90,90])
        monitor.state['check_interval_seconds']=30
        monitor.load_polling()
        self.assertEqual(monitor.state['check_interval_seconds'],90)
        with patch.object(monitor,'utc_now',return_value=STAMP):
            monitor.begin_cooldown(541)
            monitor.state['check_interval_seconds']=30
            monitor.load_polling();monitor.load_cooldown()
            self.assertEqual(monitor.state['check_interval_seconds'],90)

    def test_increasing_rate_extends_pending_wait_without_an_early_request(self):
        monitor.state['check_interval_seconds']=60
        clock=FakeClock(3);times=[]
        original_wait=clock.wait
        def wait(seconds):
            done=original_wait(seconds)
            if len(clock.waits)==1:
                result=monitor.app.test_client().post('/api/polling',json={'delta_seconds':15},
                    headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN})
                self.assertEqual(result.status_code,200)
            return done
        def check(test_mode=False):times.append(clock.seconds);return OUT
        clock.wait=wait
        with patch.object(monitor,'stop_event',clock), \
             patch.object(monitor.time,'monotonic',lambda:clock.seconds), \
             patch.object(monitor,'utc_now',clock.utc_now), \
             patch.object(monitor,'check_inventory',side_effect=check), \
             patch.object(monitor,'send_notification',side_effect=lambda *args:clock.utc_now()), \
             redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertEqual(times,[0,75])
        self.assertEqual(clock.waits,[60,15,75])

    def test_parallel_plus_steps_do_not_lose_an_increment(self):
        monitor.state['check_interval_seconds']=60
        def step(_):
            return monitor.app.test_client().post('/api/polling',json={'delta_seconds':15},
                headers={'Authorization':'Bearer '+monitor.ADMIN_TOKEN}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(step,range(2))),[200,200])
        self.assertEqual(monitor.state['check_interval_seconds'],90)

    def test_four_hour_status_stays_four_hours_at_a_75_second_rate(self):
        sent,_=self.run_checks(193,interval=75)
        self.assertEqual([(t,reason) for t,reason,_ in sent],[(0,'initial_status'),(14400,'heartbeat')])


if __name__ == '__main__':
    unittest.main()
