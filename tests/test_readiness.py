"""开办就绪业务规则测试：容量核定、闸门冻结、报验幂等、关键路径、
补助台账、角色视图、双时态历史。"""

import unittest
from datetime import date

from readiness import Engine, Store
from service import App

SITE = "YX-STREET-03"
STREET = "永昌街道"


class Scenario:
    """构造一个两班型场地的完整开办事件流。"""

    def __init__(self, today="2026-10-01"):
        self.today = today
        self.store = Store(clock=lambda: date.fromisoformat(today))
        self.eng = Engine(self.store)
        self.app = App(self.store, today=date.fromisoformat(today))

    def reg(self):
        self.store.append("site_registered", "2026-06-01",
                          {"site_id": SITE, "street": STREET, "name": "永昌党群中心托育点"})

    def add_class(self, kind="托小班", proposed=18, rooms=None, on="2026-06-05"):
        rooms = rooms if rooms is not None else [
            {"room_id": "R1", "area_m2": 40.0, "natural_light": True},
            {"room_id": "R2", "area_m2": 20.0, "natural_light": False},
        ]
        self.store.append("class_added", on,
                          {"site_id": SITE, "kind": kind, "age_months": [18, 30],
                           "capacity_proposed": proposed, "rooms": rooms})

    def sign_site(self, gate, party_on="2026-06-10", evidence=None):
        self.store.append("gate_signed", party_on,
                          {"site_id": SITE, "gate": gate, "evidence": evidence or {}})

    def submit(self, kind, gate, report_no, on):
        self.store.append("inspection_submitted", on,
                          {"site_id": SITE, "kind": kind, "gate": gate, "report_no": report_no})

    def sign_class(self, kind, gate, on, evidence=None, report_no=None, valid_until=None):
        payload = {"site_id": SITE, "kind": kind, "gate": gate, "evidence": evidence or {}}
        if report_no:
            payload["report_no"] = report_no
        if valid_until:
            payload["valid_until"] = valid_until
        self.store.append("gate_signed", on, payload)

    def reject(self, kind, gate, on, reason):
        self.store.append("gate_rejected", on,
                          {"site_id": SITE, "kind": kind, "gate": gate, "reason": reason})

    def full_chain(self, kind="托小班", start="2026-06-10", filed=90.0, fire_cap=12, teachers=2):
        """签满除 fee_commitment 外的链路，使班型达到 ready。"""
        d = date.fromisoformat
        self.sign_site("property_consent", start)
        self.sign_site("design_change", d(start).isoformat())
        self.sign_class(kind, "construction", "2026-08-01")
        self.submit(kind, "fire_inspection", "F-1", "2026-08-02")
        self.submit(kind, "health_inspection", "H-1", "2026-08-02")
        self.sign_class(kind, "fire_inspection", "2026-08-10",
                        {"max_capacity": fire_cap}, report_no="F-1")
        self.sign_class(kind, "health_inspection", "2026-08-11", {}, report_no="H-1")
        self.sign_class(kind, "institution_license", "2026-08-20", {"filed_area_m2": filed})
        self.sign_class(kind, "staffing", "2026-08-25", {"qualified_teachers": teachers})
        self.sign_site("fee_commitment", "2026-08-26")

    def state(self):
        sites = self.eng.replay(as_of=self.today)
        return sites[SITE]

    def readiness(self, kind="托小班"):
        site = self.state()
        return self.eng.class_readiness(site, site.classes[kind], date.fromisoformat(self.today))


