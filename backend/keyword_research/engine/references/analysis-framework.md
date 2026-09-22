# 分析引擎（LLM 执行，输出写入 cache/analysis.json）

通用要求：每条结论至少引用——搜索量或趋势、相关度、购买率、点击/转化集中度至少一项、当前 PPC 或竞争指标至少一项；并标记置信度（高/中/低，定义见 metric-definitions.md）。只输出假设的地方必须标注"假设"。

## 0. 输入模式与口径（V1.1 新增）

先按 input.json 判定调研口径，所有结论的措辞效力按口径收敛：

| 模式 | 输入 | 结论效力 |
|---|---|---|
| M1 | 单 ASIN + Node | 完整结论；必须包含"市场面 vs 本品"对照视角 |
| M2 | 人工细分多 ASIN | 完整结论，但注明"人工细分样本（N 个 ASIN），代表该细分而非全类目" |
| M3 | 纯 Node | 完整结论（市场环境） |
| M4 | 纯单 ASIN | **降级**：结论表述为"基于单竞品流量面的初步判断"，q5/q6 回答中必须建议补充 Node 组成 M1 验证市场面；不得把该 ASIN 的独征（如特定语言词、特定码段）表述为市场特征 |

另需在 analysis.json 顶层产出 `category_label`：调研品类的简短中文标签（≤8 字，如"女童泳装""面包发酵篮"），用于报告看板与文件名。

v1.6.2允许把最终分析拆成 `market`、`product`、`decision` 三段分别生成和缓存。合并后的 `analysis.json` 应记录 `analysis_mode`（`model_split|hybrid_fallback|deterministic_fallback`）和 `analysis_sections` 来源元数据。任一分段模型输出在限定修复次数后仍未通过时，允许以本地事实做保守确定性降级：产品方向可为空、不得补造功能，进入等级固定为 `VALIDATE`，并在数据质量页标记降级原因。

## 1. 市场需求地图

按需求标签聚合：关键词数量、代表关键词、搜索量中位数、购买率中位数、点击集中度中位数、转化集中度中位数、当前 PPC 中位数、24 个月趋势方向。

禁止将语义重叠关键词的搜索量直接求和称为市场规模；用"需求体量指数"或分位数表达。

## 2. 高体量流量陷阱识别

识别特征：搜索量大但相关度低 / 产品形态不同 / 平均售价与目标商品严重不符 / 购买率低 / 只覆盖少数异常 ASIN / 对目标产品流量贡献有限。

输出"不能因为体量高就当核心词"的证据列表。

## 3. 点击—转化集中度象限

| 点击集中度 | 转化集中度 | 解释 |
|---|---|---|
| 高 | 高 | 点击与订单均被头部锁定，进入壁垒高 |
| 高 | 低 | 头部吸引点击但订单分散，可能存在产品或页面突破机会 |
| 低 | 高 | 点击分散但成交集中，产品力、评价或价格壁垒较强 |
| 低 | 低 | 流量和成交分散，相对易切入但需核验需求质量 |

高/低按当前词池中位数判定（见 metric-definitions.md §5），报告必须显示实际数值。

**items 选取规则（固定，不得自行发挥）**：先排除品牌词、错类词和泛流量词，再从「点击集中度与转化集中度双指标齐全（非空且非全零）」的关键词中按月搜索量取 Top 15；有效词不足 15 个时全部纳入。click/conv 任一缺失的词不进象限，在 `12_数据质量` 说明。没有有效双指标词时首页显示缺数提示，不保留空白散点图框。quadrant 字段用 high-high / high-low / low-high / low-low 四值（脚本统一转中文展示）。图表分组只使用总表流量性质：核心/精准词、人群长尾、其他长尾；不得再用趋势词入选状态分组。

## 4. 关键词机会矩阵

机会类型：核心需求机会 / 精准成交机会 / 长尾细分机会 / 产品差异化机会 / 成本压力词 / 头部垄断词 / 泛流量陷阱 / 衰退词 / 季节性机会。

保留各项证据字段，不输出单一黑箱分数。

## 5. 产品定义假设

从实际关键词及其标签提取主产品形态、材料、功能、规格、数量、场景、人群、套装/配件和礼品语义。没有关键词证据的字段必须留空，禁止补写“防漏、易清洁、多格、密封盖”等常见但未被本次关键词支持的功能。

`product_hypotheses` 每行固定字段：`demand`、`source_keywords`、`evidence`、`user_problem`、`product_form`、`material`、`function`、`spec`、`bundle_accessory`、`product_response`、`confidence`、`inference_level`、`validation_needed`。

- `source_keywords` 必须来自关键词总表，不能是概念标签；
- `evidence` 至少保留搜索量、购买率、集中度或PPC中的实际可得值；
- `user_problem` 明确标记“推断”；`product_response` 必须出现“假设”；`inference_level` 固定为 `hypothesis`；
- 评论痛点、供应链可行性、销量与利润只能列入 `validation_needed`，不得写成已证实结论。

