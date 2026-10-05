import json
import tempfile
import unittest
from pathlib import Path
from http.client import HTTPConnection
from server import MusicCompanionServer
from music_companion.calendar_model import CalendarEvent

class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.TemporaryDirectory()
        cls.server=MusicCompanionServer(('127.0.0.1',0), data_dir=cls.data_dir.name); cls.server.timeout=0.1
        import threading
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.port=cls.server.server_address[1]
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.data_dir.cleanup()
    def req(self,method,path,body=None):
        c=HTTPConnection('127.0.0.1',self.port,timeout=3); data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        c.request(method,path,data,{'Content-Type':'application/json'} if data else {}); r=c.getresponse(); raw=r.read(); c.close(); return r.status, json.loads(raw.decode()) if raw else None
    def test_calendar_crud_and_plan(self):
        status, body=self.req('POST','/api/calendar/events',{'id':'x','title':'学习','start':'2026-10-04T09:00:00+08:00','end':'2026-10-04T10:40:00+08:00','category':'study'})
        self.assertEqual(status,201); self.assertEqual(body['event']['id'],'x')
        status, body=self.req('GET','/api/calendar/events?date=2026-10-04'); self.assertEqual(status,200); self.assertEqual(len(body['events']),1)
        status, body=self.req('POST','/api/state/manual',{'captured_at':'2026-10-03T21:00:00+08:00','energy':40,'stress':70,'focus':80,'sleep_hours':6}); self.assertEqual(status,201)
        status, body=self.req('POST','/api/planner/plan',{'day_start':'2026-10-04T08:00:00+08:00','day_end':'2026-10-04T12:00:00+08:00'}); self.assertEqual(status,200); self.assertTrue(body['plans'])
    def test_ics_import(self):
        text='BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:i\nDTSTART:20261004T010000Z\nDTEND:20261004T020000Z\nSUMMARY:导入\nEND:VEVENT\nEND:VCALENDAR\n'
        status, body=self.req('POST','/api/calendar/import-ics',{'content':text}); self.assertEqual(status,201); self.assertEqual(body['imported'],1)

    def test_calendar_and_state_persist_across_server_restart(self):
        status, _ = self.req('POST', '/api/calendar/events', {
            'id': 'persisted', 'title': '学习',
            'start': '2026-10-05T09:00:00+08:00',
            'end': '2026-10-05T10:40:00+08:00', 'category': 'study',
        })
        self.assertEqual(status, 201)
        status, _ = self.req('POST', '/api/state/manual', {
            'captured_at': '2026-10-04T21:00:00+08:00',
            'energy': 40, 'stress': 70, 'focus': 80, 'sleep_hours': 6,
        })
        self.assertEqual(status, 201)
        status, body = self.req('POST', '/api/planner/plan', {
            'day_start': '2026-10-05T08:00:00+08:00',
            'day_end': '2026-10-05T12:00:00+08:00',
        })
        self.assertEqual(status, 200)
        self.assertTrue(body['plans'])
        plan_id = body['plans'][0]['id']
        plan_store = Path(self.data_dir.name) / 'plans.json'
        self.assertTrue(plan_store.exists())
        self.assertEqual(json.loads(plan_store.read_text(encoding='utf-8'))[plan_id]['status'], 'planned')
        self.server.shutdown(); self.server.server_close()
        self.server = MusicCompanionServer(('127.0.0.1', 0), data_dir=self.data_dir.name)
        self.server.timeout = 0.1
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start(); self.port = self.server.server_address[1]
        type(self).server = self.server
        type(self).thread = self.thread
        type(self).port = self.port
        status, body = self.req('GET', '/api/calendar/events?date=2026-10-05')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in body['events']], ['persisted'])
        status, body = self.req('GET', '/api/state/latest')
        self.assertEqual(status, 200)
        self.assertEqual(body['state']['energy'], 40)

    def test_plan_status_and_event_status_update_are_persisted(self):
        status, _ = self.req('POST', '/api/calendar/events', {
            'id': 'status-event', 'title': '学习',
            'start': '2026-10-06T09:00:00+08:00',
            'end': '2026-10-06T10:40:00+08:00', 'category': 'study',
        })
        self.assertEqual(status, 201)
        status, body = self.req('POST', '/api/calendar/events/status-event/status', {'status': 'done'})
        self.assertEqual(status, 200)
        self.assertEqual(body['event']['status'], 'done')
        status, body = self.req('GET', '/api/calendar/events?date=2026-10-06')
        self.assertEqual(body['events'][0]['status'], 'done')

    def test_malformed_content_length_returns_json_400(self):
        from http.client import HTTPConnection
        connection = HTTPConnection('127.0.0.1', self.port, timeout=3)
        connection.putrequest('POST', '/api/calendar/events')
        connection.putheader('Content-Length', 'not-a-number')
        connection.putheader('Content-Type', 'application/json')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.read().decode())['error'], '请求内容长度无效')

if __name__=='__main__': unittest.main()