class CapacityTest(unittest.TestCase):
    def test_four_constraints_take_minimum(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.full_chain(filed=90.0, fire_cap=12, teachers=2)
        r = s.readiness()
        c = r["capacity"]["constraints"]
        # 备案 90/7=12；采光 40/3=13；消防 12；教师 2*7=14 → 最小 12（拟定 18）
        self.assertEqual(c["filed_area"], 12)
        self.assertEqual(c["natural_light"], 13)
        self.assertEqual(c["fire_egress"], 12)
        self.assertEqual(c["staffing"], 14)
        self.assertEqual(r["capacity"]["approved"], 12)
        self.assertTrue(r["ready"])

    def test_unapproved_capacity_not_visible_until_all_gates(self):
        s = Scenario()
        s.reg(); s.add_class()
        # 只走到施工
        s.sign_site("property_consent"); s.sign_site("design_change")
        s.sign_class("托小班", "construction", "2026-08-01")
        r = s.readiness()
        self.assertFalse(r["ready"])
        self.assertEqual(r["capacity"]["publicly_visible"], 0)  # 承诺的 18 不得出现

    def test_short_staff_floors_capacity(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.full_chain(teachers=1)  # 只有 1 名持证教师，不足每班 2 名
        r = s.readiness()
        self.assertEqual(r["capacity"]["constraints"]["staffing"], 0)
        self.assertEqual(r["capacity"]["approved"], 0)
        self.assertFalse(r["ready"])


class OpeningTest(unittest.TestCase):
    def test_open_must_equal_approved_not_promised(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        # 试图按承诺 18 开园 → 拒绝
        with self.assertRaises(PermissionError):
            s.app.append_event(
                {"type": "class_opened", "on": "2026-09-01",
                 "payload": {"site_id": SITE, "kind": "托小班", "capacity_approved": 18}},
                role="street", street=STREET)
        # 核准 12 → 放行
        ok = s.app.append_event(
            {"type": "class_opened", "on": "2026-09-01",
             "payload": {"site_id": SITE, "kind": "托小班", "capacity_approved": 12}},
            role="street", street=STREET)
        self.assertTrue(ok["ok"])
        self.assertEqual(s.readiness()["open_capacity"], 12)


class InspectionTest(unittest.TestCase):
    def test_duplicate_report_does_not_advance(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.sign_site("property_consent"); s.sign_site("design_change")
        s.sign_class("托小班", "construction", "2026-08-01")
        s.submit("托小班", "fire_inspection", "F-1", "2026-08-02")
        before = len(s.store.events())
        # 承包方重复报验同一单号：落库但不产生新推进
        s.submit("托小班", "fire_inspection", "F-1", "2026-08-03")
        site = s.state()
        self.assertEqual(site.classes["托小班"].reports["fire_inspection"], "F-1")
        # 报验本身绝不把闸门变合格
        self.assertEqual(s.readiness()["gate_states"]["fire_inspection"], "pending")
        self.assertGreaterEqual(len(s.store.events()), before)

    def test_sign_requires_matching_report(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.sign_site("property_consent"); s.sign_site("design_change")
        s.sign_class("托小班", "construction", "2026-08-01")
        with self.assertRaises(ValueError):
            s.app.append_event(
                {"type": "gate_signed", "on": "2026-08-10",
                 "payload": {"site_id": SITE, "kind": "托小班", "gate": "fire_inspection",
                             "evidence": {"max_capacity": 12}, "report_no": "F-X"}},
                role="fire")


class FreezeScopeTest(unittest.TestCase):
    def _two_classes(self):
        s = Scenario()
        s.reg()
        s.add_class("托小班", 18)
        s.add_class("托大班", 12, rooms=[
            {"room_id": "R3", "area_m2": 42.0, "natural_light": True}], on="2026-06-06")
        s.full_chain("托小班")
        s.full_chain("托大班", filed=90.0, fire_cap=12, teachers=2)
        return s

    def test_rejection_freezes_only_affected_class(self):
        s = self._two_classes()
        # 两班型均已开园，家长可见 12
        for k in ("托小班", "托大班"):
            s.store.append("class_opened", "2026-09-01",
                           {"site_id": SITE, "kind": k, "capacity_approved": 12})
        # 托小班消防复验不通过（晚于原签署的新拒收）
        s.reject("托小班", "fire_inspection", "2026-09-15", "疏散通道堆物")
        small, big = s.readiness("托小班"), s.readiness("托大班")
        self.assertIn("fire_inspection", small["frozen_gates"])
        self.assertEqual(small["capacity"]["publicly_visible"], 0)
        self.assertTrue(big["ready"])  # 托大班不受影响
        self.assertEqual(big["capacity"]["publicly_visible"], 12)
        # 公众侧：托小班暂停且不显示名额，托大班仍开放
        pub = s.eng.public_view(s.state(), date.fromisoformat(s.today))
        st = {c["kind"]: c["status"] for c in pub["classes"]}
        self.assertEqual(st["托小班"], "paused")
        self.assertEqual(st["托大班"], "open")

    def test_expired_material_freezes_only_class(self):
        s = self._two_classes()
        # 托小班消防材料 2026-09-01 过期
        s.sign_class("托小班", "fire_inspection", "2026-08-10",
                     {"max_capacity": 12}, report_no="F-1", valid_until="2026-09-01")
        small = s.readiness("托小班")
        self.assertEqual(next(g for g in small["gates"] if g["gate"] == "fire_inspection")["state"],
                         "expired")
        self.assertEqual(small["capacity"]["publicly_visible"], 0)
        self.assertTrue(s.readiness("托大班")["ready"])


class ClassChangeTest(unittest.TestCase):
    def test_change_invalidates_class_and_design_signs(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        self.assertTrue(s.readiness()["ready"])
        # 班型由 18 拟改为 20、房间调整
        s.store.append("class_changed", "2026-09-10",
                       {"site_id": SITE, "kind": "托小班", "capacity_proposed": 20,
                        "rooms": [{"room_id": "R1", "area_m2": 60.0, "natural_light": True}]})
        r = s.readiness()
        self.assertEqual(r["generation"], 2)
        # class 级签署与设计变更全部失效
        self.assertIn("design_change", [g["gate"] for g in r["gates"] if g["state"] != "approved"])
        self.assertFalse(r["ready"])
        cp = s.eng.critical_path(s.state(), s.state().classes["托小班"], date(2026, 9, 10))
        self.assertIn("design_change", cp["critical_path"])  # 关键路径重算


class StagedOpeningTest(unittest.TestCase):
    def test_staged_open_shows_actual_dates(self):
        s = Scenario()
        s.reg()
        s.add_class("托小班", 18)
        s.add_class("托大班", 12, rooms=[{"room_id": "R3", "area_m2": 42.0, "natural_light": True}],
                    on="2026-06-06")
        s.full_chain("托小班")
        s.store.append("class_opened", "2026-09-01",
                       {"site_id": SITE, "kind": "托小班", "capacity_approved": 12})
        # 托大班尚未具备条件
        s.sign_site("property_consent"); s.sign_site("design_change"); s.sign_site("fee_commitment")
        pub = s.eng.public_view(s.state(), date(2026, 9, 5))
        a = next(c for c in pub["classes"] if c["kind"] == "托小班")
        b = next(c for c in pub["classes"] if c["kind"] == "托大班")
        self.assertEqual(a["status"], "open")
        self.assertEqual(a["actual_open_date"], "2026-09-01")
        self.assertEqual(a["visible_slots"], 12)
        self.assertEqual(b["status"], "preparing")
        self.assertIsNone(b["actual_open_date"])
        self.assertEqual(pub["visible_total"], 12)


class SubsidyTest(unittest.TestCase):
    def _opened(self):
        s = Scenario(today="2026-10-01")
        s.reg(); s.add_class(); s.full_chain()
        s.store.append("class_opened", "2026-09-01",
                       {"site_id": SITE, "kind": "托小班", "capacity_approved": 12})
        return s

    def test_disburse_on_verifiable_milestone(self):
        s = self._opened()
        led = s.eng.subsidy_ledger(SITE)
        dis = [e for e in led["entries"] if e["entry"] == "disburse"]
        self.assertEqual(len(dis), 1)
        self.assertEqual(dis[0]["slots"], 12)
        self.assertEqual(dis[0]["basis"], "class_opened")
        self.assertAlmostEqual(dis[0]["amount"], round(2400 * 12 * 122 / 365, 2))

    def test_clawback_on_capacity_reduction(self):
        s = self._opened()
        # 9-15 核减到 10（重走后再次开园不需要，直接班型变更事件触发追回）
        s.store.append("class_changed", "2026-09-15",
                       {"site_id": SITE, "kind": "托小班", "capacity_proposed": 10})
        led = s.eng.subsidy_ledger(SITE)
        cb = [e for e in led["entries"] if e["entry"] == "clawback"]
        self.assertTrue(cb)
        self.assertEqual(cb[0]["slots"], 2)
        self.assertLess(cb[0]["amount"], 0)

    def test_freeze_clawback_is_single_and_reversible(self):
        s = self._opened()
        s.reject("托小班", "fire_inspection", "2026-09-20", "复验不通过")
        led1 = s.eng.subsidy_ledger(SITE)
        frozen = [e for e in led1["entries"] if e["basis"] == "inspection_failed"]
        self.assertEqual(len(frozen), 1)          # 幂等：只一笔
        self.assertLess(frozen[0]["amount"], 0)
        # 再次计算不叠加
        self.assertEqual(len([e for e in s.eng.subsidy_ledger(SITE)["entries"]
                              if e["basis"] == "inspection_failed"]), 1)
        # 重新报验合格（班型代次未变，重签覆盖）→ 恢复拨付
        s.submit("托小班", "fire_inspection", "F-2", "2026-09-22")
        s.sign_class("托小班", "fire_inspection", "2026-09-25",
                     {"max_capacity": 12}, report_no="F-2")
        led2 = s.eng.subsidy_ledger(SITE)
        rev = [e for e in led2["entries"] if e["basis"] == "reverified"]
        self.assertEqual(len(rev), 1)
        self.assertGreater(rev[0]["amount"], 0)


class RoleViewTest(unittest.TestCase):
    def test_street_scoped_to_own_jurisdiction(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        # 其他街道看不到
        self.assertEqual(s.app.street_sites("其他街道"), [])
        mine = s.app.street_sites(STREET)
        self.assertEqual(len(mine), 1)
        # 其他街道不能写
        with self.assertRaises(PermissionError):
            s.app.append_event(
                {"type": "class_added", "payload": {"site_id": SITE, "kind": "乳儿班",
                                                    "capacity_proposed": 6, "age_months": [6, 12]}},
                role="street", street="其他街道")

    def test_inspector_sees_only_duty_materials(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        fire = s.app.inspect(SITE, "fire")
        gates = {m["gate"] for c in fire["classes"] for m in c["materials"]}
        self.assertEqual(gates, {"construction", "fire_inspection"})  # 不含 staffing/许可
        # 非职责角色拒绝
        with self.assertRaises(PermissionError):
            s.app.inspect(SITE, "operator")

    def test_signature_party_enforcement(self):
        s = Scenario()
        s.reg(); s.add_class()
        with self.assertRaises(PermissionError):
            s.app.append_event(
                {"type": "gate_signed", "payload": {"site_id": SITE, "gate": "property_consent"}},
                role="street", street=STREET)  # 须 owner

    def test_public_view_is_masked(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        s.store.append("class_opened", "2026-09-01",
                       {"site_id": SITE, "kind": "托小班", "capacity_approved": 12})
        pub = s.app.public(SITE)
        blob = str(pub)
        self.assertNotIn("proposed", blob)       # 拟定 18 不泄露
        self.assertNotIn("filed_area", blob)
        self.assertEqual(pub["visible_total"], 12)


class HistoryTest(unittest.TestCase):
    def test_reconstruct_commitments_at_any_date(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        s.store.append("class_opened", "2026-09-01",
                       {"site_id": SITE, "kind": "托小班", "capacity_approved": 12})
        # 8 月 1 日：尚未开园，对外看不到名额与日期
        h = s.app.history(SITE, "2026-08-01")
        self.assertEqual(h["classes"][0]["visible_slots"], 0)
        self.assertIsNone(h["classes"][0]["actual_open_date"])
        # 9 月 2 日：已开放 12
        h2 = s.app.history(SITE, "2026-09-02")
        self.assertEqual(h2["classes"][0]["visible_slots"], 12)
        self.assertEqual(h2["classes"][0]["actual_open_date"], "2026-09-01")

    def test_late_recorded_event_hidden_by_record_time(self):
        s = Scenario()
        s.reg(); s.add_class(); s.full_chain()
        # 9-10 才补录一笔业务日为 8-15 的开园；还原 8-27 视角：
        # recorded_by=2026-09-09 时该补录尚不存在，对外仍为 0
        s.store.append("class_opened", "2026-08-15",
                       {"site_id": SITE, "kind": "托小班", "capacity_approved": 12},
                       recorded_at="2026-09-10")
        h = s.app.history(SITE, "2026-08-27", recorded_by="2026-09-09")
        self.assertEqual(h["classes"][0]["visible_slots"], 0)
        h2 = s.app.history(SITE, "2026-08-27", recorded_by="2026-09-11")
        self.assertEqual(h2["classes"][0]["visible_slots"], 12)


class CriticalPathTest(unittest.TestCase):
    def test_projection_and_delay(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.sign_site("property_consent", "2026-06-05")
        s.sign_site("design_change", "2026-06-20")
        s.sign_class("托小班", "construction", "2026-08-01")
        cp = s.eng.critical_path(s.state(), s.state().classes["托小班"], date(2026, 8, 2))
        # 施工已完成，后续消防/卫生尚未完成 → 进入延迟清单，预测日晚于今天
        self.assertIn("fire_inspection", cp["delayed_gates"])
        self.assertGreater(cp["projected_open"], "2026-08-02")
        # 链上包含设计→施工→检查→许可→人员
        self.assertEqual(cp["critical_path"][0], "property_consent")
        self.assertIn("staffing", cp["critical_path"])


class ExplainTest(unittest.TestCase):
    def test_explain_partial_capacity_and_pending(self):
        s = Scenario()
        s.reg(); s.add_class()
        s.sign_site("property_consent"); s.sign_site("design_change")
        s.sign_class("托小班", "construction", "2026-08-01")
        ex = s.eng.explain(s.state(), s.state().classes["托小班"], date(2026, 10, 1))
        self.assertEqual(ex["proposed"], 18)
        self.assertEqual(ex["approved"], 0)
        self.assertTrue(any("消防" in r or "教师" in r or "备案" in r or "采光" in r
                            for r in ex["why_partial"]))
        parties = {a["party"] for a in ex["pending_actions"]}
        self.assertIn("fire", parties)
        self.assertIn("operator", parties)


if __name__ == "__main__":
    unittest.main()
