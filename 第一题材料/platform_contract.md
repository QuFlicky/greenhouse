# 平台接入公开检查契约

此契约用于线上交付五。可以使用任意语言、HTTP 或本地命令；以下是公开检查器交换 JSON 的格式，不限定内部架构。本题只要求受理 case.json 中的固定批次，超出批次的任务可返回明确错误；任意增量订单接入不在本题必做范围内。job_id 全局唯一，时间输入均为 0 至 2^63−1 内的整数虚拟秒数；周期与统计窗口大于 0，计划结束也不得超出范围，超出时返回结构化错误。导出和基础指标均为必需，可由 report 合并提供。

## 操作与返回

```json
{"op":"submit","job":{"job_id":"J0001","station_id":"S1","ready_s":0}}
{"op":"get","job_id":"J0001","at_s":40}
{"op":"report","at_s":3600}
```

- submit：首次提交返回 `{"status":"accepted","run":{...}}`；同标识、同参数返回 `duplicate` 和相同 run，不新增计划；同标识、不同参数返回 `conflict`。run 含 job_id、station_id、ready_s、start_s、end_s。所有请求共用持久记录，允许不同进程提交。
- get：已提交任务返回 status=ok、state、run、wait、events。未提交或不存在的任务返回 status=not_found。at_s 默认为 0，须在上述范围内；state 依次按该时刻与 ready_s、start_s、end_s 比较，返回 not_ready、waiting、running、simulated_completed。wait 至少含 wait_s=start_s-ready_s，可补充工位和资源可用时刻。
- report：返回 status=ok、record_kind=virtual_plan_and_replay、runs、events、metrics。runs 包含所有已提交任务的唯一计划；metrics 至少含 planned_count、simulated_completed_count、at_s、event_counts。模拟完成数按 end_s<=at_s 计数；event_counts 是各类事件实际条数。
- 请求形状错误、非法操作或字段无效时，返回 status=error，并提供非空 code、message。冲突、未找到和非法输入应可区分。

持久事件至少包括 accepted、planned、duplicate、conflict。每项新计划对应一条 accepted 和一条 planned；每次有效重试或冲突对应一条 duplicate 或 conflict。事件包含唯一递增 seq、job_id、event 和 detail。get 返回该任务事件；report 返回全部事件。等待原因可以保存在 planned.detail 中。

waiting 与 simulated_completed 可以由持久计划和 at_s 推导，无需写入时间变化事件。查询不能改变计划或事件；指标和事件必须一致。允许附加 request_id 等响应元数据，runs 顺序不作要求；检查只比较约定业务字段。每条事件必须属于正确任务，查询必须返回该任务记录与事件；模拟完成数应随请求时刻正确变化。模拟完成不等于设备已经执行，不能把本题检查当作生产安全或鉴权验证。

## 运行公开检查器

提供一个接收 `--case`、`--db`、`--request`、`--response` 的命令适配器：读取 request 文件，将一个 JSON 响应写入 response 文件后退出。db 标识本次共享持久存储；同一路径必须保留状态。HTTP 实现可提供薄适配器完成等价调用；如需预先启动服务，请注明步骤。业务错误也应写入结构化响应，适配器正常退出。

在你的实现目录建立 adapter.json，command 数组填写实际可执行入口。例如下面的 platform_entry.py 由你提供，不在题包中：

```json
{"command":["python3","platform_entry.py"]}
```

命令在 adapter.json 所在目录运行；检查器会追加上述四个参数。其他语言可填写编译后程序路径或启动命令。

```text
python3 check_platform.py --adapter adapter.json --case case.json --out platform_evidence.json
```

检查器使用临时持久存储，启动两个独立进程提交相同或不同任务，检查重复、冲突、同工位不重叠、错误、任务和事件归属、开始/结束附近的状态与指标，以及进程间数据保留。它只验证这些公开样例，不证明所有并发交错或本人作者身份。platform_requests.json 另提供五条手工请求样例，不是批量请求接口。
