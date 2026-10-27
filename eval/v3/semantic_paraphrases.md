# 同一需求，换三种说法

这组数据检查：买家换了用词，检索是否还能找到同一批商品。

从现有业务集的全部 24 个正例品类出发，每个需求写三种问法，共 72 条。所有问题都搜索完整 3,700 条目录，配送到中国，沿用原需求的同款标准答案。同一需求的三种说法不改变规格要求，不依据检索得分挑选或删除样本。

| 问法 | 移动电源的例子 | 主要检查 |
|---|---|---|
| 直接名称 | Roamix 移动电源 | 商品正文中的名称是否容易命中 |
| 同义说法 | Roamix 充电宝 | 换成日常别称后能否命中 |
| 使用场景 | Roamix 在外面一整天，掌上设备电量见底，附近又找不到墙上的电源 | 不说商品名时能否理解需求 |

其他配对包括“行李箱／拉杆箱”“保温壶／暖水瓶”“静音鼠标／按下去没响声的无绳滑鼠”。没有刻意让所有汉字都不同：自然说法仍可能共享一些词，关键词检索也可能成功。

## 文件与标注

用例在 [semantic_paraphrases.jsonl](semantic_paraphrases.jsonl)：

- `pair_id` 将同一需求的三种问法关联起来。
- `query_kind` 分别为 `direct`、`synonym`、`scenario`，结果分组展示。
- `reference_case_id` 指向 [v2 原业务集](../v2/product_retrieval.jsonl)，三条问题共用它的 `relevant_canonical_ids`。
- `equivalence_note` 解释替换的词与必须保留的条件。例如耳机三条都要求减少听到的环境噪声，只有通话麦克风降噪的型号仍不算正确。

本次查询由助手依据目录编写并核对，尚未经独立人工复核；它们是补充诊断集，不是独立留出验收。原业务集中的规格问题和 6 个空结果负例继续运行，未被这批正例替换。品牌 Roamix 保留在查询中以限定答案范围，检索器并没有额外获得品牌过滤提示。

运行前冻结用例、模型与参数；三路使用相同过滤、K=5、每路候选 32，混合保持等权 RRF，不加精排。不得因某条问题没有体现向量优势而删题或修改标准答案。

## 复现与读数

在工程根目录执行，使用新的输出目录；已有商品向量会复用：

```bash
uv run --extra retrieval python -m scripts.eval.full_catalog_retrieval --output eval/verification/semantic-new-run
```

新增三组结果键为 `semantic_direct`、`semantic_synonym`、`semantic_scenario`。先在同组内比较 BM25、embedding、混合，再比较同一算法换说法前后的变化。直接名称和改写问题并非互相独立的购物需求，不能当成 72 个全新意图。

每个分组分别读两张汇总：`candidate_summary` 是每路最多 32 条候选、两路取完整并集后的覆盖率；`final_top5_summary` 是排序与同款去重后最终前 5 条的 Recall、MRR、NDCG。兼容字段 `summary` 仍指最终前 5 条，不是并集覆盖率。负例单独检查误返回，不参加正例 Recall 均值。

评测逐条检查混合候选是否等于两条单路候选的并集，并保存 `retrieval_stages`。`ranking_lost_ids` 列出候选里已经找到、但最终前 5 条没有保留的正确型号。合并不应减少覆盖，最终前 5 条变差时应查排序与截断。

保留原有 `full_business`、`full_multilingual`、`language_slice` 三组。所有分组不能混成一个“总体召回率”：增加困难问法会改变样本比例，不能据此宣称旧算法变差。
