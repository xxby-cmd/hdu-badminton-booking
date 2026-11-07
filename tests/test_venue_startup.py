import unittest
from unittest.mock import Mock

from booking import BookingError, Stopped
from session import SessionAccountChanged, SessionError
from venue_startup import wait_for_session


class SimulatedTime:
    def __init__(self, start=0):
        self.current = start
        self.stopped = False

    def now(self):
        return self.current

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.current += seconds


class VenueStartupTests(unittest.TestCase):
    def test_entry_open_failure_retries_then_new_login_is_accepted(self):
        clock = SimulatedTime()
        opens, events = [], []

        def enter():
            opens.append(clock.now())
            if len(opens) == 1:
                raise OSError('offline')

        def load(release):
            self.assertEqual(release, 600)
            if len(opens) < 2:
                raise SessionError('offline')
            return 'fresh_session'

        result = wait_for_session(600, clock, lambda **e: events.append(e), load,
                                  now=clock.now, open_entrance=enter)
        self.assertEqual(result, 'fresh_session')
        self.assertEqual(opens, [0, 50])
        self.assertTrue(all(e['state'] == 'WAITING_LOGIN' for e in events))

    def test_early_wait_reopens_near_release_and_stops_at_release(self):
        clock = SimulatedTime()
        opens = []
        with self.assertRaises(BookingError):
            wait_for_session(2400, clock, Mock(), Mock(side_effect=SessionError('offline')),
                             now=clock.now, open_entrance=lambda: opens.append(clock.now()))
        self.assertEqual(opens, [0, 50, 1200, 1250, 1300])
        self.assertEqual(clock.now(), 2400)

    def test_account_switch_or_stop_does_not_open_entrance(self):
        clock = SimulatedTime()
        opener = Mock()
        with self.assertRaises(BookingError):
            wait_for_session(600, clock, Mock(), Mock(side_effect=SessionAccountChanged('offline')),
                             now=clock.now, open_entrance=opener)
        clock.stopped = True
        with self.assertRaises(Stopped):
            wait_for_session(600, clock, Mock(), Mock(), now=clock.now, open_entrance=opener)
        opener.assert_not_called()
