"""Supported user-interface locales shared across inbound adapters and ports.

定义入站适配器和端口共享的用户界面语言集合."""

from enum import StrEnum


class UiLanguage(StrEnum):
    """Languages supported by the application-owned terminal interface.

    定义应用层拥有的终端界面支持的语言."""

    ENGLISH = "en"
    SIMPLIFIED_CHINESE = "zh-CN"


__all__ = ["UiLanguage"]
