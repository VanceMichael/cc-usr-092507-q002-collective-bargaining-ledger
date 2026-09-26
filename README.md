# 集体协商方案履约账本

保存集体协商提案、签署依据和协议履约事实，并交付一套完整的
**集体协商与履约服务**：让职工方与企业方在有效授权内完成协商、
逐字确认、联动校验、表决与原子会签，生效后按周期追加履约事实。

## 领域范围

系统涉及职工代表、企业代表、园区工会人员、协议复核人员。当前领域资料记录以下业务事实：

- 协商议题覆盖薪酬待遇和福利保障
- 方案需要兼顾企业经营与职工合理诉求
- 协商方法要沉淀到基层劳资沟通中

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供不含真实身份信息的示例，`src/collective_bargaining_ledger/context.py` 负责读取并检查这些资料。

## 服务设计

一条主线贯穿全部命令：

```
授权任命 → 各自提交（诉求 / 经营假设 / 方案与测算口径 / 反建议）
→ 双方现任代表逐字确认同一版本 → 工资·工时·福利整体联动校验
→ 双方表决 → 原子会签 → 按周期追加履约事实 / 争议 / 补充约定
→（新证据）发起重新审议，原协议条款保持原样
```

关键不变量由代码与数据库约束共同保证：

- **有效授权**：所有写操作校验令牌与角色；诉求只能职工方提、经营假设只能企业方提。
- **逐字一致**：版本以规范化 JSON 的 SHA-256 为唯一标识，双方现任代表确认同一哈希才冻结候选；措辞、空白不同即不同版本。
- **人员更换不继承**：任命新代表即撤销前任令牌；历史确认/表决留痕，但只有现任任次的确认与表决计入，前任未获接受的意见不自动继承。
- **联动校验**：表决前对整包方案（工资/工时/福利）在共同测算口径下整体测算——时薪不得因工时变动而下降、月工时不得超法定上限、整包新增人工成本不得超企业承担上限、工资不得低于最低工资。任何一项不通过都不能交付表决。
- **签署后不可改**：已签署回合拒绝任何修改；新证据只能发起重新审议，复核受理后开启全新回合重走完整流程，原协议条款与文本原样保留。
- **幂等**：所有写命令要求业务号 `request_id`。同号同文返回原结果；同号异文抛出 `idempotency_conflict`，明确暴露冲突且不覆盖原结果。
- **原子会签**：第二个签名在同一事务内创建协议，`round_id UNIQUE` 兜底并发——并发签署只会产生一份有效协议，单方签署只是待会签状态，不产生半份事务。
- **履约账本**：按周期追加实际履行/部分履行/争议与补充约定（追加，不改写历史）；个人陈述按角色脱敏——本方看原文，对方与中立角色只见角色级代号。
- **角色视图**：工会与企业各自调用接口，看到自己的待办、尚未解决的分歧及共同有效的承诺。
- **可恢复流水线**：回合开启等准备动作由检查点流水线执行，进程被终止后重启从原检查进度继续；动作层幂等保证不产生第二个回合或第二任代表。

## 模块

| 模块 | 职责 |
| --- | --- |
| `canonical.py` | 规范化 JSON 与 SHA-256，逐字一致的判定基准 |
| `linkage.py` | 工资/工时/福利联动整体测算（Decimal，确定性） |
| `render.py` | 版本与协议的确定性文本渲染（附逐字基准段） |
| `masking.py` | 个人陈述按角色脱敏 |
| `storage.py` | SQLite 持久化：WAL、`BEGIN IMMEDIATE`、唯一约束 |
| `service.py` | 全部领域命令与角色视图 |
| `pipeline.py` | 检查点流水线，终止后从原进度恢复 |
| `api.py` | 标准库 HTTP JSON 接口（`Authorization: Bearer`） |

## 运行

```bash
# 启动 HTTP 服务（首次调用 POST /setup 领取主持人与复核人令牌）
PYTHONPATH=src python3 -m collective_bargaining_ledger.api --db ledger.db --port 8080
```

主要接口（写操作均需业务号 `request_id` 或 `Idempotency-Key` 头）：

- `POST /rounds`、`POST /rounds/{id}/representatives`
- `POST /rounds/{id}/demands|assumptions|proposals|confirmations|votes|signatures`
- `GET /rounds/{id}`、`GET /rounds/{id}/evaluate`、`GET /dashboard`
- `POST /agreements/{id}/performance|disputes|supplements|reviews`
- `POST /agreements/{id}/supplements/{seq}/accept`、`POST /agreements/{id}/disputes/{seq}/resolution`
- `POST /reviews/{id}/decision`、`POST /bootstrap-pipeline`

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

测试覆盖：授权与越权、逐字确认与异文版本、人员更换不继承、联动校验各规则、
幂等重试与异文冲突、并发会签唯一协议、履约追加与脱敏、重新审议不改条款、
进程硬终止（`os._exit`）后的检查点恢复、HTTP 双方角色视图。

上述命令只读取仓库内资料，不需要连接外部业务服务。
