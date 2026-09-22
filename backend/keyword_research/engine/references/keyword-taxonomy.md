# 关键词翻译规范与标签体系

## 1. 清洗与标准化（脚本完成，见 normalize_keywords.py）

1. Unicode 标准化（NFKC）；2. 转小写仅用于内部匹配，保留原始展示词；3. 清理多余空格与标点；4. 识别单复数；5. 识别连写/分写/常见拼写变体；6. 标记品牌词候选；7. 标记明显无关词候选；8. 建立同义词族（LLM 确认）；9. 合并多批次相同原词；10. 合并关联 ASIN 并保留来源追踪；11. 数值字段使用最新有效快照；12. 多站点数据不直接相加。

## 2. 中文翻译规范（LLM 完成）

每个关键词产出 6 个字段：`keyword`（原词）、`keywordCn`（MCP 原译）、`translation_fixed`（校正中文）、`translation_reason`（校正理由）、`local_context`（本地消费语境说明，必要时）、`translation_confidence`（high/mid/low）。

翻译优先级：① Amazon 类目语境 → ② 产品实际形态 → ③ 当地消费者表达 → ④ 字面翻译。

已知错误类型（必须纠正，且作为同类问题的检查范式）：

- `proofing basket` ≠"打样篮"→ 应为"发酵篮"（面包烘焙语境）；
- `banneton` ≠"楠木"→ 应为"藤编发酵篮"；
- 产品术语不得按通用词典机械翻译；德语复合词先拆词再按类目语境理解。

## 3. 需求属性标签（可多选）

`core_category` 核心品类 / `product_form` 产品形态 / `material` 材料 / `function` 功能·问题 / `spec` 规格·尺寸·数量 / `scene` 使用场景 / `audience` 目标人群 / `bundle` 套装 / `accessory` 配件 / `gift` 礼品·节日 / `brand` 品牌·竞品 / `broad` 泛相关 / `noise` 噪声。

打标时标签值尽量具体（如 material: rattan/藤编），并在标签列优先填具体类别值而非 Y/N。

### 字段契约（严格遵守，脚本只认这些字段名）

LLM 标注写回 `keyword_labeled.json` 时，每条关键词必须使用以下字段名与类型（真实任务中曾发生用 `tags`/`traffic_nature` 自建字段导致报告整列为空的事故）：

| 字段 | 类型 | 示例 |
|---|---|---|
| `demand_tags` | **list[str]**，元素为 `"前缀:值"` | `["material:rattan", "spec:9inch"]` |
| `traffic_type` | str，9 类枚举英文键 | `"audience_longtail"` |
| `traffic_type_reason` | str | `"包含人群、材料、功能、规格、套装或配件属性"` |
| `relevance` | str | `"A"` / `"B"` / `"C"` / `"D"` |
| `relevance_reason` | str | 一句可审计理由 |
| `purchase_intent` | str | `"高"` / `"中"` / `"低"` |
| `translation_fixed` | str | 校正中文 |
| `translation_reason` | str | 校正理由 |
| `translation_confidence` | str | `"high"` / `"mid"` / `"low"` |
| `is_brand` / `is_noise` | bool | — |

禁止自造字段名；禁止把 demand_tags 写成 dict。中文展示由 build 脚本统一映射，标注层保持上述枚举键。

## 4. 流量性质（单选）

`core_head` 核心大词 / `precise_category` 精准品类词 / `high_intent_longtail` 高意图长尾 / `audience_longtail` 人群长尾 / `attribute_longtail` 属性长尾 / `scene_longtail` 场景长尾 / `broad_traffic` 泛流量词 / `brand_word` 品牌词 / `wrong_category` 错类词。

流量性质不由LLM自由发挥，先完成语义标签，再运行 `classify_keywords.py` 按固定优先级生成：品牌词 → 错类词 → 泛流量词 → 人群长尾 → 场景长尾 → 属性长尾 → 精准品类词 → 核心大词 → 其他高意图长尾。`traffic_type_reason` 必须同步生成。

除品牌、错类、噪声和泛流量优先规则外，只要包含 `audience:*` 标签就固定归为 `audience_longtail`，即使同时包含材料、功能或场景。例如 `insulated lunch box for kids`、`school lunch box for boys` 均为人群长尾。

`core_head` 仅允许无具体属性修饰、通常不超过2词的品类头部表达。只要存在 `audience/material/function/spec/scene/bundle/accessory/gift` 任一标签，就不得判为核心大词。跨品类示例：

- `lunch box` → 核心大词；`bento box` → 精准品类词；
- `lunch box boys/girls/adult/kids` → 人群长尾；`insulated lunch box`、`stainless steel lunch box`、`small lunch box` → 属性长尾；
- `school lunch box` → 场景长尾；
- `rattan proofing basket` → 属性长尾；`girls summer dress` → 人群长尾，不依赖午餐盒专用词表。

标注后必须运行 `validate_labels.py`。标签与流量性质冲突、理由缺失或购买意图不是中文枚举时停止报告生成。

## 5. 相关度等级（必须附一句可审计理由）

| 等级 | 定义 |
|---|---|
| A | 搜索该词时，目标产品是主要购买对象 |
| B | 与目标产品高度相关，但包含其他产品形态 |
| C | 有场景或品类关联，购买意图较弱 |
| D | 基本无关、错类或噪声 |

## 6. 购买意图（语义判断，与 purchaseRate 数据字段分开）

- 高：明确产品、规格、材料或套装表达；
- 中：品类明确但范围较宽；
- 低：信息型、场景型或过于泛化。

## 7. 趋势词选取标准（select_trend_keywords.py 执行，LLM 可复核）

### 核心代表词（默认 5）

候选条件：相关度 A；产品形态与调研对象一致；搜索量处于候选词高位；排除品牌词、错类词、过宽泛词。当前前100流量词矩阵中可观测到的 ASIN 覆盖度、流量贡献与购买率只作为次级排序信号；未进入某 ASIN 前100时保持未知，不视为覆盖度0。

方法：语义相关度 × 月搜索量二维矩阵，先选右上象限；以当前矩阵的可观测覆盖、流量贡献、购买率为次级排序；控制同义词重复，不让近似词占满名额。`traffic_extend` 的批次元数据不得伪装成逐关键词 ASIN 覆盖。

### 代表长尾词（默认 5）

候选条件：通常 ≥3 个单词或有明确细分语义；相关度 A；购买率高于同类词中位数；搜索量达有效门槛；有明确材料/功能/规格/场景/人群/套装/配件属性；非品牌非噪声。

每个主要需求标签簇最多 1–2 个代表词，优先保证不同需求方向，而不是机械选搜索量最高的 5 个长词。

### 数量限制

默认总数 10；最小建议 5；单站点硬上限 20；多站点分别选择和调用；**所有趋势词必须在报告中说明入选原因**。
