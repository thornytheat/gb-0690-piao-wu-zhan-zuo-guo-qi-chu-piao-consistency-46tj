# 租约与票据定义

id为事件号、order为订单、ticket为票号，字符串保留前导零及超大数字。seat接受A01或A-01并统一A-01，目录A-01至A-16。channel为官方、票社、团购。
time带偏移，统一UTC，回放按事件时间排序，同刻先release再hold再pay再issue。hold需expires且后于time。
占座有效区间[start,end)，到期点付款无效。release仅能释放当前同订单租约，缩短end；其后旧支付不得恢复座席。占用中其他订单hold隔离。
同事件最高version采信，同版相互矛盾全部隔离。重复、旧版、无效状态都有来源行号与原因码。支付每租约只允许一笔，出票需在支付之后且金额相等，票号及座席全场唯一。
渠道对账为有效支付减有效出票，金额为Decimal字符串，悬单单独统计，不把它认作丢失事件。事件原始行序判断补传迟到，业务仍按事件时间完整回放。
输入行数=采信事件+隔离，租约/支付/票据各为派生集合不替代原始计数。空文件等失败码独立，运行ID含输入哈希、规则版本与参数，复核整数版本递增。

支付渠道须与其占座租约一致，出票渠道须与有效支付一致；跨渠道冲突逐行隔离，不在渠道间静默移动金额。

## 团购补位（M8）

订单按 `order` 聚合，应出票座位来自未释放租约；主动释放的租约不计入。每个订单给出 `expected/issued/backfilled/missing` 与状态：全部座位有凭证为“已完成”，有任一凭证但仍缺票为“部分出票”，一张凭证都没有为“待补位”。缺失座位中无有效支付或带 `ORPHAN/EXPIRED` 证据的为受阻座位 `blocked_seats`，其余可补位座位为 `missing_seats`。

补位动作 `POST /api/backfill/<run_id>`，请求体含整数 `version` 与可选 `seats`（留空自动分配）。补位凭证写入批次 `backfills`，字段含 `order/channel/old_seat/new_seat/ticket/amount/payment/lease/evidence_line/evidence_code/source/time/state`；票号以 `BF` 开头且与原票全场唯一，渠道与金额沿用该座位的有效支付。自动分配只取未出票、无有效租约且不属于受阻订单的空闲座席；显式座席占用或越界分别返回 `OVERLAP/UNMAPPED`，容量不足返回 `LIMIT`。

补位与复核共用整数版本乐观锁：版本不符返回409且旧版状态原样保留；无新增凭证的重跑不推进版本、不重复出票。`GET /api/backfill.csv` 分三段导出原票回执、补位凭证与订单完成状态，按同一订单号互相核对。
