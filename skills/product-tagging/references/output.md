# 批量填表输出

严格返回 JSON {"results":[{"product_id":"输入中的ID","raw_value":"原文或null","normalized_value":"中文标准值或null","evidence":"简短证据字符串","attributes":[{"attribute":"具体候选属性","value":true,"evidence":"简短证据字符串"}]}]}。evidence 必须是字符串，不能照搬文字底稿中的证据数组；逐字段底稿由应用另行保留。只输出请求中的商品ID，按已确认定义和判断规则填表，未知为 null，不输出推理链。

严格服从定义中的 result_shape：scalar_open 返回开放式单值；scalar_enum 只返回候选中的一个值；这两类标量结果不返回 attributes。multi_enum 返回所有明确命中的字符串列表，并提供逐属性证据。boolean 只返回 true/false/null。

attributes.attribute 是具体候选值，不是种类名。例如种类“套装件数”命中“2件套”，attribute 应为“2件套”，不能填“套装件数”。单个正命中必须与 normalized_value 一致。

种类列 normalized_value 为单值或多个明确命中的字符串列表。只要产生逐属性判断就同时保留 attributes 数组，包括单个正命中伴随的否定或未知，不可丢弃；多个命中时必须提供。每项 {attribute:"中文属性",value:true/false/null,evidence:"逐属性依据"}，不能用“或”组成虚假多选。若同一属性不确定用 null。只有至少两个明确正命中才触发首次拆列。属性列 normalized_value 必须为 true/false/null，非空判断必须有具体证据；仅缺少提及不能 false。

开放类别可发现新属性，但不自动创建列。已知候选外的明确属性也要完整保留在 attributes，交给应用提出新增属性提案。候选外的普通单值仍需按当前定义处理，不能默默扩大定义。

证据应包含可核查原文或已有观察，引用底稿时带对应来源。任务只接受其冻结规则版本；发生人工编辑、上游变化时保留冲突候选而非覆盖。
