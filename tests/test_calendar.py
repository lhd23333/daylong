import unittest
from music_companion.calendar_model import CalendarEvent, find_conflicts, validate_events
from music_companion.ics_parser import parse_ics

class CalendarTests(unittest.TestCase):
    def test_event_validation_and_conflicts(self):
        events = [
            CalendarEvent('a','学习','2026-10-04T09:00:00+08:00','2026-10-04T10:00:00+08:00'),
            CalendarEvent('b','会议','2026-10-04T09:30:00+08:00','2026-10-04T11:00:00+08:00'),
        ]
        self.assertEqual(validate_events(events), events)
        self.assertEqual(find_conflicts(events), [('a','b')])

    def test_ics_unfolds_and_parses_utc(self):
        text = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n'
                'BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261004T010000Z\r\n'
                'DTEND:20261004T023000Z\r\nSUMMARY:数学\r\n'
                'DESCRIPTION:连续\r\n 专注\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, 'x')
        self.assertEqual(events[0].title, '数学')
        self.assertTrue(events[0].start.endswith('+00:00'))
        self.assertIn('连续专注', events[0].description)

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            CalendarEvent('x','bad','2026-10-04T11:00:00+08:00','2026-10-04T10:00:00+08:00')

    def test_event_status_can_be_updated_and_round_tripped(self):
        event = CalendarEvent(
            'x', '学习', '2026-10-04T09:00:00+08:00',
            '2026-10-04T10:00:00+08:00', status='started',
        )
        self.assertEqual(CalendarEvent.from_mapping(event.to_dict()).status, 'started')

    def test_string_false_interruptible_is_not_truthy(self):
        event = CalendarEvent.from_mapping({
            'id': 'x', 'title': '学习',
            'start': '2026-10-04T09:00:00+08:00',
            'end': '2026-10-04T10:00:00+08:00',
            'interruptible': 'false',
        })
        self.assertFalse(event.interruptible)

if __name__ == '__main__':
    unittest.main()
