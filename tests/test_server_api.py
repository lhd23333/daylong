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
    def req(self,method,path,body=None,timeout=3):
        c=HTTPConnection('127.0.0.1',self.port,timeout=timeout); data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
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

    def test_playlist_collect_replay_and_delete(self):
        # 空歌单是合法状态，不是 404：前端首屏就会拉一次。
        status, body = self.req('GET', '/api/playlist')
        self.assertEqual(status, 200)
        self.assertEqual(body['items'], [])

        recipe = {
            'styleId': 'first-light', 'bpm': 66, 'density': 0.28,
            'key': 'C', 'progressionIndex': 1, 'seed': 7, 'name': '晨光 · 66 BPM',
        }
        status, body = self.req('POST', '/api/playlist', recipe)
        self.assertEqual(status, 201)
        self.assertFalse(body['duplicate'])
        entry = body['item']
        self.assertEqual(entry['styleId'], 'first-light')
        self.assertEqual(entry['bpm'], 66)
        self.assertTrue(entry['id'])
        entry_id = entry['id']

        # 同一段音乐再收藏一次：命中已有条目，回 200 且不改名字。
        status, body = self.req('POST', '/api/playlist', {**recipe, 'name': '换个名字'})
        self.assertEqual(status, 200)
        self.assertTrue(body['duplicate'])
        self.assertEqual(body['item']['id'], entry_id)
        self.assertEqual(body['item']['name'], '晨光 · 66 BPM')

        status, body = self.req('GET', '/api/playlist')
        self.assertEqual(len(body['items']), 1)

        # 越界不夹紧，直接 400——存下来的必须是当时听到的那一段。
        status, body = self.req('POST', '/api/playlist', {**recipe, 'bpm': 300})
        self.assertEqual(status, 400)
        self.assertIn('bpm', body['error'].lower())

        status, _ = self.req('DELETE', f'/api/playlist/{entry_id}')
        self.assertEqual(status, 200)
        status, _ = self.req('DELETE', f'/api/playlist/{entry_id}')
        self.assertEqual(status, 404)
        status, body = self.req('GET', '/api/playlist')
        self.assertEqual(body['items'], [])
        self.assertTrue((Path(self.data_dir.name) / 'playlist.json').exists())

    def test_agent_endpoint_returns_writable_events(self):
        # 对话入口：读一段自由文本，返回的 events 必须能原样 POST 回日历——
        # 前端就是这么用的（写进今天），两边对不上的话按钮会直接 400。
        # 这条会真的走一次模型网络调用（服务端超时 8 s，失败会回落本地规则），
        # 所以客户端超时放宽到 15 s——3 s 的默认值在模型稍慢时必然假失败。
        status, body = self.req('POST', '/api/agent', {
            'text': '上午数学课，下午四点半去操场跑半小时',
            'now': '2026-10-05T14:07:00+08:00',
        }, timeout=15)
        self.assertEqual(status, 200)
        self.assertEqual([item['title'] for item in body['events']], ['数学课', '操场跑'])
        self.assertEqual(body['music']['soundscape'], 'run')
        self.assertIn('reply', body)
        for item in body['events']:
            status, created = self.req('POST', '/api/calendar/events', item)
            self.assertEqual(status, 201)
            self.assertEqual(created['event']['start'], item['start'])
        # 同一个 server 实例被这个文件里所有用例共用，写完要清干净，
        # 否则后面断言日程列表的用例会看见这几条。
        for item in body['events']:
            self.req('DELETE', f"/api/calendar/events/{item['id']}")

    def test_agent_endpoint_rejects_empty_text(self):
        status, body = self.req('POST', '/api/agent', {'text': '   '})
        self.assertEqual(status, 400)
        self.assertIn('error', body)

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
