"""开办就绪领域引擎：事件溯源 + 闸门推导 + 关键路径 + 补助台账 + 角色视图。

事件只追加（append-only），业务时间 ``on`` 与记录时间 ``recorded_at`` 分离：
- 状态推导按业务时间 ``on`` 重放；
- 历史视图（as_of）同时受 on 与 recorded_at 双重约束，可还原“某历史日期
  当时对外承诺过什么”。

线程安全：内部一把可重入锁，服务层多线程 HTTP 可直接共用一个 Store。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

CONTRACT_PATH = Path(__file__).parent / "contracts" / "childcare_capacity.json"

# 检查部门（角色）→ 可见闸门材料
INSPECTOR_GATES: dict[str, list[str]] = {
    "fire": ["construction", "fire_inspection"],
    "health": ["construction", "health_inspection"],
    "health_commission": ["institution_license", "staffing"],
    "price_authority": ["fee_commitment"],
}

# 检查材料的有效期（天）；None 表示长期有效
_EXPIRY = {"fire_inspection": 365, "health_inspection": 365}
# 需要先报验、由责任方签署才算通过的闸门
INSPECTION_GATES = {"fire_inspection", "health_inspection"}


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _ds(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 事件存储
# --------------------------------------------------------------------------- #
class Store:
    """append-only 事件存储。recorded_at 由存储层统一签发（单调不回退）。"""

    def __init__(self, clock: Optional[callable] = None):
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._clock = clock or date.today
        self._seq = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append(self, type_: str, on: str, payload: dict[str, Any],
               recorded_at: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "type": type_,
                "on": on,
                # 默认业务发生当天录入；补录事件显式传入更晚的 recorded_at
                "recorded_at": recorded_at or on,
                "payload": dict(payload),
            }
            self._events.append(ev)
            return ev

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._seq = 0


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
@dataclass
class GateSign:
    gate: str
    on: date
    expires_on: Optional[date]
    evidence: dict[str, Any]
    seq: int
    generation: int
    report_no: Optional[str] = None


@dataclass
class Rejection:
    gate: str
    on: date
    reason: str
    evidence: dict[str, Any]
    seq: int


@dataclass
class ClassState:
    kind: str
    age_months: list[int]
    proposed: int
    rooms: list[dict[str, Any]]
    added_on: date
    generation: int = 1
    signs: dict[str, GateSign] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)
    reports: dict[str, str] = field(default_factory=dict)  # gate -> report_no
    opened_on: Optional[date] = None
    open_capacity: Optional[int] = None
    last_changed: Optional[date] = None


@dataclass
class SiteState:
    site_id: str
    street: str
    name: str
    registered_on: date
    classes: dict[str, ClassState] = field(default_factory=dict)
    site_signs: dict[str, GateSign] = field(default_factory=dict)
    site_rejections: list[Rejection] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    design_generation: int = 1


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, store: Store, contract: Optional[dict[str, Any]] = None):
        self.store = store
        self.contract = contract or load_contract()
        self.policy = self.contract["policy"]
        self.gates = {g["gate"]: g for g in self.contract["gate_chain"]}
        self.order = [g["gate"] for g in self.contract["gate_chain"]]

    # ----- 回放 ---------------------------------------------------------- #
    def replay(self, as_of: Optional[str] = None, recorded_by: Optional[str] = None) -> dict[str, SiteState]:
        """重放到 as_of（业务时间）且 recorded_at <= recorded_by 的事件。"""
        cutoff = _ds(as_of)
        rec_cutoff = _ds(recorded_by)
        sites: dict[str, SiteState] = {}
        for ev in self.store.events():
            on = _d(ev["on"])
            recorded = _d(ev["recorded_at"])
            if cutoff is not None and on > cutoff:
                continue
            if rec_cutoff is not None and recorded > rec_cutoff:
                continue
            self._apply(sites, ev, on)
        return sites

    @staticmethod
    def _site(sites: dict[str, SiteState], site_id: str) -> SiteState:
        if site_id not in sites:
            raise ValueError(f"未知场地 {site_id}，请先 site_registered")
        return sites[site_id]

    def _apply(self, sites: dict[str, SiteState], ev: dict[str, Any], on: date) -> None:
        t = ev["type"]
        p = ev["payload"]
        if t == "site_registered":
            sites[ev["payload"]["site_id"]] = SiteState(
                site_id=p["site_id"],
                street=p["street"],
                name=p.get("name", p["site_id"]),
                registered_on=on,
            )
            return
        site = self._site(sites, p["site_id"])

        if t == "plan_revised":
            site.plans.append({"on": on.isoformat(), "days": dict(p["gate_days"]), "seq": ev["seq"]})

        elif t in ("class_added", "class_changed"):
            kind = p["kind"]
            if t == "class_added":
                if kind in site.classes:
                    raise ValueError(f"班型已存在: {kind}")
                site.classes[kind] = ClassState(
                    kind=kind,
                    age_months=list(p.get("age_months", [])),
                    proposed=int(p["capacity_proposed"]),
                    rooms=list(p.get("rooms", [])),
                    added_on=on,
                )
            else:
                cs = self._class(site, kind)
                structural = ("rooms" in p) or ("age_months" in p)
                cs.proposed = int(p.get("capacity_proposed", cs.proposed))
                if "age_months" in p:
                    cs.age_months = list(p["age_months"])
                if "rooms" in p:
                    cs.rooms = list(p["rooms"])
                cs.last_changed = on
                if structural:
                    # 结构（房间布局/年龄段）变更：图纸与该班型 class 级签署失效，须重走
                    cs.generation += 1
                    site.design_generation += 1
                    for gate in list(cs.signs):
                        if self.gates[gate]["scope"] == "class":
                            del cs.signs[gate]
                    cs.rejections.clear()
                    cs.reports.clear()
                # 仅下调拟定容量不破坏既有核准，容量随 min(约束, 拟定) 自动收敛

        elif t == "inspection_submitted":
            # 幂等：同一报验单号重复提交不产生任何推进
            cs = self._class(site, p["kind"])
            gate = p["gate"]
            if gate not in INSPECTION_GATES:
                raise ValueError(f"{gate} 不是报验闸门")
            if cs.reports.get(gate) == p["report_no"]:
                return
            cs.reports[gate] = p["report_no"]

        elif t == "gate_signed":
            self._apply_signed(site, ev, on)

        elif t == "gate_rejected":
            rejection = Rejection(
                gate=p["gate"],
                on=on,
                reason=p.get("reason", ""),
                evidence=p.get("evidence", {}),
                seq=ev["seq"],
            )
            scope = self.gates[p["gate"]]["scope"]
            if scope == "site":
                site.site_rejections.append(rejection)
            else:
                self._class(site, p["kind"]).rejections.append(rejection)

        elif t == "class_opened":
            cs = self._class(site, p["kind"])
            cs.opened_on = on
            cs.open_capacity = int(p["capacity_approved"])

    def _apply_signed(self, site: SiteState, ev: dict[str, Any], on: date) -> None:
        p = ev["payload"]
        gate = p["gate"]
        conf = self.gates[gate]
        expires_days = p.get("expires_days", _EXPIRY.get(gate))
        expires_on = _d(p["valid_until"]) if p.get("valid_until") else (
            None if not expires_days else date.fromordinal(on.toordinal() + int(expires_days))
        )
        sign = GateSign(
            gate=gate,
            on=on,
            expires_on=expires_on,
            evidence=p.get("evidence", {}),
            seq=ev["seq"],
            generation=p.get("generation", 1),
            report_no=p.get("report_no"),
        )
        if conf["scope"] == "site":
            gen = site.design_generation if gate == "design_change" else 1
            sign.generation = gen
            site.site_signs[gate] = sign  # 重签覆盖，seq 更大；不重复触发里程碑
        else:
            cs = self._class(site, p["kind"])
            sign.generation = cs.generation
            if gate in INSPECTION_GATES:
                report_no = p.get("report_no")
                if not report_no:
                    raise ValueError(f"{gate} 签署必须带报验单号 report_no")
                if cs.reports.get(gate) != report_no:
                    raise ValueError(f"报验单 {report_no} 未提交/不匹配，不得签署")
                sign.report_no = report_no
            # 已合格再签（含重复报验单）只更新材料，不产生重复里程碑/拨付
            cs.signs[gate] = sign

    @staticmethod
    def _class(site: SiteState, kind: str) -> ClassState:
        if kind not in site.classes:
            raise ValueError(f"场地 {site.site_id} 不存在班型 {kind}")
        return site.classes[kind]

    # ----- 容量核定（四项约束取最小）------------------------------------ #
    def capacity_constraints(self, site: SiteState, cs: ClassState, today: Optional[date] = None) -> dict[str, Any]:
        today = today or date.today()
        # 备案明细到班型：许可签署按班型给 filed_area_m2
        lic = cs.signs.get("institution_license")
        filed_area = float(lic.evidence["filed_area_m2"]) if lic and "filed_area_m2" in lic.evidence else 0.0

        activity_area = sum(float(r.get("area_m2", 0)) for r in cs.rooms if r.get("natural_light"))
        teachers = 0
        staff_sign = cs.signs.get("staffing")
        if staff_sign:
            teachers = int(staff_sign.evidence.get("qualified_teachers", 0))
        fire_sign = cs.signs.get("fire_inspection")
        fire_cap = int(fire_sign.evidence.get("max_capacity", cs.proposed)) if fire_sign else cs.proposed

        by_filed = int(filed_area // self.policy["min_m2_per_slot_filed_area"]) if filed_area else cs.proposed
        by_light = int(activity_area // self.policy["min_m2_per_slot_activity"]) if activity_area else 0
        ratio = self.policy["staff_child_ratio"][1]
        # 班型至少配备 qualified_teachers_per_class 名持证教师方可承载，每名教师按师生比折算
        if 0 < teachers < self.policy["qualified_teachers_per_class"]:
            by_staff = 0
        else:
            by_staff = teachers * ratio

        caps = {
            "filed_area": min(by_filed, cs.proposed),
            "natural_light": min(by_light, cs.proposed),
            "fire_egress": min(fire_cap, cs.proposed),
            "staffing": min(by_staff, cs.proposed),
        }
        approved = min(caps.values()) if caps else 0
        return {"proposed": cs.proposed, "constraints": caps, "approved": approved}

    # ----- 闸门就绪判定 -------------------------------------------------- #
    def gate_status(self, site: SiteState, cs: ClassState, gate: str, today: date) -> dict[str, Any]:
        conf = self.gates[gate]
        scope = conf["scope"]
        sign = site.site_signs.get(gate) if scope == "site" else cs.signs.get(gate)
        rejection = self._latest_rejection(site, cs, gate)

        # 前置闸门
        blockers = []
        for dep in conf["deps"]:
            dep_conf = self.gates[dep]
            if dep_conf["scope"] == "site":
                dep_sign = site.site_signs.get(dep)
                dep_ok = bool(dep_sign) and self._sign_valid(site, None, dep, dep_sign, today)
            else:
                dep_sign = cs.signs.get(dep)
                dep_ok = bool(dep_sign) and self._sign_valid(site, cs, dep, dep_sign, today)
            if not dep_ok:
                blockers.append(dep)

        if not sign:
            state = "rejected" if rejection else ("blocked" if blockers else "pending")
            return {"gate": gate, "state": state, "blockers": blockers,
                    "reason": rejection.reason if rejection else None,
                    "signed_on": None, "expires_on": None, "party": conf["party"]}

        # 签署代次失效（班型/设计变更后旧签署）
        if scope == "site":
            stale = gate == "design_change" and sign.generation != site.design_generation
        else:
            stale = sign.generation != cs.generation
        if stale:
            return {"gate": gate, "state": "frozen", "blockers": ["class_changed"],
                    "reason": "班型变更后须重新核准", "signed_on": sign.on.isoformat(),
                    "expires_on": sign.expires_on.isoformat() if sign.expires_on else None,
                    "party": conf["party"]}

        if sign.expires_on and today > sign.expires_on:
            return {"gate": gate, "state": "expired", "blockers": [],
                    "reason": f"材料已于 {sign.expires_on.isoformat()} 过期",
                    "signed_on": sign.on.isoformat(), "expires_on": sign.expires_on.isoformat(),
                    "party": conf["party"]}

        # 合格签署之后又被判定不通过（复验拒收）→ 该班型重新冻结，重签合格后离开
        if rejection and rejection.seq > sign.seq:
            return {"gate": gate, "state": "rejected", "blockers": blockers,
                    "reason": rejection.reason, "signed_on": sign.on.isoformat(),
                    "expires_on": sign.expires_on.isoformat() if sign.expires_on else None,
                    "party": conf["party"]}

        return {"gate": gate, "state": "approved", "blockers": [], "reason": None,
                "signed_on": sign.on.isoformat(),
                "expires_on": sign.expires_on.isoformat() if sign.expires_on else None,
                "party": conf["party"]}

    @staticmethod
    def _latest_rejection(site: SiteState, cs: ClassState, gate: str) -> Optional[Rejection]:
        scope_gate = gate in ("property_consent", "design_change", "fee_commitment")
        pool = site.site_rejections if scope_gate else cs.rejections
        rej = [r for r in pool if r.gate == gate]
        return rej[-1] if rej else None

    def _sign_valid(self, site, cs, gate, sign, today) -> bool:
        if sign.expires_on and today > sign.expires_on:
            return False
        if self.gates[gate]["scope"] == "class" and cs is not None and sign.generation != cs.generation:
            return False
        if gate == "design_change" and sign.generation != site.design_generation:
            return False
        return True

    def class_readiness(self, site: SiteState, cs: ClassState, today: Optional[date] = None) -> dict[str, Any]:
        today = today or date.today()
        gates = [self.gate_status(site, cs, g, today) for g in self.order]
        approved_states = {g["gate"]: g["state"] for g in gates}
        all_approved = all(g["state"] == "approved" for g in gates)
        cap = self.capacity_constraints(site, cs, today)
        frozen = [g["gate"] for g in gates if g["state"] in ("expired", "rejected", "frozen")]
        # 名额对外可见需同时满足：闸门全核准 且 已到达开园里程碑（实际/预定开园日）
        opened = cs.opened_on is not None and today >= cs.opened_on
        public_capacity = cap["approved"] if all_approved and opened else 0
        return {
            "kind": cs.kind,
            "age_months": cs.age_months,
            "proposed": cs.proposed,
            "capacity": {**cap, "publicly_visible": public_capacity},
            "gates": gates,
            "gate_states": approved_states,
            "ready": all_approved and cap["approved"] > 0,
            "frozen_gates": frozen,
            "opened_on": cs.opened_on.isoformat() if cs.opened_on else None,
            "is_open": opened,
            "open_capacity": cs.open_capacity if opened else None,
            "generation": cs.generation,
        }

    def site_readiness(self, site: SiteState, today: Optional[date] = None) -> dict[str, Any]:
        today = today or date.today()
        classes = [self.class_readiness(site, cs, today) for cs in site.classes.values()]
        return {
            "site_id": site.site_id,
            "name": site.name,
            "street": site.street,
            "registered_on": site.registered_on.isoformat(),
            "classes": classes,
            "proposed_total": sum(c["proposed"] for c in classes),
            "approved_total": sum(c["capacity"]["approved"] for c in classes),
            "visible_total": sum(c["capacity"]["publicly_visible"] for c in classes),
            "open_total": sum(c["open_capacity"] or 0 for c in classes),
        }

    # ----- 关键路径 ------------------------------------------------------ #
    def critical_path(self, site: SiteState, cs: ClassState, today: Optional[date] = None) -> dict[str, Any]:
        """依据计划工期与实际签署日重算预测开园日与关键路径。

        班型变更/分期开园会改变前置状态，因而每次都重新计算。
        """
        today = today or date.today()
        overrides = {}
        if site.plans:
            overrides = site.plans[-1]["days"]
        schedule: dict[str, date] = {}
        for gate in self.order:
            conf = self.gates[gate]
            sign = site.site_signs.get(gate) if conf["scope"] == "site" else cs.signs.get(gate)
            valid_sign = sign and self._sign_valid(
                site, cs, gate, sign, date.max if conf["scope"] == "site" else today
            )
            if valid_sign:
                schedule[gate] = sign.on
            else:
                start = max((schedule[d] for d in conf["deps"] if d in schedule), default=today)
                duration = int(overrides.get(gate, 30 if gate in ("fire_inspection", "health_inspection",
                                                                  "institution_license") else 15))
                schedule[gate] = date.fromordinal(start.toordinal() + duration)
        # 链长（前置深度），用于同日期平局时仍回溯出完整关键链
        rank: dict[str, int] = {}
        for gate in self.order:
            deps = self.gates[gate]["deps"]
            rank[gate] = 1 + max((rank[d] for d in deps), default=0)
        chain: list[str] = []
        node = max(schedule, key=lambda g: (schedule[g].toordinal(), rank[g])) if schedule else None
        while node:
            chain.append(node)
            deps = self.gates[node]["deps"]
            node = max(deps, key=lambda d: (schedule[d].toordinal(), rank[d])) if deps else None
        chain.reverse()
        projected = max(schedule.values()).isoformat() if schedule else None
        statuses = {g: self.gate_status(site, cs, g, today) for g in self.order}
        return {
            "kind": cs.kind,
            "today": today.isoformat(),
            "projected_open": projected,
            "critical_path": chain,
            "schedule": {g: schedule[g].isoformat() for g in self.order},
            "delayed_gates": [g for g in self.order
                              if statuses[g]["state"] in ("pending", "blocked", "expired", "rejected")
                              and schedule[g] > today],
            "opened_on": cs.opened_on.isoformat() if cs.opened_on else None,
        }

    # ----- 补助台账 ------------------------------------------------------ #
    def subsidy_ledger(self, site_id: str) -> dict[str, Any]:
        """按可核验里程碑生成分录（逐业务事件增量重算，幂等可重放）。

        有效托位 = 班型已开园且当前闸门全核准时的核准容量，否则 0。
        - 开园：按开园当年剩余比例一次性拨付；
        - 检查不通过/材料过期/班型变更致有效托位下降：按下降数追回；
        - 复验合格恢复：按恢复数补拨。每笔分录引用触发事件 seq。
        """
        rate = self.policy["subsidy"]["per_slot_year"]
        all_events = self.store.events()
        entries: list[dict[str, Any]] = []
        opened_on: dict[str, date] = {}
        per_slot: dict[str, float] = {}
        last_eff: dict[str, int] = {}
        basis_name = {
            "class_opened": "class_opened", "class_changed": "capacity_reconfigured",
            "gate_signed": "reverified", "gate_rejected": "inspection_failed",
        }

        for i, ev in enumerate(all_events):
            p = ev["payload"]
            if p.get("site_id") != site_id:
                continue
            d = _d(ev["on"])
            if ev["type"] == "class_opened":
                opened_on.setdefault(p["kind"], d)
                per_slot.setdefault(p["kind"], round(rate * self._year_fraction(d), 4))

            # 仅在可能改变有效托位的事件之后结算，且只看业务时间已开园的班型
            if ev["type"] not in ("class_opened", "class_changed", "gate_signed",
                                  "gate_rejected"):
                continue
            snapshot = self._replay_subset(all_events[: i + 1])
            site = snapshot.get(site_id)
            if not site:
                continue
            for kind, od in opened_on.items():
                if d < od or kind not in site.classes:
                    continue
                r = self.class_readiness(site, site.classes[kind], d)
                eff = r["capacity"]["approved"] if r["ready"] else 0
                prev = last_eff.get(kind, 0)
                if eff == prev:
                    continue
                amount = round(per_slot[kind] * (eff - prev), 2)
                if amount == 0:
                    last_eff[kind] = eff
                    continue
                entries.append({
                    "date": d.isoformat(), "kind": kind,
                    "entry": "disburse" if amount > 0 else "clawback",
                    "slots": abs(eff - prev), "amount": amount,
                    "basis": basis_name.get(ev["type"], "effective_slots_changed"),
                    "evidence_seq": ev["seq"],
                    "note": self._ledger_note(ev["type"], amount, eff - prev),
                })
                last_eff[kind] = eff

        total_disbursed = round(sum(e["amount"] for e in entries if e["amount"] > 0), 2)
        total_clawback = round(-sum(e["amount"] for e in entries if e["amount"] < 0), 2)
        return {
            "site_id": site_id,
            "rate_per_slot_year": rate,
            "entries": entries,
            "total_disbursed": total_disbursed,
            "total_clawback": total_clawback,
            "net": round(total_disbursed - total_clawback, 2),
        }

    def _replay_subset(self, events: list[dict[str, Any]]) -> dict[str, SiteState]:
        sites: dict[str, SiteState] = {}
        for ev in events:
            self._apply(sites, ev, _d(ev["on"]))
        return sites

    @staticmethod
    def _ledger_note(ev_type: str, amount: float, delta: int) -> str:
        if ev_type == "class_opened":
            return "开园里程碑，按当年剩余比例拨付核准托位补助"
        if amount < 0:
            return "有效托位下降（材料过期/检查不通过/班型变更），追回对应补助"
        return "复验合格恢复有效托位，补拨对应补助"

    @staticmethod
    def _year_fraction(d: date) -> float:
        """当年剩余比例（含当天）：1/1 为 1.0，12/31 约为 1/365。金额再统一取整。"""
        import calendar
        days_in_year = 366 if calendar.isleap(d.year) else 365
        return (days_in_year - d.timetuple().tm_yday + 1) / days_in_year

    # ----- 专班解释 ------------------------------------------------------ #
    def explain(self, site: SiteState, cs: ClassState, today: Optional[date] = None) -> dict[str, Any]:
        today = today or date.today()
        r = self.class_readiness(site, cs, today)
        cap = r["capacity"]
        binding = min(cap["constraints"], key=lambda k: cap["constraints"][k])
        reasons = []
        names = {"filed_area": "备案面积", "natural_light": "采光/活动面积",
                 "fire_egress": "消防动线安全容量", "staffing": "教师配置（师生比）"}
        if cap["approved"] < cap["proposed"]:
            reasons.append(
                f"拟定 {cap['proposed']} 托位，{names[binding]}仅支持 "
                f"{cap['constraints'][binding]} 托位，故核准 {cap['approved']}。")
        for g in r["gates"]:
            if g["state"] != "approved":
                reasons.append(f"闸门「{self.gates[g['gate']]['name']}」状态 {g['state']}"
                               f"（责任方 {self.gates[g['gate']]['party']}）"
                               f"{'：' + g['reason'] if g['reason'] else ''}")
        pending_actions = [
            {"gate": g["gate"], "name": self.gates[g["gate"]]["name"],
             "party": self.gates[g["gate"]]["party"], "state": g["state"],
             "blockers": g["blockers"]}
            for g in r["gates"] if g["state"] != "approved"
        ]
        return {
            "site_id": site.site_id, "kind": cs.kind,
            "proposed": cap["proposed"], "approved": cap["approved"],
            "publicly_visible": cap["publicly_visible"],
            "binding_constraint": binding if cap["approved"] < cap["proposed"] else None,
            "why_partial": reasons,
            "pending_actions": pending_actions,
            "opened_on": r["opened_on"],
        }

    # ----- 角色视图 ------------------------------------------------------ #
    def public_view(self, site: SiteState, today: Optional[date] = None) -> dict[str, Any]:
        """公众：脱敏进度 + 实际开放日期；未核准容量不出现。"""
        r = self.site_readiness(site, today)
        return {
            "site_id": site.site_id,
            "name": site.name,
            "classes": [
                {
                    "kind": c["kind"],
                    "age_months": c["age_months"],
                    "visible_slots": c["capacity"]["publicly_visible"],
                    "status": ("paused" if c["is_open"] and c["frozen_gates"]
                               else "open" if c["is_open"]
                               else "ready" if c["ready"] else "preparing"),
                    "actual_open_date": c["opened_on"] if c["is_open"] else None,
                    "progress_gates": sum(1 for g in c["gates"] if g["state"] == "approved"),
                    "total_gates": len(c["gates"]),
                }
                for c in r["classes"]
            ],
            "visible_total": r["visible_total"],
        }

    def inspector_view(self, role: str, site: SiteState, today: Optional[date] = None) -> dict[str, Any]:
        """检查部门：只看到职责所需材料（site 级材料在顶层，class 级按班型列出）。"""
        today = today or date.today()
        gates_allowed = INSPECTOR_GATES[role]
        site_materials = []
        class_gates = []
        for g in gates_allowed:
            conf = self.gates[g]
            if conf["scope"] == "site":
                st = self.gate_status(site, next(iter(site.classes.values())), g, today) \
                    if site.classes else {"state": "pending", "reason": None,
                                          "signed_on": None, "expires_on": None}
                sign = site.site_signs.get(g)
                site_materials.append({"gate": g, "state": st["state"], "reason": st["reason"],
                                       "signed_on": st["signed_on"], "expires_on": st["expires_on"],
                                       "evidence": self._mask(sign.evidence) if sign else None})
            else:
                class_gates.append(g)
        out_classes = []
        for cs in site.classes.values():
            materials = []
            for g in class_gates:
                st = self.gate_status(site, cs, g, today)
                sign = cs.signs.get(g)
                materials.append({
                    "gate": g, "state": st["state"], "reason": st["reason"],
                    "report_no": sign.report_no if sign else None,
                    "signed_on": st["signed_on"], "expires_on": st["expires_on"],
                    "evidence": self._mask(sign.evidence) if sign else None,
                })
            out_classes.append({"kind": cs.kind, "materials": materials})
        return {"role": role, "site_id": site.site_id,
                "site_materials": site_materials, "classes": out_classes}

    @staticmethod
    def _mask(evidence: dict[str, Any]) -> dict[str, Any]:
        # 对外只保留核定结论字段，隐去证件号/联系方式类材料
        sensitive = {"id_no", "phone", "id_card", "contact", "cert_no"}
        return {k: ("***" if k in sensitive else v) for k, v in evidence.items()}

    def street_view(self, role_street: str, sites: Iterable[SiteState], today: Optional[date] = None) -> list[dict[str, Any]]:
        """街道：只能维护本辖区项目。"""
        out = []
        for site in sites:
            if site.street != role_street:
                continue
            r = self.site_readiness(site, today)
            ledger = self.subsidy_ledger(site.site_id)
            out.append({**r, "subsidy_net": ledger["net"],
                        "critical_paths": [self.critical_path(site, cs, today)
                                           for cs in site.classes.values()]})
        return out

    # ----- 历史还原 ------------------------------------------------------ #
    def as_of_commitments(self, site_id: str, as_of: str, recorded_by: Optional[str] = None) -> dict[str, Any]:
        """按任意历史日期还原当时对外承诺（双时态）。

        recorded_by 缺省等于 as_of：即“截至该日，当时已录入系统的信息”，
        事后补录的事件不会污染历史承诺。
        """
        recorded_by = recorded_by or as_of
        sites = self.replay(as_of=as_of, recorded_by=recorded_by)
        site = sites.get(site_id)
        if not site:
            return {"site_id": site_id, "as_of": as_of, "recorded_by": recorded_by,
                    "classes": [], "note": "当日场地尚未登记"}
        return {"as_of": as_of, "recorded_by": recorded_by,
                **self.public_view(site, _d(as_of))}
