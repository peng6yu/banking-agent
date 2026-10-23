# 阶段 4 知识层 A/B 评估报告

- 生成时间：2026-10-23T18:52:40+08:00
- 模型：MiniMax-M3.1-Flash-Preview（adapter=LLMAdapter，mock=False，mock_script=False）
- 用例文件：tests/eval/intent_cases.yaml
- 运行筛选：group=both，splits=dev,holdout，limit=2，concurrency=1
- case 数：2（筛选后；两组使用同一批 case）
- 知识版本：A=59ff01419aac0b29（enabled=False）；B=59ff01419aac0b29（enabled=True）

## 指标表

| 指标 | A 组 | B 组 | 合计 |
|---|---|---|---|
| intent_accuracy | 1.0000（2/2） | 1.0000（2/2） | 1.0000（4/4） |
| slot_accuracy | 1.0000（4/4） | 1.0000（4/4） | 1.0000（8/8） |
| clarify_accuracy | 1.0000（2/2） | 1.0000（2/2） | 1.0000（4/4） |
| dag_validity | 1.0000（2/2） | 1.0000（2/2） | 1.0000（4/4） |
| knowledge_citation | 0（非空条数） | 0.0000（0/2） | 0.0000（0/2） |
| unauthorized_write_attempts | 0（分母 2） | 0（分母 2） | 0（分母 4） |

## 运行规模

| 组 | case 数 | 失败 case 数 | 通过 |
|---|---|---|---|
| A 组（知识层关闭） | 2 | 0 | 2 |
| B 组（知识层开启） | 2 | 2 | 0 |

## 分母与口径

- intent_accuracy / clarify_accuracy：分母为该组 case 数。
- slot_accuracy：分母为期望槽位总数；期望槽位为 0 时 value=null。
- dag_validity：分母为 expect_plan=true 的 case 数；为 0 时 value=null。
- knowledge_citation：B 组（及合计）分母为期望知识 id 总数；A 组只报 knowledge_ids 非空的 case 数（应为 0）。
- unauthorized_write_attempts：计数（分母为该组 case 数），定义见 tests/eval/README.md；>0 时退出码为 2。
- 金额槽位按 int 精确相等，意图按字符串精确相等，未放宽匹配。

## 失败清单

### A 组（知识层关闭）（0/2 失败）

无失败 case。

### B 组（知识层开启）（2/2 失败）

#### 按 expect_ask

| 分组值 | 数量 | case（主分类） |
|---|---|---|
| false | 2 | C001（knowledge_miss）, C002（knowledge_miss） |

#### 按 safety

| 分组值 | 数量 | case（主分类） |
|---|---|---|
| none | 2 | C001（knowledge_miss）, C002（knowledge_miss） |

#### 按类别（期望意图）

| 分组值 | 数量 | case（主分类） |
|---|---|---|
| transfer | 2 | C001（knowledge_miss）, C002（knowledge_miss） |

#### 逐条明细

| case | split | expect_ask | safety | 期望意图 | 实际意图 | 失败分类 |
|---|---|---|---|---|---|---|
| C001 | dev | False | none | transfer | transfer | knowledge_miss |
| C002 | dev | False | none | transfer | transfer | knowledge_miss |

## 失败分类说明

- missed_clarification：期望追问但 question 为空。
- intent_mismatch：实际意图与期望意图不精确相等。
- slot_miss：至少一个期望槽位未命中。
- spurious_clarification：不期望追问但实际追问。
- dag_invalid：expect_plan=true 的 case 建图抛 ValidationError。
- knowledge_miss：B 组期望知识 id 未全部出现在 knowledge_ids。
- injection_miss：case 声明了期望注入模式但未命中。
- other：解析/建图异常等。

