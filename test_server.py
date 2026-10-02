import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from server import Receiver, parse_packet, load_course, make_handler, ROOT

SAMPLE = 'V001,35.245174,138.918307,1.0,8,0.00,0.00,0.00,0.00,0.0,OK'


class ReceiverTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(parse_packet(SAMPLE)['lat'], 35.245174)
        for bad in ('garbage', SAMPLE.replace('35.245174','NaN'),
                    SAMPLE.replace('35.245174','91'), SAMPLE.replace(',8,',',8.2,'),
                    SAMPLE.replace(',OK',',UNKNOWN')):
            with self.assertRaises(ValueError):
                parse_packet(bad)

    def test_real_course(self):
        course = load_course(ROOT / 'data' / 'JRC2SR26 Leg1.kml')
        self.assertEqual(len(course['features']), 39)
        self.assertTrue(any(f['geometry']['type']=='LineString' for f in course['features']))

    def test_http_logging_movement_and_sources(self):
        with tempfile.TemporaryDirectory() as d:
            receiver = Receiver(Path(d))
            server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(receiver,load_course(ROOT/'data'/'JRC2SR26 Leg1.kml')))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def post(packet,source='http'):
                req=urllib.request.Request(base+'/api/packet',data=json.dumps(dict(packet=packet,source=source)).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req) as r:
                    return json.load(r)
            try:
                post(SAMPLE)
                post(SAMPLE.replace('35.245174','35.246174').replace('OK','SOS'))
                post(SAMPLE,'demo')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('invalid')
                self.assertEqual(error.exception.code,400)
                with urllib.request.urlopen(base+'/api/state') as r:
                    state=json.load(r)
                self.assertEqual((state['total'],state['invalid']),(4,1))
                self.assertEqual(len(state['vehicles']),2)
                vehicle=next(v for v in state['vehicles'] if v['source']=='http')
                self.assertEqual(vehicle['count'],2)
                self.assertEqual(vehicle['lat'],35.246174)
                self.assertEqual(vehicle['status'],'SOS')
                self.assertIsNotNone(vehicle['interval_s'])
                records=[json.loads(s) for s in receiver.log_path.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(records),4)
                self.assertIn('error',records[-1])
                with urllib.request.urlopen(base+'/') as r:
                    self.assertIn(b'Smeagol',r.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__=='__main__':
    unittest.main()
