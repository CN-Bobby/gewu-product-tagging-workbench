# 工作台工具边界

先 read_worksheet 获取当前列ID、修订、商品和文字底稿摘要；按需 read_product_source 获取 sku、parameters、title 等原文及底稿证据。read_tagging_rules(topic) 的主题为 core/text/attributes/normalization/tools/output。read_tasks 查询进度、失败和耗时。

propose_changes 是唯一变更提案工具，没有人工确认或直接写入权限。add/revise 的 definition 是嵌套对象，包含 key/name/type/definition/options/source，并可带 judgment。judgment 的 target/include/exclude/conflict/unknown 是字符串，fields 是字符串列表，synonyms 是字典，examples 是列表。不要猜参数形态或反复试相同错误。

新增或修改与执行合并提交：changes=[{kind:"add",definition:{key:"size_band",name:"尺码段",type:"INTERPRETATION",source:"text",definition:"按确认的年龄边界分类",options:[],fields:["sku"],cardinality:"single"}},{kind:"execute",column_ids:["$size_band"],overwrite:false,refresh_images:false}]。省略 product_ids 表示全部；空数组不是全部。绑定这个提案的明确“确认执行”可在网页对话直接执行，无需再去右侧安排任务。

局部处理用 query_scope(filters=[{column_id:"raw:件数标签",values:[2]}]) 或标签列ID+states:["failed"]。返回的 scope_id 直接放入 execute/correct。它冻结当时匹配的商品行；当前表格筛选不偷偷扩大范围。多列失败可分别提案，避免把行列交叉后重打成功单元格。read_fields 查任意原始字段，read_profile 查全量覆盖、分布与取样依据。

correct 为修正提案；expand 为类别拆列或新属性提案，带 column_id、attributes。明确 single 的类别遇到多值先修正规则或结果。条件依赖写入 when:{column_id:"上游列ID",values:["印花+纯色","完全印花"]}，应用自动加入依赖；只写自然语言不足以落实条件执行。

已确认字符规则用 method:text_rule、parser:{operation:"size_age",field:"sku",bins:[{min:0,max:4,value:"小童"},{min:4,max:9,value:"中童"},{min:9,max:13,value:"大童"},{min:13,max:null,value:"青少年"}],unknown:"特殊"}。区间取更大数，单位和裸数字须在定义中确认；字母码没有对照表不能推断年龄。该解析器不执行任意代码。

每份提案说明列、商品范围、方式、图片需求、已有结果处理。范围可为全部、筛选或勾选，抽样可选。绑定提案的“确认执行”由工作台识别，模型不能伪造确认。

mapping/bins 使用程序加工；模型不逐商品填表、不运行 shell 或子任务。看到工具失败就说明原因，不能声称已经写入。文字底稿自动提取与标签提案分开；read_tasks 可查看前者的任务状态。
