# 集体协商方案履约账本

保存集体协商提案、签署依据和协议履约事实。

## 领域范围

系统涉及职工代表、企业代表、园区工会人员、协议复核人员。当前领域资料记录以下业务事实：

- 协商议题覆盖薪酬待遇和福利保障
- 方案需要兼顾企业经营与职工合理诉求
- 协商方法要沉淀到基层劳资沟通中

后续实现需要围绕这些边界组织服务：

- 提案版本协商
- 授权代表变更
- 条款联动测算
- 协议原子签署
- 周期履约复盘

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供不含真实身份信息的示例，`src/collective_bargaining_ledger/context.py` 负责读取并检查这些资料。

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

上述命令只读取仓库内资料，不需要连接外部业务服务。
