# 数据库读取模块：根据 nfc_tag_id 读取 LLM1 生成的角色配置。
# DB reader: load the character profile written by LLM1 by nfc_tag_id.
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from settings import DEFAULT_DB_PATH, ROOT

# 复用 LLM1 的 Pydantic 模型来校验数据（character_generator 顶层只依赖 pydantic，
# llama_cpp 是函数内懒加载，所以这里不需要安装 llama-cpp）。
# Reuse LLM1's Pydantic model for validation (character_generator only imports pydantic at
# module level; llama_cpp is lazy-loaded, so llama-cpp is NOT required here).
sys.path.insert(0, str(ROOT / "LLM1"))
from character_generator import CharacterProfile  # noqa: E402


class CharacterNotFound(KeyError):
    """该 NFC 标签没有对应角色 / No character is stored for this NFC tag."""


@dataclass(frozen=True)
class StoredCharacter:
    nfc_tag_id: str
    character_id: str
    version: int
    profile: CharacterProfile


def get_character(nfc_tag_id: str, db_path: Path = DEFAULT_DB_PATH) -> StoredCharacter:
    """按 nfc_tag_id 读取角色；找不到抛 CharacterNotFound。
    Load a character by nfc_tag_id; raises CharacterNotFound if missing.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"找不到数据库 {db_path}，请先运行 `cd LLM1 && uv run seed_characters.py` / "
            f"Database {db_path} not found; run `cd LLM1 && uv run seed_characters.py` first"
        )
    # 只读方式打开，避免 LLM2 误写 LLM1 的数据
    # Open read-only so LLM2 can never modify LLM1's data
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT nfc_tag_id, character_id, profile_json, version "
            "FROM characters WHERE nfc_tag_id = ?",
            (nfc_tag_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise CharacterNotFound(f"No character for nfc_tag_id={nfc_tag_id!r}")
    tag, cid, profile_json, version = row
    return StoredCharacter(tag, cid, version, CharacterProfile.model_validate_json(profile_json))


def list_tags(db_path: Path = DEFAULT_DB_PATH) -> list[str]:
    """列出库里所有标签（调试用）/ List all stored tags (for debugging)."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT nfc_tag_id FROM characters ORDER BY nfc_tag_id")]
    finally:
        conn.close()
