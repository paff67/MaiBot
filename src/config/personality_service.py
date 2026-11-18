"""全局人设查询与持久化切换。"""

from pathlib import Path
from typing import Any, Dict
import asyncio
import os
import tempfile

import tomlkit

from .config_base import AttributeData
from .official_configs import ChatReplyStyleConfig, PersonalityConfig, PersonalityProfile

_switch_lock = asyncio.Lock()
# 注意：background_story 为全局统一项目背景，故意不纳入 _FIELDS，切换备选人设时保持原值不变。
_FIELDS = {
    "personality",
    "behavior_style",
    "reply_style",
    "multiple_reply_style",
    "multiple_probability",
    "owner_private_prompt",
    "persona_v2_card_id",
    "mvu_enabled",
}
_CHAT_FIELDS = {"group_chat_prompt", "private_chat_prompts"}
_PROFILE_ONLY_FIELDS = {"nickname", "alias_names", "st_preset_file", "persona_v2_card_id", "mvu_enabled"}


def current_profile(config: PersonalityConfig, reply_style: ChatReplyStyleConfig) -> Dict[str, Any]:
    """返回生效字段的快照，不返回 ConfigBase 内部元数据。"""
    return {
        "name": config.selected_profile,
        **config.model_dump(include=_FIELDS),
        **reply_style.model_dump(include=_CHAT_FIELDS),
    }


def profile_data(profile: PersonalityProfile) -> Dict[str, Any]:
    return profile.model_dump(include=_FIELDS | _CHAT_FIELDS | _PROFILE_ONLY_FIELDS | {"name"})


def persist_switch(path: Path, name: str) -> bool:
    """在线程池中读改写配置；保留其他配置与注释，原子替换避免半写文件。"""
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    section = document["personality"]
    config = PersonalityConfig.from_dict(AttributeData(), section.unwrap())
    chat_section = document["chat"]["reply_style"]
    reply_style = ChatReplyStyleConfig.from_dict(AttributeData(), chat_section.unwrap())
    bot_section = document.get("bot") if isinstance(document, dict) else None
    target = next((profile for profile in config.profiles if profile.name == name), None)
    if target is None:
        raise ValueError(f"人设不存在: {name}")
    if config.selected_profile == name:
        return False

    # 与 WebUI 一致：保存当前编辑；未命名的人设在首次切换时留档。
    profiles = [profile_data(profile) for profile in config.profiles]
    previous = next((profile for profile in profiles if profile["name"] == config.selected_profile), None)
    if previous is not None:
        previous.update(current_profile(config, reply_style))
        if previous.get("nickname") and isinstance(bot_section, dict) and bot_section.get("nickname"):
            previous["nickname"] = str(bot_section.get("nickname") or "")
        if previous.get("alias_names") and isinstance(bot_section, dict) and isinstance(bot_section.get("alias_names"), list):
            previous["alias_names"] = [str(item) for item in bot_section.get("alias_names", [])]
    else:
        backup_name = "切换前人设"
        suffix = 2
        while any(profile["name"] == backup_name for profile in profiles):
            backup_name = f"切换前人设 {suffix}"
            suffix += 1
        profiles.append({**current_profile(config, reply_style), "name": backup_name})

    for key, value in target.model_dump(include=_FIELDS).items():
        section[key] = value
    # 与人格字段在同一次原子写入中更新，避免新旧人设提示词混用。
    for key, value in target.model_dump(include=_CHAT_FIELDS).items():
        chat_section[key] = value
    if isinstance(bot_section, dict):
        if str(target.nickname or "").strip():
            bot_section["nickname"] = str(target.nickname).strip()
        if target.alias_names:
            bot_section["alias_names"] = [str(item) for item in target.alias_names]
    section["profiles"] = profiles
    section["selected_profile"] = name
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(tomlkit.dumps(document))
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


async def switch_profile(name: str) -> Dict[str, Any]:
    """切换成功仅在配置已保存且运行时热重载完成后返回。"""
    from .config import config_manager, global_config

    async with _switch_lock:
        changed = await asyncio.to_thread(persist_switch, config_manager.bot_config_path, name)
        if not await config_manager.reload_config(
            changed_scopes=["bot"], changed_sections=["personality", "chat.reply_style"]
        ):
            raise RuntimeError("人设配置已保存，但运行时热重载失败，请检查日志并重试")
        if global_config.personality.selected_profile != name:
            raise RuntimeError("人设切换期间配置被其他操作修改，请重新读取当前人设")
        return {"changed": changed, "current": current_profile(global_config.personality, global_config.chat.reply_style)}
