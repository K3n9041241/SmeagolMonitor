import base64
import csv
import json
import io
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import server

PACKET = 'V001,35.2,138.9,1,8,0,O'

class ResetTests(unittest.TestCase):
    def test_logs_and_gap_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            r = server.Receiver(Path(folder))
            r.receive(PACKET, base_received_epoch=1000, base_interval_s=99)
            with self.assertRaises(ValueError): r.receive('bad')
            old = {p: p.read_bytes() for p in Path(folder).iterdir()}
            state = r.reset()
            self.assertEqual((state['total'], state['invalid'], state['vehicles']), (0, 0, []))
            self.assertNotIn(r.log_path, old)
            for p, data in old.items():
                self.assertEqual(p.read_bytes(), data)
            r.receive(PACKET, base_received_epoch=1100, base_interval_s=100)
            r.receive(PACKET, base_received_epoch=1105, base_interval_s=5)
            rows = list(csv.DictReader(io.StringIO(r.csv_path.read_text(encoding='utf-8-sig'))))
            gaps = list(csv.DictReader(io.StringIO(r.gaps_path.read_text(encoding='utf-8-sig'))))
            self.assertEqual((rows[0]['interval_s'], rows[0]['gap_detected']), ('', '0'))
            self.assertEqual(len(gaps), 1)
            self.assertEqual(r.snapshot()['vehicles'][0]['count'], 2)

    def test_failed_reset_keeps_session(self):
        with tempfile.TemporaryDirectory() as folder:
            r = server.Receiver(Path(folder)); r.receive(PACKET)
            old = r.log_path
            with patch.object(r, '_new_session', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): r.reset()
            self.assertEqual(r.log_path, old)
            self.assertEqual(r.snapshot()['total'], 1)

    def test_concurrent_packets_and_resets_preserve_all_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            r = server.Receiver(Path(folder))
            def work(i):
                return r.reset() if i % 10 == 0 else r.receive(PACKET)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(work, range(100)))
            records = [line for p in Path(folder).glob('*.jsonl') for line in p.read_text().splitlines()]
            self.assertEqual(len(records), 90)
            self.assertEqual(sum(len(list(csv.DictReader(io.StringIO(p.read_text(encoding='utf-8-sig'))))) for p in Path(folder).glob('*.csv') if not p.name.endswith('-gaps.csv')), 90)

    def test_authenticated_http_reset_and_downloads(self):
        with tempfile.TemporaryDirectory() as folder, patch.multiple(server, VIEW_USER='viewer', VIEW_PASSWORD='secret', API_KEY='pi-key'):
            r = server.Receiver(Path(folder))
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(r, {'type':'FeatureCollection','features':[]}))
            t = threading.Thread(target=http.serve_forever, daemon=True); t.start()
            url = f'http://127.0.0.1:{http.server_port}'
            auth = 'Basic ' + base64.b64encode(b'viewer:secret').decode()
            def request(path, body=None, headers=None):
                req=urllib.request.Request(url+path,data=body,headers=headers or {})
                return urllib.request.urlopen(req)
            try:
                with request('/api/packet',json.dumps({'packet':PACKET}).encode(),{'X-Smeagol-API-Key':'pi-key'}) as response:
                    self.assertEqual(response.status,200)
                old = r.log_path
                for headers, code in (({},401),({'Authorization':auth},403),({'Authorization':auth,'Origin':'https://other.example'},403),({'X-Smeagol-API-Key':'pi-key','Origin':url},401)):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        request('/api/reset',b'',headers)
                    self.assertEqual(error.exception.code,code)
                    self.assertEqual(r.log_path,old)
                with request('/api/reset',b'',{'Authorization':auth,'Origin':url}) as response:
                    state=json.load(response)
                self.assertEqual(state['total'],0)
                self.assertNotEqual(state['log'],old.name)
                for path in ('/api/state','/api/course','/api/log/current.csv','/api/log/current-gaps.csv','/'):
                    with request(path,headers={'Authorization':auth}) as response:
                        self.assertEqual(response.status,200)
                        if path=='/': self.assertIn('id="reset"',response.read().decode())
                with request('/api/health') as response: self.assertEqual(response.status,200)
            finally:
                http.shutdown(); http.server_close(); t.join()

if __name__ == '__main__': unittest.main()
