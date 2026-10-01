"""普惠托育「开办就绪」服务入口。

在原健康检查之上挂载 JSON API：
- POST /events                      追加领域事件（按 X-Role / X-Street 授权）
- GET  /sites/<id>/readiness        专班视图：闸门、容量、冻结、关键路径
- GET  /sites/<id>/explain?kind=    专班解释：为何只开放部分托位/谁未完成
- GET  /sites/<id>/ledger           补助拨付与追回分录
- GET  /sites/<id>/public           公众脱敏进度与实际开放日期
- GET  /sites/<id>/inspect?role=    检查部门职责材料（fire/health/...）
- GET  /sites?street=               街道：仅本辖区项目
- GET  /sites/<id>/history?as_of=..[&recorded_by=..]  按历史日期还原对外承诺
"""

import argparse
import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from readiness import Engine, Store, load_contract

SERVICE_ID = "inclusive-childcare-allocation"
SERVICE_NAME = "普惠托育公平分配"

# 街道可发出的事件
STREET_EVENTS = {
    "site_registered", "class_added", "class_changed",
    "plan_revised", "class_opened",
}


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class App:
    """领域应用服务：授权 + 事件追加 + 查询。"""

    def __init__(self, store: Store | None = None, today: date | None = None):
        self.store = store or Store()
        self.engine = Engine(self.store)
        self._today = today

    @property
    def today(self) -> date:
        return self._today or date.today()

    # ----- 写入 ---------------------------------------------------------- #
    def append_event(self, event: dict, role: str, street: str | None = None) -> dict:
        etype = event.get("type")
        on = event.get("on") or self.today.isoformat()
        payload = event.get("payload") or {}
        date.fromisoformat(on)  # 校验

        if etype == "site_registered":
            self._require(role == "street" and street == payload.get("street"),
                          "只有项目所在街道可登记场地")
        elif etype in STREET_EVENTS:
            site = self._require_site(payload.get("site_id"))
            self._require(role == "street" and street == site.street,
                          f"只有本辖区（{site.street}）街道可操作")
            if etype == "class_opened":
                self._validate_opening(site.site_id, payload, on)
        elif etype == "inspection_submitted":
            site = self._require_site(payload.get("site_id"))
            self._require(role == "contractor", "报验由承包方发起")
            self._require(payload.get("report_no") and payload.get("kind") and payload.get("gate"),
                          "报验须带 report_no/kind/gate")
        elif etype in ("gate_signed", "gate_rejected"):
            gate = payload.get("gate")
            self._require(gate in self.engine.gates, f"未知闸门 {gate}")
            conf = self.engine.gates[gate]
            self._require(role == conf["party"],
                          f"闸门「{conf['name']}」须由责任方 {conf['party']} 签署")
            if conf["scope"] == "class":
                self._require(payload.get("kind"), "class 级闸门必须指定 kind")
        else:
            raise PermissionError(f"未知事件类型 {etype}")

        # 落库前在重放快照上试应用，拒绝无法回放的事件（如未报验先签署）
        tentative = {"seq": 0, "type": etype, "on": on, "recorded_at": on, "payload": payload}
        probe = self.engine.replay(as_of=on)
        try:
            self.engine._apply(probe, tentative, date.fromisoformat(on))
        except ValueError as exc:
            raise ValueError(f"事件校验失败: {exc}") from exc

        ev = self.store.append(etype, on, payload)
        return {"ok": True, "seq": ev["seq"], "recorded_at": ev["recorded_at"]}

    def _validate_opening(self, site_id: str, payload: dict, on: str) -> None:
        # 开园里程碑只能确认当前已核准的容量，杜绝“按承诺数拨付”
        sites = self.engine.replay(as_of=on)
        site = sites.get(site_id)
        self._require(site is not None, "场地未登记")
        cs = site.classes.get(payload.get("kind"))
        self._require(cs is not None, "班型不存在")
        r = self.engine.class_readiness(site, cs, date.fromisoformat(on))
        self._require(r["ready"], "闸门未全部核准，不得开园")
        approved = r["capacity"]["approved"]
        claimed = int(payload.get("capacity_approved", -1))
        self._require(claimed == approved,
                      f"开园容量只能取核准值 {approved}（拟定 {r['proposed']}），不得按承诺数填报")

    # ----- 查询 ---------------------------------------------------------- #
    def readiness(self, site_id: str) -> dict:
        site = self._require_site(site_id)
        r = self.engine.site_readiness(site, self.today)
        r["classes"] = [
            {**c, "critical_path": self.engine.critical_path(site, site.classes[c["kind"]], self.today)}
            for c in r["classes"]
        ]
        return r

    def explain(self, site_id: str, kind: str) -> dict:
        site = self._require_site(site_id)
        return self.engine.explain(site, site.classes[kind], self.today)

    def ledger(self, site_id: str) -> dict:
        return self.engine.subsidy_ledger(site_id)

    def public(self, site_id: str) -> dict:
        return self.engine.public_view(self._require_site(site_id), self.today)

    def inspect(self, site_id: str, role: str) -> dict:
        from readiness import INSPECTOR_GATES
        self._require(role in INSPECTOR_GATES, "该角色无检查材料视图")
        return self.engine.inspector_view(role, self._require_site(site_id), self.today)

    def street_sites(self, street: str) -> list[dict]:
        sites = self.engine.replay(as_of=self.today.isoformat()).values()
        return self.engine.street_view(street, sites, self.today)

    def history(self, site_id: str, as_of: str, recorded_by: str | None = None) -> dict:
        return self.engine.as_of_commitments(site_id, as_of, recorded_by)

    def _require_site(self, site_id: str):
        sites = self.engine.replay(as_of=self.today.isoformat())
        if site_id not in sites:
            raise ValueError(f"场地 {site_id} 未登记")
        return sites[site_id]

    @staticmethod
    def _require(cond: bool, msg: str, value=None):
        if not cond:
            raise PermissionError(msg)
        return value


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    app: App = None  # 由 main 注入到类属性

    def _send(self, code: int, obj: dict | list):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hdr(self, name: str) -> str | None:
        """还原请求头：http.server 按 latin-1 解析，非 ASCII（如中文街道名）需还原为 UTF-8。"""
        raw = self.headers.get(name)
        if raw is None:
            return None
        try:
            return raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return raw

    def do_GET(self):
        try:
            target = self.path.encode("latin-1").decode("utf-8")  # 还原 URL 中的 UTF-8
        except (UnicodeEncodeError, UnicodeDecodeError):
            target = self.path
        try:
            parts = urlsplit(target)
            if parts.path == "/health":
                self._send(200, health_payload()); return
            q = parse_qs(parts.query)
            m = re.fullmatch(r"/sites/([^/]+)/(readiness|explain|ledger|public|inspect|history)", parts.path)
            if parts.path == "/sites":
                street = self._hdr("X-Street") or (q.get("street") or [None])[0]
                if not street:
                    self._send(403, {"error": "需要 X-Street"}); return
                self._send(200, self.app.street_sites(street)); return
            if m:
                site_id, action = m.group(1), m.group(2)
                if action == "readiness":
                    self._send(200, self.app.readiness(site_id))
                elif action == "explain":
                    self._send(200, self.app.explain(site_id, q["kind"][0]))
                elif action == "ledger":
                    self._send(200, self.app.ledger(site_id))
                elif action == "public":
                    self._send(200, self.app.public(site_id))
                elif action == "inspect":
                    self._send(200, self.app.inspect(site_id, q["role"][0]))
                elif action == "history":
                    self._send(200, self.app.history(site_id, q["as_of"][0],
                                                     (q.get("recorded_by") or [None])[0]))
                return
            self._send(404, {"error": "not found"})
        except PermissionError as e:
            self._send(403, {"error": str(e)})
        except (KeyError, ValueError) as e:
            self._send(400, {"error": str(e)})

    def do_POST(self):
        if self.path != "/events":
            self._send(404, {"error": "not found"}); return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            event = json.loads(raw.decode("utf-8"))
            result = self.app.append_event(
                event,
                role=self._hdr("X-Role") or "public",
                street=self._hdr("X-Street"),
            )
            self._send(201, result)
        except PermissionError as e:
            self._send(403, {"error": str(e)})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, *_args):
        return


def build_server(port: int, app: App | None = None) -> ThreadingHTTPServer:
    Handler.app = app or App()
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert load_contract()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    build_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
