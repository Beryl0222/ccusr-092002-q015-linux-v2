"""HTTP 层集成测试：授权、中文请求头/查询编码、视图路由。"""

import json
import unittest
from datetime import date

from readiness import Store
from service import App, build_server

SITE = "YX-STREET-03"
STREET = "永昌街道"


def make_server():
    store = Store(clock=lambda: date(2026, 10, 1))
    app = App(store, today=date(2026, 10, 1))
    httpd = build_server(0, app)
    return httpd, app


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.httpd, self.app = make_server()
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        import urllib.request
        self.urlopen = urllib.request.urlopen
        self.Request = urllib.request.Request
        host, port = self.httpd.server_address
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, body, role=None, street=None, raw_street=False):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
        if street is not None:
            # 故意按 UTF-8 字节直传中文头，模拟 curl，验证服务端还原
            headers["X-Street"] = street.encode("utf-8").decode("latin-1")
        req = self.Request(self.base + "/events", data=data, headers=headers, method="POST")
        try:
            with self.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except Exception as e:
            return e.code, json.loads(e.read())

    def _get(self, path, street=None):
        headers = {}
        if street is not None:
            headers["X-Street"] = street.encode("utf-8").decode("latin-1")
        req = self.Request(self.base + path, headers=headers)
        try:
            with self.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except Exception as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        code, body = self._get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["service"], "inclusive-childcare-allocation")

    def test_chinese_street_header_authorization(self):
        # 错误街道：403
        code, body = self._post(
            {"type": "site_registered",
             "payload": {"site_id": SITE, "street": STREET, "name": "x"}},
            role="street", street="其他街道")
        self.assertEqual(code, 403)
        # 本街道（UTF-8 中文头）：201
        code, body = self._post(
            {"type": "site_registered",
             "payload": {"site_id": SITE, "street": STREET, "name": "党群点"}},
            role="street", street=STREET)
        self.assertEqual(code, 201)
        # 街道列表按辖区隔离
        code, body = self._get("/sites", street=STREET)
        self.assertEqual(code, 200)
        self.assertEqual(len(body), 1)
        code, body = self._get("/sites", street="别的街道")
        self.assertEqual(body, [])

    def test_party_must_sign_own_gate(self):
        self._post({"type": "site_registered",
                    "payload": {"site_id": SITE, "street": STREET, "name": "x"}},
                   role="street", street=STREET)
        # 街道不能替产权方签署
        code, body = self._post(
            {"type": "gate_signed", "payload": {"site_id": SITE, "gate": "property_consent"}},
            role="street", street=STREET)
        self.assertEqual(code, 403)
        # 产权方可以
        code, _ = self._post(
            {"type": "gate_signed", "payload": {"site_id": SITE, "gate": "property_consent"}},
            role="owner")
        self.assertEqual(code, 201)

    def test_chinese_query_param_kind(self):
        self._post({"type": "site_registered",
                    "payload": {"site_id": SITE, "street": STREET, "name": "x"}},
                   role="street", street=STREET)
        code, body = self._get(f"/sites/{SITE}/public")
        self.assertEqual(code, 200)
        self.assertEqual(body["classes"], [])
        # 未登记场地 → 400
        code, body = self._get("/sites/NOPE/public")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
