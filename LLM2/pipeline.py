# 核心流水线：常驻内存的 LLM2 实例。
# Core pipeline: the long-lived, in-memory LLM2 instance.
# 流程 / Flow: 事件 -> 取当前角色 -> 拼 Prompt -> OpenAI -> 解析 JSON -> 分发硬件/HA
#              event -> active character -> build prompt -> OpenAI -> parse JSON -> dispatch
from __future__ import annotations

import json
import logging
import threading
from collections import deque

from openai import OpenAI
from pydantic import ValidationError

from adapters import ConsoleFace, ConsoleSpeaker, Face, HomeAssistantClient, Speaker
from character_store import CharacterNotFound, StoredCharacter, get_character
from prompt_builder import build_system_prompt
from schemas import OUTPUT_JSON_SCHEMA, LLM2Input, LLM2Output
from settings import Settings

log = logging.getLogger("pebble.pipeline")


class NoActiveCharacter(RuntimeError):
    """尚未刷卡选择角色 / No character selected yet (no NFC tag scanned)."""


class LLM2Pipeline:
    def __init__(
        self,
        settings: Settings,
        speaker: Speaker | None = None,
        face: Face | None = None,
        ha: HomeAssistantClient | None = None,
    ):
        self.s = settings
        # OpenAI 客户端只创建一次并常驻（内部复用 HTTP 连接）
        # The OpenAI client is created once and kept alive (HTTP connections are reused)
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=30.0,
            max_retries=2,
        )
        self.speaker = speaker or ConsoleSpeaker()
        self.face = face or ConsoleFace()
        self.ha_entities = self._load_entities()
        self.ha = ha or HomeAssistantClient(
            settings.ha_base_url,
            settings.ha_token,
            settings.ha_allowed_domains,
            frozenset(self.ha_entities),
            settings.ha_timeout,
        )
        # 串行处理事件 / process events one at a time
        self._lock = threading.Lock()
        self._active: StoredCharacter | None = None
        self._system_prompt = ""
        self._history: deque[dict] = deque(maxlen=settings.history_turns * 2)
        if settings.default_nfc_tag_id:
            self.set_active_tag(settings.default_nfc_tag_id)

    # ---- 设备清单 / entity catalogue ----
    def _load_entities(self) -> dict[str, str]:
        f = self.s.ha_entities_file
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        return {}

    # ---- NFC 刷卡 / NFC scan ----
    def set_active_tag(self, nfc_tag_id: str) -> StoredCharacter:
        """刷卡切换角色：重新读库、重建 Prompt、清空对话记忆。
        Scan switches the character: reload from DB, rebuild prompt, clear memory."""
        char = get_character(nfc_tag_id, self.s.db_path)  # 找不到会抛 CharacterNotFound
        with self._lock:
            self._active = char
            self._system_prompt = build_system_prompt(char, self.ha_entities)
            self._history.clear()
        log.info("当前角色 / active character: %s (v%s)", char.profile.display_name, char.version)
        return char

    @property
    def active(self) -> StoredCharacter | None:
        return self._active

    # ---- 推理 / inference ----
    def _infer(self, event: LLM2Input) -> tuple[LLM2Output, str]:
        user_msg = json.dumps(event.model_dump(), ensure_ascii=False)
        messages = [
            {"role": "system", "content": self._system_prompt},
            *self._history,
            {"role": "user", "content": user_msg},
        ]
        last_err: Exception | None = None
        # 格式错误时重试一次 / retry once on bad output
        for _ in range(2):
            resp = self.client.chat.completions.create(
                model=self.s.openai_model,
                messages=messages,
                temperature=0.7,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "robot_output", "strict": True, "schema": OUTPUT_JSON_SCHEMA},
                },
            )
            raw = resp.choices[0].message.content or ""
            try:
                return LLM2Output.model_validate_json(raw), raw
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = e
        raise RuntimeError(f"LLM2 输出不合法 / invalid LLM2 output: {last_err}")

    # ---- 分发 / dispatch ----
    def _dispatch(self, out: LLM2Output) -> None:
        # 每个硬件独立 try，一个坏了不影响其他
        # Each sink is isolated so one failure does not affect the others
        for name, fn in (
            ("face", lambda: self.face.show_emotion(out.affect)),
            ("speaker", lambda: self.speaker.speak(out.tts_text, out.affect)),
        ):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.error("%s 失败 / %s failed: %s", name, name, e)
        for action in out.ha_actions:
            self.ha.execute(action)

    # ---- 对外入口 / public entry ----
    def handle(self, event: LLM2Input) -> LLM2Output:
        if self._active is None:
            raise NoActiveCharacter("请先刷卡选择角色 / scan an NFC tag first")
        with self._lock:
            out, raw = self._infer(event)
            self._history.append({"role": "user", "content": json.dumps(event.model_dump(), ensure_ascii=False)})
            self._history.append({"role": "assistant", "content": raw})
            self._dispatch(out)
            return out


__all__ = ["LLM2Pipeline", "NoActiveCharacter", "CharacterNotFound"]
