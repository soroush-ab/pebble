# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40", "pydantic>=2", "httpx>=0.27"]
# ///
"""命令行入口（无需硬件，直接在终端测试整条流水线）。
CLI entry (no hardware needed; test the whole pipeline from the terminal).

用法 / Usage:
    uv run --env-file LLM2/.env LLM2/main.py
命令 / Commands:
    /tag <nfc_tag_id>   模拟刷卡 / simulate an NFC scan
    /ha <文本 text>     模拟 HA 通知 / simulate a Home Assistant notification
    /tags               列出可用标签 / list available tags
    其他输入 / anything else = 用户语音 (STT) / user speech (STT)
"""
import logging

from character_store import CharacterNotFound, list_tags
from pipeline import LLM2Pipeline, NoActiveCharacter
from schemas import LLM2Input
from settings import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    s = load_settings()
    pipe = LLM2Pipeline(s)
    print(f"可用标签 / tags: {list_tags(s.db_path)}")
    print("命令 / commands: /tag <id>, /ha <text>, /tags, Ctrl+C 退出 / quit")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        try:
            if line == "/tags":
                print(list_tags(s.db_path))
            elif line.startswith("/tag "):
                c = pipe.set_active_tag(line[5:].strip())
                print(f"已切换 / switched to: {c.profile.display_name}")
            elif line.startswith("/ha "):
                pipe.handle(LLM2Input(source="ha_notification", text=line[4:].strip()))
            else:
                pipe.handle(LLM2Input(source="stt", text=line))
        except (CharacterNotFound, NoActiveCharacter, FileNotFoundError) as e:
            print(f"[WARN] {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] {e}")


if __name__ == "__main__":
    main()
