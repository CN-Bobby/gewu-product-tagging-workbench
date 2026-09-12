---
name: product-tagging
description: Use when discussing, defining, calibrating, executing, or repairing product tags from spreadsheet text and product images, especially for entity ownership, multi-component products, dense collections, visual evidence, or ambiguous label boundaries.
---

# 商品通识打标

先确定工作模式、结论层级和目标实体，再调用与当前标签相关的少量模块。不要把整套知识塞进一次请求，也不要把品类常识当成商品事实。

## 路由顺序

1. 模式：advisor（讨论建议）、definition/calibration（形成或校准定义）、execution（批量填表）、repair（修复失败）。
2. 维度：功能、结构、材质、主题、外观；外观可细化为形态、颜色、印花、纹理、表面处理/CMF。
3. 大类：服装、水杯、3C数码、家居装饰、收纳、工具、手工配件、玩具/活动套装、礼品。
4. 产品形态：多组件、多变体展示、安装系统、同质集合、异质套装、电子系统、礼品套装。
5. 只加载命中的模式、维度、大类与形态模块；核心证据、实体范围、校准、成本和运行契约始终加载。

## 不可跳过的判断

- 明确结论层级：单图观察、单商品结论、数据集标准或单元格结果；不得由单图直接声称全局规律。
- 区分主商品、组成件、配件、演示物、人物/环境、包装、颜色缩略图与文字图标；属性必须绑定实体和区域。
- 观察值使用 observed / inferred / unknown / conflict。看不清、未展示和证据冲突均不得猜测。
- 用户确认的标签定义才可批量执行。执行中发现新类别只累计“定义缺口”，任务结束后集中建议，不自动拆列。
- 能由结构化文字或程序规则确定时不调用视觉模型；视觉观察按图片指纹复用。
- 代表样本只代表已观察样本。先看6–10件，每组最多6件；出现新类型、低确定性或覆盖不足再扩到24、最多60。全量看图必须先报告预计请求量与成本并由用户确认。

## 运行产物

校准产生可复用 observation snapshot 和 visual standard；确认定义后编译 runtime profile。正式任务冻结 Skill 版本、模块ID和哈希、输入策略、标签边界及未知/冲突策略，后续讨论不得改变已启动任务。

旧版文字、属性、归一、工具和输出规则继续作为兼容参考；工作台只允许模型提出方案，由用户确认后再写入或执行。
