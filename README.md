# FieldProof 家电试点成效分析

运营团队可用同一套人群构成比较标准与辅助程序，核对样本代表性、能耗和可靠性，并按封存的证据发布市场决策。

## 本地运行

使用 Python 3.11 或更新版本。首次搭建本地虚拟环境时依照 `requirements.txt` 安装锁定依赖，已经预装依赖的环境可直接启动。服务使用 SQLite，面向同一受信任的操作环境，评审人标识由操作人员填写。

```text
python -m pip install -r requirements.txt
python -m uvicorn fieldproof.api:from_environment --factory --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/docs` 查看交互式接口说明。环境变量 `FIELDPROOF_DB` 可指定数据库路径，默认使用 `fieldproof.sqlite3`。通过 `python -m unittest discover -v` 运行测试。命令 `python -m tests.live_service` 在环回地址启动服务，用临时数据库和合成数据完成试点、封存和审批。

## 登记试点

通过 `POST /trials` 定义程序、市场、起始日期及不含当天的结束日期。每个市场可以按气候和负载登记对照单元，权重之和为 10,000 基点。站点属于一个市场，使用 IANA 时区。设备在一段前闭后开的本地日期内登记到一个站点，分入标准组或辅助组。

试点计划可以预先登记 `primary_windows` 时段划分与 `minimum_common_coverage_bp` 最低共同覆盖。时段必须落在试点日期之内、互不重叠；未登记时整个试点期视为一个主比较时段。主比较只使用这些登记时段，观察窗口本身仍保留全部日期，供安全统计使用。

草稿可以更新试点计划，已有设备的登记必须仍然符合计划。站点和设备登记可用相同内容重试，名册查询显示各单元和试验组的分配。开启试点需要提交当前版本，每个对照单元的两组都达到最低登记数后，计划和名册随试点一起锁定。

## 接收与核查数据

设备可通过 `POST /trials/{trial_id}/deliveries` 上报事件，现场也可导入 CSV。重发使用相同批次编号，事件编号保持稳定，版本从一开始顺序递增。更正可以替换事实的字段，设备、周期和事件类型保持固定。撤回从所在的事实截点起去掉事件。允许同批事件乱序，包括同一事件的多个连续版本。冲突拒绝整批内容，只有重复事件的批次也不会推进事实截点。

CSV 要求列 `event_id,revision,unit_id,cycle_id,kind,observed_at`，可选列为 `fold,void,meter_id,meter_wh,reason`。带偏移的时间戳直接确定一个时刻。本地时间依据设备站点的时区解释，时钟回拨出现重复时间时用 `fold` 的 0 或 1 标明归属。夏令时跳过的本地时间不可导入。不同内容使用新批次编号。

```text
python -m fieldproof.cli --db fieldproof.sqlite3 ingest-sheet valley north-20261025 north.csv
python -m fieldproof.cli --db fieldproof.sqlite3 ingest-notices valley grid-notices notices.json
python -m fieldproof.cli --db fieldproof.sqlite3 ingest-acks valley grid-acks acks.json
```

`ingest-notices` 与 `ingest-acks` 读取 JSON，内容分别对应 `POST /curtailment/notices` 的 `notices` 与 `POST /curtailment/acks` 的 `acknowledgments`；`delivery` 参数既是重试键，也会写入请求。

一个周期包含一次 `start` 和一次 `complete` 或 `abort`。跨午夜周期归入开启时的本地日期。时长为零、时长超限、两段闭合周期重叠，或有手动动作及告警落在周期以外，都会使该周期失去分析资格。核查队列会列出周期缺口及电量异常。中止周期进入可靠性统计。完整周期只有在同一计量表的累计读数连续单调、起止时刻均有读数时才计算耗电量，缺失或不可信的电量保留为空。

## 站点级电网限载

站点可提交带生效区间、执行范围和撤销记录的限载通知，以及运行侧的应答，两者都是可回看的事实。

通知通过 `POST /trials/{trial_id}/curtailment/notices` 提交，运行应答通过 `POST /trials/{trial_id}/curtailment/acks` 提交。二者与设备事件共用事实截点：重复批次不推进截点，只有新事实才前移，因此旧截点始终可复现当时的判断。通知和应答各自按 `revision` 顺序修订，冲突内容拒绝整批，撤回用 `void` 表示。

通知字段包括：

- `starts_at` / `ends_at`：命令生效区间，右开。
- `scope`：`site`（全站点）、`units`（指定设备）或 `selection`（按单元/试验组选择）。
- `source`：`grid-operator`、`site-controller`、`field-dispatch` 或 `unconfirmed`。只有前三种视为可确认来源。
- `revocation`：`revoked_at`、`by`、`reason`。撤销把命令的生效终点提前到撤销时刻。
- `revision`、`void`：与其他事实相同的修订与撤回语义。

