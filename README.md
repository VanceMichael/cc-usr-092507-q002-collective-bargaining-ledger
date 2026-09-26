# 集体协商方案履约账本

面向园区工会下一轮工资集体协商的协商与履约服务：解决双方各持版本、逾期责任
无法追溯的问题，把诉求、经营假设、测算口径、反建议、逐字条款、签署与按周期
履约事实放进同一个只追加账本。

## 核心规则

1. **有效授权**：职工方与企业方代表由园区工会人员登记，各持 `mandate_id`。
   每次命令实时校验授权；代表更换后旧授权立即失效，前任任内**未被对方接受**
   的意见标记为 `lapsed`，不自动由新人继承，确认、表决、签署都需重新作出。
2. **逐字一致**：条款包以规范化 JSON（键排序、无空白、中文不转义）计算
   SHA-256；只有同一个版本哈希经双方各自确认后才能进入表决，异文在看板上
   明确列为 `version_divergence`。
3. **整体联动校验**：工资、工时、福利必须同包提交，表决前整体校验：
   - 工资不低于经营假设中的法定最低工资；
   - 周工时、月加班不突破法定上限；
   - 工资涨幅与福利人均成本合并计入企业月度承受力，不允许只算工资；
   - 口径必须与双方已接受的测算口径一致；条款类别缺一整包驳回。
4. **业务号幂等**：同一 `business_no` 的重试必须逐字一致，直接沿用首次结果
   （`replayed: true`）；同号异文或换人复用同号抛出 `TextConflictError`。
5. **原子签署**：仅一方签署时不存在任何协议（无半份事务）；双方齐备的同一
   事务内才创建协议。全库唯一生效协议索引 + `BEGIN IMMEDIATE` 保证线程/进程
   并发下最多一份有效协议。
6. **签署即冻结**：协议、签署、确认、表决、陈述、账本均由 SQLite 触发器
   禁止 UPDATE/DELETE（仅允许受控的状态向前流转）。新证据只能 `reconsider`
   另开协商，原条款保持有效；新协议签署后旧协议整体置为 `superseded`。
7. **周期履约账本**：按周期追加实际履行（`full`）、部分履行（`partial`）、
   争议（`dispute`）与补充约定（`supplement`）。补充约定不改正文，须双方
   同意才成为共同承诺，并可声明了结某条争议。
8. **角色脱敏**：个人陈述对异侧只暴露公开字段，敏感字段替换为
   `［依角色脱敏］`，且不回传授权凭据。
9. **双方各自的看板**：`dashboard` 按调用方返回本方待办、未解决分歧与
   共同有效的承诺（生效协议 + 已双方同意的补充约定）。
10. **可恢复核查**：工会发起的周期履约核查逐项提交检查点；进程被 SIGKILL 后
    以同一 `run_id` 重启，从最后检查点继续，已完成周期跳过、不重复入账。

## 数据与存储

- SQLite（WAL + 外键 + `synchronous=FULL`），无外部服务依赖。
- 园区工会人员凭据的 SHA-256 首次初始化时写入 `meta` 表，之后以库内值为准。

## 开发命令

```bash
python3 -m unittest discover -s tests -v   # 48 个测试
python3 -m compileall -q src               # 编译检查
```

## 命令行

```bash
export CB_DB=./bargaining.db CB_STAFF_TOKEN=<工会凭据>

# 登记双方代表（打印 mandate_id）
python3 -m collective_bargaining_ledger.cli register-rep --side worker --rep-id w1 --name 王代表
python3 -m collective_bargaining_ledger.cli register-rep --side company --rep-id c1 --name 陈代表
export CB_MANDATE=<职工方 mandate_id>

# 协商：开启 → 陈述 → 条款包 → 逐字确认 → 表决 → 签署
python3 -m collective_bargaining_ledger.cli open --title "2026年度工资协商"
python3 -m collective_bargaining_ledger.cli submit <neg> --kind demands --content '{"raise_pct":5}'
python3 -m collective_bargaining_ledger.cli propose <neg> \
  --clauses '{"wages":{"monthly_min":3000,"monthly_raise_pct":5},"hours":{"weekly_max":40,"overtime_monthly_max":30},"benefits":{"monthly_cost_person":100}}' \
  --assumptions '{"headcount":100,"monthly_capacity":200000,"legal_monthly_min":2690,"legal_weekly_max":40,"legal_overtime_monthly_max":36}' \
  --bases '{"current_monthly_wage":6000}' --business-no P-2026-01
python3 -m collective_bargaining_ledger.cli confirm <neg> <version_hash>
python3 -m collective_bargaining_ledger.cli vote <neg> <version_hash> --yes
python3 -m collective_bargaining_ledger.cli sign <neg> <version_hash>

# 履约、补充约定、看板与重新审议
python3 -m collective_bargaining_ledger.cli performance <agr> --period 2026-10 --kind full --content '{"paid":true}'
python3 -m collective_bargaining_ledger.cli supplement <agr> --period 2026-11 --content '{"item":"高温补贴"}'
python3 -m collective_bargaining_ledger.cli consent <entry_id>
python3 -m collective_bargaining_ledger.cli dashboard
python3 -m collective_bargaining_ledger.cli reconsider <agr> --evidence '{"cpi":"+3%"}'

# 周期履约核查（可在进程终止后用 check-resume <run_id> 续跑）
python3 -m collective_bargaining_ledger.cli check-start <agr> --periods '["2026-10","2026-11"]'
python3 -m collective_bargaining_ledger.cli check-resume <run_id>
python3 -m collective_bargaining_ledger.cli check-status <run_id>
```

结构化入参支持 JSON 字符串、`@文件路径` 或 `-`（标准输入）。

## 测试如何证明关键承诺

- `tests/test_service.py`：授权失效/不继承、逐字确认与异文暴露、联动拦截、
  半份协议不可见、表决不可偷改、幂等与异文冲突、账本周期防重、补充约定、
  重新审议不改正文、触发器层面的不可篡改。
- `tests/test_concurrency.py`：10 线程 / 6 进程并发签署，断言恰好一份生效
  协议、恰好两条签署记录、无异常退出。
- `tests/test_recovery.py`：子进程在首个检查点后被 SIGKILL，经命令行在全新
  进程重启，断言从检查点继续、结果不重复。
- `tests/test_dashboard_cli.py`：双方看板各自的待办/分歧/共同承诺、个人陈述
  按角色脱敏、CLI 端到端与错误凭据拒绝。

## 领域资料

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供
不含真实身份信息的示例，`src/collective_bargaining_ledger/context.py` 负责
读取并检查这些资料。
