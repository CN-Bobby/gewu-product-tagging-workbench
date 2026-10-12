from __future__ import annotations

import json
from pathlib import Path

from .config import PACKAGE_ROOT
from .full_voc_v2 import PROMPT_VERSION
from .full_voc_v2_extract import EXTRACT_PROMPT_VERSION


PROMPT_DIR = PACKAGE_ROOT / "prompts"

DEFINITIONS = """
统一定义：人群是会改变任务或成功标准的实际使用者特征；场景是会改变任务、约束或成功标准的情境，不能只写地点；动机回答为什么现在要做；任务回答用户想取得什么进展并保持解决方案无关；痛点是失败、风险、损失、阻断或明显负担；痒点是任务可完成但效率、体验、审美或情绪仍可改善；Workaround 是为绕开限制而增加、替代、组合或放弃的动作。事实来自评论原文；推理必须单列依据；外部资料不能成为 Amazon 评论频率或站内人群事实。缺失信息使用空字符串或空数组，严禁靠常识补写。
正例：原文“胶不粘，我又加了封层才固定住”可拆为粘附失败事件，Workaround=增加封层。反例：仅因购买了美甲用品推断用户是专业美甲师；仅因在家使用把“家庭”当作决定性场景；把站外流行趋势计入评论出现率。
""".strip()


class PromptRegistry:
    def __init__(self, root: Path = PROMPT_DIR):
        self.root = Path(root)

    def load(self, task):
        if task == "full_voc_v2":
            prompt_path = self.root / "full_voc_v2.txt"
            schema_path = self.root / "full_voc_v2_schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            return {"version": PROMPT_VERSION, "schema": schema}, prompt_path.read_text(encoding="utf-8-sig")
        if task in ("full_voc_v2_extract", "full_voc_v2_synthesis", "full_voc_v2_assign",
                    "full_voc_v2_dictionary"):
            prompt_path = self.root / (task + ".txt")
            schema_path = self.root / (task + "_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            if task.endswith("extract"):
                version = EXTRACT_PROMPT_VERSION
            elif task.endswith("synthesis"):
                version = PROMPT_VERSION + "-synthesis-compact"
            elif task.endswith("dictionary"):
                version = PROMPT_VERSION + "-dictionary-v1"
            else:
                version = PROMPT_VERSION + "-assign-v1"
            return {"version": version, "schema": schema}, prompt_path.read_text(encoding="utf-8-sig")
        data = json.loads((self.root / (task + ".json")).read_text(encoding="utf-8"))
        body = "\n\n".join([
            "任务目标：" + data["objective"],
            "禁止项：" + "；".join(data["forbidden"]),
            DEFINITIONS,
            "本任务正例：" + data["positive_example"],
            "本任务反例：" + data["negative_example"],
            "输入字段：" + "、".join(data["input_fields"]),
            "输出必须严格匹配调用方提供的 JSON Schema；不得输出 Markdown 或额外说明。",
            "运行元数据由应用记录：prompt_version=%s；模型名与运行参数不得写入分析事实。" % data["version"],
        ])
        return data, body