写完必须通过 `validate_analysis.py`，否则停止生成Excel。

## 6. 单品、套装与配件结构判断

比较单品词 / 套装词 / 配件词 / 规格组合词的搜索量、购买率、集中度、PPC 和趋势。输出：

- 建议优先验证的主商品形态；
- 是否存在套装需求；
- 哪些配件可能是真需求、哪些可能只是卖家堆件数；
- 建议的主 SKU / 辅助 SKU 假设。

不分析 SKU 之间的关键词重叠和内部蚕食（V1 排除）。

## 7. 季节性与进入时间轴

不仅指出峰值月份，还要识别需求启动拐点。输出：需求开始连续增长的月份、峰值月份、旺季持续时间、淡季持续时间、是否两年重复、建议的调研/产品冻结/下单/运输/入仓/上线月份区间。

时间轴允许用户输入：产品开发周期、打样周期、生产周期、运输周期、Amazon 入仓周期、广告预热周期。**未输入这些参数时只给相对提前量（如"峰值前 4–6 个月完成产品冻结"），不给虚假的精确日期。**

## 8. 市场生命周期

分类：结构性增长 / 稳定成熟 / 强季节性 / 短期异动 / 需求迁移 / 衰退。

判断依据：核心词与长尾词同比；连续增长是否持续；两年季节曲线是否重复；单品词与套装词是否迁移；传统材料与新材料词是否迁移；新表达增长是否伴随旧表达下降。

## 9. 市场进入结论

必须回答 7 个问题：① 需求是否真实、稳定、增长或季节性；② 竞争壁垒主要来自流量、成交、广告成本还是产品形态；③ 最值得验证的产品方向；④ 最适合切入的核心与长尾需求；⑤ 现在是否值得继续调研；⑥ 何时开始产品和供应链动作；⑦ 哪些结论仍缺数据。

结论等级（每档必须给数据证据和限制条件）：

- `GO` 值得优先推进
- `CONDITIONAL` 有条件推进
- `VALIDATE` 继续验证
- `HOLD` 暂不建议进入

## 10. 风险与验证清单

自动声明关键词报告**不能证明**：实际市场销售额、产品利润、评论中的真实痛点、某功能一定值得开发、新品实际转化率、真实 ACOS、具体备货数量、供应链可行性、专利/认证/合规风险。

按当前发现生成下一步验证任务：需抓取哪些评论、检查哪些头部产品、比较哪些价格和销量、向供应链确认哪些材料/结构/成本、是否需要专利或合规检索、哪些假设应优先验证。

## analysis.json 建议结构

```json
{
  "marketplace": "DE",
  "mode": "M1",
  "category_label": "女童泳装",
  "demand_map": [{"tag": "material:rattan", "kw_count": 8, "representative": ["..."], "searches_median": 0, "purchase_rate_median": 0, "click_conc_median": 0, "conv_conc_median": 0, "ppc_median": 0, "trend_direction": "up|flat|down|seasonal", "product_implication": "..."}],
  "traffic_traps": [{"keyword": "...", "evidence": "..."}],
  "quadrant": {"click_median": 0, "conv_median": 0, "items": [{"keyword": "...", "click": 0, "conv": 0, "quadrant": "high-high", "interpretation": "..."}]},
  "opportunity_matrix": [{"keyword": "...", "type": "core|precise|longtail|differentiation|cost_pressure|monopoly|trap|declining|seasonal", "evidence": {"searches": 0, "relevance": "A", "purchase_rate": 0, "concentration": 0, "ppc": 0}, "confidence": "high|mid|low"}],
  "product_hypotheses": [{"demand": "...", "source_keywords": ["..."], "evidence": {"searches": 0, "purchase_rate": 0}, "user_problem": "...（推断）", "product_form": "", "material": "", "function": "", "spec": "", "bundle_accessory": "", "product_response": "...（假设）", "confidence": "high|mid|low", "inference_level": "hypothesis", "validation_needed": "..."}],
  "sku_structure": {"main_form": "...", "bundle_demand": "...", "real_accessories": ["..."], "fake_accessories": ["..."], "sku_hypothesis": "...(假设)"},
  "seasonality": {"growth_start_month": "...", "peak_months": ["..."], "peak_duration": "...", "off_duration": "...", "two_year_repeat": true, "timeline_advice": "..."},
  "lifecycle": {"classification": "growth|mature|seasonal|spike|migration|decline", "evidence": "..."},
  "entry_conclusion": {"grade": "GO|CONDITIONAL|VALIDATE|HOLD", "answers": {"q1": "...", "q2": "...", "q3": "...", "q4": "...", "q5": "...", "q6": "...", "q7": "..."}, "evidence": "...", "limitations": "..."},
  "risk_checklist": {"cannot_prove": ["..."], "next_validations": ["..."]}
}
```
