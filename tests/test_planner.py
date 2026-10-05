import json
import unittest
from music_companion.calendar_model import CalendarEvent
from music_companion.state import StatusSnapshot, parse_status_import
from music_companion.planner import plan_breaks, music_context_for_plan

class PlannerTests(unittest.TestCase):
    def test_long_study_gets_walk_with_reason_and_bpm(self):
        events=[CalendarEvent('study','学习','2026-10-04T09:00:00+08:00','2026-10-04T10:40:00+08:00',category='study')]
        status=StatusSnapshot('2026-10-03T21:00:00+08:00',energy=40,stress=70,focus=80,sleep_hours=6,heart_rate=80,steps=2000,sedentary_minutes=80)
        plans=plan_breaks(events,status,day_start='2026-10-04T08:00:00+08:00',day_end='2026-10-04T12:00:00+08:00')
        self.assertTrue(any(item.type == 'walk' for item in plans))
        self.assertTrue(plans[0].reason)
        self.assertTrue(80 <= plans[0].recommended_bpm <= 115)

    def test_no_short_gap_insertion(self):
        events=[CalendarEvent('a','课','2026-10-04T09:00:00+08:00','2026-10-04T10:00:00+08:00',category='study'), CalendarEvent('b','会','2026-10-04T10:05:00+08:00','2026-10-04T11:00:00+08:00')]
        plans=plan_breaks(events,None,day_start='2026-10-04T08:00:00+08:00',day_end='2026-10-04T12:00:00+08:00')
        self.assertTrue(all(item.start != '2026-10-04T10:00:00+08:00' for item in plans))

    def test_import_json_and_csv(self):
        data=parse_status_import(json.dumps({'captured_at':'2026-10-03T21:00:00+08:00','energy':50,'stress':20,'focus':70,'sleep_hours':8,'source':'watch_import'}),'json')
        self.assertEqual(data.source,'watch_import')
        csv='captured_at,energy,stress,focus,sleep_hours\n2026-10-03T21:00:00+08:00,50,20,70,8\n'
        self.assertEqual(parse_status_import(csv,'csv').focus,70)

    def test_context_uses_plan_bpm(self):
        events=[CalendarEvent('s','学习','2026-10-04T09:00:00+08:00','2026-10-04T10:40:00+08:00',category='study')]
        plan=plan_breaks(events,None,day_start='2026-10-04T08:00:00+08:00',day_end='2026-10-04T12:00:00+08:00')[0]
        context=music_context_for_plan(plan,events,None)
        self.assertEqual(context['plan_type'],plan.type)
        self.assertEqual(context['target_bpm'],plan.recommended_bpm)

    def test_contiguous_focus_events_are_counted_as_one_segment(self):
        events = [
            CalendarEvent('a', '数学', '2026-10-04T09:00:00+08:00', '2026-10-04T10:00:00+08:00', category='study'),
            CalendarEvent('b', '物理', '2026-10-04T10:00:00+08:00', '2026-10-04T10:40:00+08:00', category='study'),
        ]
        plans = plan_breaks(
            events, None,
            day_start='2026-10-04T08:00:00+08:00',
            day_end='2026-10-04T12:00:00+08:00',
        )
        self.assertTrue(any(item.type == 'walk' for item in plans))
        self.assertIn('100', plans[0].reason)

    def test_events_are_clipped_to_planning_day(self):
        events = [
            CalendarEvent('a', '跨界学习', '2026-10-04T06:00:00+08:00', '2026-10-04T10:00:00+08:00', category='study'),
        ]
        plans = plan_breaks(
            events, None,
            day_start='2026-10-04T08:00:00+08:00',
            day_end='2026-10-04T12:00:00+08:00',
        )
        self.assertTrue(plans)
        self.assertGreaterEqual(plans[0].start, '2026-10-04T10:00:00+08:00')

    def test_hard_non_interruptible_event_never_contains_a_plan(self):
        events = [
            CalendarEvent('a', '学习', '2026-10-04T09:00:00+08:00', '2026-10-04T10:40:00+08:00', category='study', priority='hard', interruptible=False),
            CalendarEvent('b', '会议', '2026-10-04T10:45:00+08:00', '2026-10-04T11:30:00+08:00', category='work', priority='hard', interruptible=False),
        ]
        plans = plan_breaks(
            events, None,
            day_start='2026-10-04T08:00:00+08:00',
            day_end='2026-10-04T12:00:00+08:00',
        )
        self.assertTrue(all(item.end <= events[1].start for item in plans))
        self.assertTrue(all(item.start >= events[0].end for item in plans))

    def test_interruptible_focus_event_can_contain_a_plan(self):
        events = [
            CalendarEvent('a', '可打断学习', '2026-10-04T09:00:00+08:00', '2026-10-04T11:00:00+08:00', category='study', priority='soft', interruptible=True),
        ]
        plans = plan_breaks(
            events, None,
            day_start='2026-10-04T08:00:00+08:00',
            day_end='2026-10-04T12:00:00+08:00',
        )
        self.assertTrue(any(item.type == 'walk' for item in plans))
        self.assertTrue(any(item.start >= events[0].start and item.end <= events[0].end for item in plans))

if __name__=='__main__': unittest.main()
