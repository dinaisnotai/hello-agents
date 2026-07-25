"""LLM服务模块"""

import os
import sys

from hello_agents import HelloAgentsLLM
from ..config import get_settings

# 全局LLM实例
_llm_instance = None


def _configure_utf8_console() -> None:
    """Keep HelloAgents' Unicode trace logs from crashing Windows GBK consoles."""

    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def get_llm() -> HelloAgentsLLM:
    """
    获取LLM实例(单例模式)
    
    Returns:
        HelloAgentsLLM实例
    """
    global _llm_instance
    _configure_utf8_console()
    
    if _llm_instance is None:
        settings = get_settings()
        
        # HelloAgentsLLM会自动从环境变量读取配置
        # 包括OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL等
        _llm_instance = HelloAgentsLLM()
        
        print("LLM 服务初始化成功")
        print(f"   提供商: {getattr(_llm_instance, 'provider', '由 HelloAgents 配置')}")
        print(f"   模型: {getattr(_llm_instance, 'model', '默认模型')}")
        print(f"   单次调用超时: {getattr(_llm_instance, 'timeout', '默认')} 秒")
    
    return _llm_instance


def reset_llm():
    """重置LLM实例(用于测试或重新配置)"""
    global _llm_instance
    _llm_instance = None