运行应答 `acknowledgments` 用 `status` 区分 `affected`、`unaffected`、`unknown`；`affected` 必须给出设备实际受影响的 `affected_from` / `affected_until`。**命令区间与实际执行区间分开保存**：命令来自通知，执行只来自应答，两者都不由事件时间或“同一分钟”推断。

`GET /trials/{trial_id}/curtailment/exposure` 按设备列出命令区间、实际执行区间、通知编号和无法确认的原因。`GET /trials/{trial_id}/curtailment/notices` 显示通知的活动、撤销、撤回状态以及未确认来源数量，`/curtailment/overlaps` 列出命令区间重叠的通知。`GET /trials/{trial_id}/curtailment/notices/{notice_id}`、`/acknowledgments/{ack_id}` 返回修订历史。

无法确认的情况会显式列出，而不是留空：来源为 `unconfirmed`、应答为 `unknown`、或通知没有对应应答，都会在该设备上记录 `notice_source_unconfirmed`、`acknowledgment_unknown`、`acknowledgment_missing`。站点级通知的未确认区间会传播到同站点全部设备（`site_exposure_unconfirmed`），因为命令面向站点而非单台设备；按设备或选择范围下达的通知只影响声明的范围。

## 周期归因

周期的 `attribution` 保留命令与异常各自的实际时序：

- `external_stop`：执行中的命令覆盖了该运行，且设备没有上报终态。
- `equipment_anomaly_first`：设备告警的时刻不晚于命令执行窗口开始；即使运行被命令截断，仍按设备先行异常处理。
- `equipment_failure`：设备自行上报 `abort`，没有更早的告警。
- `completed_with_alarm`、`curtailed_exposure_completed`、`exposure_unknown`、`normal`。

外部停机不计入设备故障率，也不进入设备终止运行的故障分母；设备先行异常与已上报的设备故障照常进入。所有真实告警与故障都可以通过 `GET /trials/{trial_id}/markets/{market}/safety` 独立查询，不受主比较资格影响，并标注 `resolved`、`comparable`、`primary`。

## 核对成效

预览可以指定事实截点和本地日期范围。单元内按合格设备等权计算，再以预登记的固定权重汇总两个试验组。要求覆盖的单元缺数据时，相应市场指标不可用。比例用基点表示，电量用瓦时表示。能耗覆盖率考察合格设备的所有完整周期中已计量的比例，并检查提供读数的设备是否达标。护栏比较使用精确比例，显示结果再舍入。

主比较还要求“共同暴露支持”：同一登记时段、同一站点、同一对照单元的两组都要有可比运行，且两组的登记覆盖都不低于 `minimum_common_coverage_bp`。只有 `primary` 运行进入主比较的 `manual`、电量与时长指标。受限（通知覆盖、执行未确认、外部停机）或缺对照的单侧时段不并入另一组的机会，而是列在 `participation.restricted` 与 `participation.uncontrolled` 中。`comparable_cycles` 与 `primary_cycles` 分开给出，便于核对被换出了多少运行。

设备观察记录可追溯指标到事件修订。截点对账显示相同日期范围内哪些周期和市场指标变化。覆盖日历同时列出每日处于登记期的设备以及累计达到周期要求的设备。缺失设备诊断估计未知率可能影响的范围，站点影响分析逐个省去站点并报告证据支持变化，两者用于解释样本的局限。

`GET /trials/{trial_id}/markets/{market}/external-stop-influence` 逐个还原被外部停机截断的运行，重算整个市场的资格与覆盖，并给出另一试验组在该单元的机会变化（`opportunity.other_arm_changed`）。还原不会为缺失终态的运行动造完成事件，因此场景中该运行仍是未决缺口，结论应据此解读。

## 封存与审批

关闭时提供操作者看到的版本和事实截点，新数据抢先到达会让关闭请求失效。试点关闭后封存全期证据，证据记录包含截点、统计政策、市场结论和内容摘要。可通过证据接口回读或下载指标 CSV。

当成效和风险护栏通过，且现场与方法岗位由不同人员确认同一份证据，市场才允许推进。未解决的事实问题或覆盖缺口可以阻止放行，任一岗位可以记录反对意见。公告记录当时的判断和已有评审，完全相同的发布重试返回原公告。发布请求使用预期版次以保护操作者已经读到的市场状态。

市场结论同时守住紧急安全事件与可比样本两个条件：`emergency_events` 发现已确认的告警即 `fail`，比对结果再好也不能推进；未解决告警使该门控不可用。`comparable_sample` 要求主比较确有其支持样本。外部停机本身不是安全事件，但不会把另一组的正常机会算成两组共有。

证据包含 `curtailment_digest`，把该截点的通知编号与未确认原因一并封存。已签发的公告保留当时的证据、统计政策和结论，后续通知或应答只影响新截点，不回写历史公告；需要比较时用截点对账或按旧截点重新预览。
