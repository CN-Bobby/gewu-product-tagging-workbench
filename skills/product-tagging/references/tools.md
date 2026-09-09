# 工作台工具边界

先 read_worksheet 获取当前列ID、修订、商品和文字底稿摘要；按需 read_product_source 获取 sku、parameters、title 等原文及底稿证据。read_tagging_rules(topic) 的主题为 core/text/attributes/normalization/tools/output。read_tasks 查询进度、失败和耗时。

propose_changes 是通用变更提案工具，没有人工确认或直接写入权限。add/revise 的 definition 是嵌套对象，包含 key/name/type/definition/options/source/result_shape，并可带 judgment。result_shape 只能是 scalar_open、scalar_enum、multi_enum、boolean。judgment 的 target/include/exclude/conflict/unknown 是字符串，fields 是字符串列表，synonyms 是字典，examples 是列表。不要猜参数形态或反复试相同错误。

新增或修改与执行合并提交：changes=[{kind:"add",definition:{key:"size_band",name:"尺码段",type:"INTERPRETATION",source:"text",definition:"按确认的年龄边界分类",options:["小童","中童","大童","青少年"],fields:["sku"],cardinality:"single",result_shape:"scalar_enum"}},{kind:"execute",targets:[{column_id:"$size_band",product_ids:["工作表商品ID"]}],overwrite:false,refresh_images:false}]。多列执行必须为每列提供独立 target，不能用共享 product_ids 自动交叉。绑定这个提案的明确“确认执行”可在网页对话直接执行，无需再去右侧安排任务。

普通局部处理用 query_scope(filters=[{column_id:"raw:件数标签",values:[2]}])。失败任务先 read_tasks，再用 propose_failed_repair(job_ids=[...])；它由服务端冻结各列真正可修复的单元格并直接生成待确认提案。需要核查时用 read_repair_scope。不要自行把多列失败行合并为共同商品范围。read_fields 查任意原始字段，read_profile 查全量覆盖、分布与取样依据。

correct 为修正提案；expand 为类别拆列或新属性提案，带 column_id、attributes。明确 single 的类别遇到多值先修正规则或结果。条件依赖写入 when:{column_id:"上游列ID",values:["印花+纯色","完全印花"]}，应用自动加入依赖；只写自然语言不足以落实条件执行。

开放尺码值使用 result_shape:"scalar_open"、method:"text_rule"、parser:{operation:"size_value",field:"sku",unknown:null}。尺码分段使用 result_shape:"scalar_enum"、method:"text_rule"、parser:{operation:"size_age",field:"sku",bins:[{min:0,max:4,value:"小童"},{min:4,max:9,value:"中童"},{min:9,max:13,value:"大童"},{min:13,max:null,value:"青少年"}],unknown:null}。两者对范围取更大数，支持裸数字、T、Y和Years；字母码没有对照表时保持未知。该解析器不调用模型，也不执行任意代码。

每份提案说明列、商品范围、方式、图片需求、已有结果处理。范围可为全部、筛选或勾选，抽样可选。绑定提案的“确认执行”由工作台识别，模型不能伪造确认。

mapping/bins 使用程序加工；模型不逐商品填表、不运行 shell 或子任务。看到工具失败就说明原因，不能声称已经写入。文字底稿自动提取与标签提案分开；read_tasks 可查看前者的任务状态。
