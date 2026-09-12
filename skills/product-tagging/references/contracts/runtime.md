# 运行契约

`observation_snapshot`：image_id/hash、model、input_policy、modules、entities、relationships、regions、dimensions、evidence、unknown、conflicts。

`visual_standard`：goal、sample_ids、sample_count/total、coverage、candidate_values、include/exclude boundaries、unresolved、stability、confirmation/revision。

`runtime_profile`：label/key、mode、target_scope、dimension、include/exclude、allowed_values、result_shape/cardinality、unknown/conflict、source/fields、input_policy、module_ids/module_hashes、examples。执行只发送 runtime profile 与命中模块；完整 Skill 冻结包仅用于审计。
