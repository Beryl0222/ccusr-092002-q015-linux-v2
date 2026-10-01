# 普惠托位开办就绪服务

在原「普惠托位公平分配」骨架上，实现老城区党群中心/闲置公房改办普惠托育的**开办就绪（readiness-to-open）**服务。以 `contracts/childcare_capacity.json` 的场地、班型、拟定容量为起点，把开办过程串成一条有责任方的闸门链，杜绝「18 个托位」按承诺数开园、补助按承诺数拨付。

## 解决的问题

- 项目表写 18 托位，开园前才发现消防动线、采光、备案面积、教师配置只支持更少 → **容量按四项可核验约束取最小**；
- 补助按承诺数提前拨出 → **补助只随可核验里程碑（开园）拨付，冻结/核减自动生成追回分录**；
- 材料过期或检查不通过会否掉全园 → **冻结精确到班型（class scope），其他班型照常推进与开放**；
- 尚未核准的容量泄露给家长 → **未走完全部闸门且未到开园里程碑的容量，公众视图恒为 0**；
- 承包方重复报验刷进度 → **报验按报验单号幂等，且报验本身永不推进闸门，只有责任方签署才推进**；
- 延期、分期开园、改班型 → **关键路径按班型重算；结构变更使该班型 class 级签署与设计图纸失效重走**；
- 越权查看/维护 → **街道只见本辖区、检查部门只见职责材料、公众只见脱敏进度与实际开放日期**；
- 事后说不清 → **专班可解释「为何只开放部分托位 / 资金依据 / 谁未完成动作」，并可按任意历史日期（双时态）还原当时对外承诺**。

## 闸门链与责任方

| 闸门 | 责任方 | 作用域 | 依赖 | 说明 |
|---|---|---|---|---|
| property_consent 产权同意 | owner 产权方 | site | — | 书面同意改造普惠办托 |
| design_change 设计变更 | designer 设计单位 | site | property_consent | 动线/采光/分隔图纸；结构改班型后失效 |
| construction 施工节点 | contractor 承包方 | class | design_change | 节点须报验，重复报验不重复推进 |
| fire_inspection 消防检查 | fire 消防机构 | class | construction | 有效期 365 天，证据给 max_capacity |
| health_inspection 卫生评价 | health 卫健(评价) | class | construction | 有效期 365 天 |
| institution_license 机构许可备案 | health_commission 卫健(备案) | class | fire + health | 备案面积到班型 filed_area_m2 |
| staffing 人员到岗 | operator 运营方 | class | institution_license | 持证教师数按师生比折算容量 |
| fee_commitment 收费承诺 | price_authority 价格部门 | site | institution_license | 普惠收费承诺，全园适用 |

## 容量核定（四项约束取最小，再与拟定数取小）

- 备案面积：`floor(filed_area_m2 / 7)`（人均备案面积阈值）
- 采光/活动面积：有自然采光房间面积合计 `/ 3`
- 消防动线：消防意见载明的 `max_capacity`
- 教师配置：`持证教师数 × 7`（师生比 1:7，每班至少 2 名持证教师，不足则 0）

阈值见契约 `policy`，可按地方标准配置。

## 事件与双时态

所有变化都是 append-only 事件，业务时间 `on` 与录入时间 `recorded_at` 分离：

`site_registered · class_added · class_changed · plan_revised · inspection_submitted · gate_signed · gate_rejected · class_opened`

- 状态按业务时间重放；历史视图 `history?as_of=…[&recorded_by=…]` 同时受两个时间约束。
- `recorded_by` 默认等于 `as_of`，事后补录（更晚的 `recorded_at`）不会污染「当时」的对外承诺。

## 补助台账（可核验里程碑）

- `class_opened`：按开园当年剩余比例 × 核准托位 × 2400 元/托位·年 拨付；
- 检查不通过/材料过期/班型变更致有效托位下降：逐事件增量生成等额**追回**（负分录）；
- 复验合格恢复：生成等额**补拨**。台账对同一事件流重复计算结果一致（幂等），每笔带 `evidence_seq`。

## HTTP API

启动：`python3 service.py --port 8000`；自检：`python3 service.py --check`。

写入（角色经 `X-Role`，街道经 `X-Street`，中文头已做 UTF-8 还原）：

- `POST /events` — 追加事件。街道只能写本辖区；闸门只能由对应责任方签署；报验须由承包方发起且带 `report_no`；消防/卫生签署须匹配已提交报验单；开园容量只能等于当前核准值，否则 403。

查询：

- `GET /sites/<id>/readiness` — 专班：各班型闸门状态、四约束容量、冻结、关键路径
- `GET /sites/<id>/explain?kind=托小班` — 专班解释：binding 约束、部分开放原因、未完成动作及责任方
- `GET /sites/<id>/ledger` — 补助拨付/追回分录与净额
- `GET /sites/<id>/public` — 公众脱敏视图（无拟定数/面积等内部字段，含实际开放日期）
- `GET /sites/<id>/inspect?role=fire|health|health_commission|price_authority` — 检查部门职责材料
- `GET /sites`（`X-Street`）— 街道本辖区项目列表
- `GET /sites/<id>/history?as_of=YYYY-MM-DD[&recorded_by=YYYY-MM-DD]` — 历史承诺还原

## 测试

```bash
python3 -m unittest discover -s tests -v   # 27 个：领域规则 23 + HTTP 集成 4
```

`contracts/childcare_capacity.json` 为公开领域样例与策略配置，不含真实个人资料、业务凭据或生产连接信息。
