"""Exercise scheduling without waiting four hours or sending real pushes."""
import copy
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

import monitor


OUT = {name: {color: False for color in monitor.SKUS} for name in monitor.STORES.values()}
IN = copy.deepcopy(OUT)
IN['Franklin Park']['White 256GB'] = True
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

    def tearDown(self):
        monitor.state.clear()
        monitor.state.update(self.original_state)

    def run_checks(self, checks, inventory=OUT, send_failure=None):
        clock = FakeClock(checks)
        sent = []
        attempts = []

        def send(result, reason, checked_at):
            attempts.append((clock.seconds, reason))
            if send_failure and len(attempts) == send_failure:
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
        return sent, attempts

    def test_unchanged_stock_has_initial_and_four_hour_statuses(self):
        sent, _ = self.run_checks(481)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'started'), (14400, 'heartbeat'), (28800, 'heartbeat')])
        self.assertEqual(monitor.state['completed'], 481)
        self.assertEqual(monitor.state['next_status_at'], '2026-10-24T12:00:00.000+00:00')

    def test_in_stock_also_gets_four_hour_status(self):
        sent, _ = self.run_checks(241, IN)
        self.assertEqual([(t, r) for t, r, _ in sent], [(0, 'started'), (14400, 'heartbeat')])
        self.assertEqual(sent[-1][2], IN)

    def test_stock_changes_do_not_postpone_periodic_status(self):
        results = [OUT, IN] + [IN] * 238 + [IN, OUT]
        sent, _ = self.run_checks(len(results), results)
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'started'), (60, 'stock_change'),
                          (14400, 'heartbeat'), (14460, 'stock_change')])

    def test_store_and_variant_changes_are_detected(self):
        other_store = copy.deepcopy(IN)
        other_store['Briarwood']['Blue 256GB (verify)'] = True
        sent, _ = self.run_checks(3, [OUT, IN, other_store])
        self.assertEqual([reason for _, reason, _ in sent], ['started', 'stock_change', 'stock_change'])

    def test_check_errors_notify_once_then_report_recovery(self):
        sent, _ = self.run_checks(5, [OUT, ValueError('bad response'),
                                    ValueError('bad response'), OUT, OUT])
        self.assertEqual([(t, r) for t, r, _ in sent],
                         [(0, 'started'), (60, 'error'), (180, 'recovered')])
        self.assertIsNone(sent[1][2])
        self.assertIsNone(monitor.state['last_error'])

    def test_outage_still_gets_four_hour_status(self):
        sent, _ = self.run_checks(241, [requests.Timeout('offline')] * 241)
        self.assertEqual([(t, r) for t, r, _ in sent], [(0, 'error'), (14400, 'error')])
        self.assertTrue(all(result is None for _, _, result in sent))
        self.assertIn('offline', monitor.state['last_error'])

    def test_failed_initial_push_is_retried(self):
        sent, attempts = self.run_checks(3, send_failure=1)
        self.assertEqual(attempts, [(0, 'started'), (60, 'started')])
        self.assertEqual([(t, r) for t, r, _ in sent], [(60, 'started')])

    def test_failed_periodic_push_is_retried(self):
        sent, attempts = self.run_checks(242, send_failure=2)
        self.assertEqual(attempts, [(0, 'started'), (14400, 'heartbeat'), (14460, 'heartbeat')])
        self.assertEqual(sent[-1][0], 14460)

    def test_failed_error_push_is_retried(self):
        sent, attempts = self.run_checks(4, [OUT] + [ValueError('offline')] * 3, send_failure=2)
        self.assertEqual(attempts, [(0, 'started'), (60, 'error'), (120, 'error')])
        self.assertEqual(sent[-1][1], 'error')

    def test_notification_time_is_est_to_the_second_including_summer(self):
        for stamp, expected in [(STAMP, '2026-10-24 03:34:56 PM EST'),
                                ('2026-07-01T02:03:04.999+00:00', '2026-06-30 09:03:04 PM EST')]:
            payload = monitor.notification_payload(OUT, 'heartbeat', stamp)
            self.assertIn(expected, payload['body'])
            self.assertEqual(payload['title'], 'Duo is out of stock')
            self.assertIn('4-hour status check', payload['body'])

    def test_notification_shows_available_store_and_variant(self):
        payload = monitor.notification_payload(IN, 'stock_change', STAMP)
        self.assertEqual(payload['title'], 'Duo is in stock')
        self.assertIn('Franklin Park: White 256GB in stock', payload['body'])
        self.assertIn('Briarwood: Out of stock', payload['body'])

    def test_error_notification_does_not_claim_out_of_stock(self):
        payload = monitor.notification_payload(None, 'error', STAMP)
        self.assertIn('could not be verified', payload['body'])
        self.assertNotIn('out of stock', payload['body'].lower())

    def test_inventory_parsing_and_incomplete_response(self):
        stores = [{'storeNumber': store_id, 'partsAvailability': {
            sku: {'pickupDisplay': 'available' if (store_id, color) == ('R299', 'White 256GB')
                  else 'unavailable'} for color, sku in monitor.SKUS.items()}}
                  for store_id in monitor.STORES]
        response = Mock()
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
        for payload in [None, [], {'head': None}, {'head': {'status': '200'}, 'body': None},
                        {'head': {'status': '200'}, 'body': {'stores': [None]}}]:
            response.json.return_value = payload
            with self.subTest(payload=payload), patch.object(monitor.requests, 'get', return_value=response):
                with self.assertRaises(ValueError):
                    monitor.check_inventory()


if __name__ == '__main__':
    unittest.main()
