# -*- coding: utf-8 -*-
"""关键词调研引擎配置。

只承载引擎运行需要的参数：MCP 端点与 LLM 凭据由上层（工作台设置）注入，
本模块不读写任何凭据存储，也不落盘配置文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MCP_URL = "https://mcp.sellersprite.com/mcp"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-flash"
DEFAULT_LLM_MODE = "auto"  # auto | responses | chat
# 官方输出上限 384K；关闭思考后输出预算全归正文，16384 留足余量
DEFAULT_LLM_MAX_TOKENS = 16384


@dataclass
class EngineConfig:
    """引擎运行配置（限流口径 + MCP/LLM 接入参数）。"""

    # Clean MCP endpoint; authentication is kept separately and sent as HTTP headers.
    mcp_endpoint: str = DEFAULT_MCP_URL
    mcp_headers: dict[str, str] = field(default_factory=dict, repr=False)
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    llm_mode: str = DEFAULT_LLM_MODE
    llm_temperature: float = 0.2
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_request_timeout: float = 120.0
    window_max_calls: int = 30
    window_seconds: float = 60.0
    min_call_interval: float = 1.0
