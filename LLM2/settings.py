# 配置模块：所有可调参数都从环境变量读取。
# Settings module: every tunable value is read from environment variables.
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 仓库根目录 / Repository root
ROOT = Path(__file__).resolve().parent.parent
# LLM1 的数据库默认位置（与 LLM1/character_generator.py 的 DB_PATH 保持一致）
# Default DB location written by LLM1 (same as DB_PATH in LLM1/character_generator.py)
DEFAULT_DB_PATH = ROOT / "LLM1" / "data" / "characters.db"


def _csv(name: str, default: str) -> frozenset[str]:
    """读取逗号分隔的环境变量 / Read a comma-separated env var."""
    return frozenset(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # --- OpenAI ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""  # 留空使用官方地址 / leave empty for the official endpoint

    # --- 数据库 / Database ---
    db_path: Path = DEFAULT_DB_PATH
    default_nfc_tag_id: str = ""  # 启动时默认角色（可选）/ optional startup character

    # --- Home Assistant ---
    ha_base_url: str = ""  # 例如 http://homeassistant.local:8123 / e.g. http://homeassistant.local:8123
    ha_token: str = ""  # 长期访问令牌 / long-lived access token
    ha_timeout: float = 5.0
    # 安全白名单：LLM 只能调用这些 domain，防止乱控设备（如门锁）
    # Safety allowlist: the LLM may only call these domains (e.g. never locks)
    ha_allowed_domains: frozenset[str] = field(default_factory=frozenset)
    # 可选：实体描述文件，告诉 LLM 有哪些设备 / optional entity catalogue for the LLM
    ha_entities_file: Path = ROOT / "LLM2" / "ha_entities.json"

    # --- HTTP 服务 / HTTP server ---
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    # 非空则要求请求头 X-Pebble-Token / if set, require the X-Pebble-Token header
    api_token: str = ""

    # --- 对话记忆 / Conversation memory ---
    history_turns: int = 6

    @property
    def ha_dry_run(self) -> bool:
        """未配置 HA 时只打印不联网 / Without HA config, log only and do not touch the network."""
        return not (self.ha_base_url and self.ha_token)


def load_settings() -> Settings:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "缺少环境变量 OPENAI_API_KEY / Missing environment variable OPENAI_API_KEY"
        )
    return Settings(
        openai_api_key=key,
        openai_model=os.getenv("LLM2_MODEL", "gpt-4o-mini"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
        db_path=Path(os.getenv("PEBBLE_DB_PATH", str(DEFAULT_DB_PATH))),
        default_nfc_tag_id=os.getenv("DEFAULT_NFC_TAG_ID", ""),
        ha_base_url=os.getenv("HA_BASE_URL", "").rstrip("/"),
        ha_token=os.getenv("HA_TOKEN", ""),
        ha_timeout=float(os.getenv("HA_TIMEOUT", "5")),
        ha_allowed_domains=_csv(
            "HA_ALLOWED_DOMAINS",
            "light,switch,fan,cover,climate,media_player,scene,input_boolean",
        ),
        ha_entities_file=Path(os.getenv("HA_ENTITIES_FILE", str(ROOT / "LLM2" / "ha_entities.json"))),
        server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.getenv("SERVER_PORT", "8000")),
        api_token=os.getenv("PEBBLE_API_TOKEN", ""),
        history_turns=int(os.getenv("HISTORY_TURNS", "6")),
    )
