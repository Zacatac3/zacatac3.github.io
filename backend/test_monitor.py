"""Exercise scheduling without waiting four hours or sending real pushes."""
import copy
import io
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

import monitor


OUT = {name: {color: False for color in monitor.SKUS} for name in monitor.STORES.values()}
IN = copy.deepcopy(OUT)
IN['Franklin Park']['Star White 256GB'] = True
STAMP = '2026-10-24T20:34:56.789+00:00'


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
        monitor.check_history.clear()
        monitor.activity['in_flight'] = None

    def tearDown(self):
        monitor.state.clear()
        monitor.state.update(self.original_state)
        monitor.check_history.clear()
        monitor.check_history.extend(self.original_history)
        monitor.activity.update(self.original_activity)

    def run_checks(self, checks, inventory=OUT, send_failure=None, include_lifecycle=False,
                   lifecycle_failure=None):
        clock = FakeClock(checks)
        sent = []
        attempts = []

        def send(result, reason, checked_at):
            if reason not in ('started', 'stopped'):
                attempts.append((clock.seconds, reason))
            if reason == lifecycle_failure or (reason not in ('started', 'stopped') and
                                                send_failure and len(attempts) == send_failure):
                raise OSError('Simulated push outage')
            sent.append((clock.seconds, reason, result))
            return clock.utc_now()

        check = Mock(side_effect=inventory) if isinstance(inventory, list) else Mock(return_value=inventory)
        monitor.state['running'] = True
        with patch.object(monitor, 'stop_event', clock), \
             patch.object(monitor.time, 'monotonic', lambda: clock.seconds), \
             patch.object(monitor, 'utc_now', clock.utc_now), \
             patch.object(monitor, 'check_inventory', check), \
             patch.object(monitor, 'send_notification', send), redirect_stdout(io.StringIO()):
            monitor.run_monitor()
        self.assertFalse(monitor.state['running'])
        self.assertEqual(check.call_count, checks)
        self.assertTrue(all(wait == 60 for wait in clock.waits))
        return (sent if include_lifecycle else [row for row in sent if row[1] not in ('started', 'stopped')]), attempts

    def test_unchanged_stock_has_initial_and_four_hour_statuses(self):
        sent, _ = self.run_checks(481)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status'), (14400, 'heartbeat'), (28800, 'heartbeat')])
        self.assertEqual(monitor.state['completed'], 481)
        self.assertIsNone(monitor.state['next_status_at'])

    def test_in_stock_also_gets_four_hour_status(self):
        sent, _ = self.run_checks(241, IN)
        self.assertEqual([(t, r) for t, r, _ in sent], [(0, 'initial_status'), (14400, 'heartbeat')])
        self.assertEqual(sent[-1][2], IN)

    def test_stock_changes_do_not_postpone_periodic_status(self):
        results = [OUT, IN] + [IN] * 238 + [IN, OUT]
        sent, _ = self.run_checks(len(results), results)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'initial_status'), (60, 'stock_change'),
                          (14400, 'heartbeat'), (14460, 'stock_change')])

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
            self.assertEqual(payload['title'], 'Duo is out of stock')
            self.assertIn('4-hour status check', payload['body'])

    def test_lifecycle_notifications_have_explicit_titles_and_event_times(self):
        for reason in ('started', 'stopped'):
            payload = monitor.notification_payload(None, reason, STAMP)
            self.assertEqual(payload['title'], 'Duo monitoring '+reason)
            self.assertIn(reason.capitalize()+': 2026-10-24 04:34:56 PM EDT', payload['body'])
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
        def check():
            events.append('apple_request')
            event.set()
            return OUT
        def send(inventory, reason, checked_at):
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
        self.assertEqual(payload['title'], 'In stock: Star White at Franklin Park')
        self.assertIn('Franklin Park · Star White 256GB: IN STOCK', payload['body'])
        self.assertIn('Franklin Park · Night Sky 256GB: Out of stock', payload['body'])
        self.assertIn('Briarwood · Star White 256GB: Out of stock', payload['body'])
        self.assertIn('Briarwood · Night Sky 256GB: Out of stock', payload['body'])

    def test_all_four_combinations_are_explicit_for_every_stock_pattern(self):
        pairs = [(store, color) for store in monitor.STORES.values() for color in monitor.SKUS]
        for pattern in range(16):
            inventory = copy.deepcopy(OUT)
            for index, (store, color) in enumerate(pairs):
                inventory[store][color] = bool(pattern & (1 << index))
            payload = monitor.notification_payload(inventory, 'heartbeat', STAMP)
            for store, color in pairs:
                status = 'IN STOCK' if inventory[store][color] else 'Out of stock'
                self.assertIn(f'{store} · {color}: {status}', payload['body'])
            options = monitor.stock_options(inventory)
            if pattern:
                self.assertTrue(options[0]['available'])
                self.assertIn(options[0]['store'], payload['title'])
                self.assertIn(options[0]['color'].removesuffix(' 256GB'), payload['title'])

    def test_briarwood_blue_stock_is_immediately_identifiable(self):
        inventory = copy.deepcopy(OUT)
        inventory['Briarwood']['Night Sky 256GB'] = True
        payload = monitor.notification_payload(inventory, 'stock_change', STAMP)
        self.assertEqual(payload['title'], 'In stock: Night Sky at Briarwood')
        self.assertEqual(payload['body'].splitlines()[1], 'Briarwood · Night Sky 256GB: IN STOCK')

    def test_legacy_color_names_are_supported_without_losing_stock_status(self):
        inventory = {store: {'White 256GB': False, 'Blue 256GB (verify)': store == 'Briarwood'}
                     for store in monitor.STORES.values()}
        options = monitor.stock_options(inventory)
        self.assertEqual(len(options), 4)
        self.assertEqual(options[0], {'store': 'Briarwood', 'color': 'Night Sky 256GB', 'available': True})

    def test_error_notification_does_not_claim_out_of_stock(self):
        payload = monitor.notification_payload(None, 'error', STAMP)
        self.assertIn('could not be verified', payload['body'])
        self.assertNotIn('out of stock', payload['body'].lower())
        self.assertEqual(payload['body'].count(': Unknown'), 4)

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
        self.assertEqual(client.get('/api/status', headers=headers).json['check_interval_seconds'], 60)

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


if __name__ == '__main__':
    unittest.main()
