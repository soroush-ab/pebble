# 数据结构：LLM2 的输入与输出格式。
# Data schemas: input and output formats of LLM2.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 机器人可表达的情绪 / Emotions the robot can express
Emotion = Literal["happy", "sad", "angry", "neutral", "excited", "sleepy", "worried"]
EMOTIONS: list[str] = list(Emotion.__args__)  # type: ignore[attr-defined]

# 输入来源：用户语音转文字 或 Home Assistant 通知
# Input source: user speech-to-text or a Home Assistant notification
Source = Literal["stt", "ha_notification"]


class LLM2Input(BaseModel):
    source: Source
    text: str = Field(min_length=1)


class HAAction(BaseModel):
    service: str  # 例如 "light.turn_on" / e.g. "light.turn_on"
    entity_id: str  # 例如 "light.bedroom" / e.g. "light.bedroom"
    # 额外参数，序列化成 JSON 字符串（OpenAI strict 模式不允许自由格式对象）
    # Extra params serialised as a JSON string (OpenAI strict mode forbids free-form objects)
    data_json: str = "{}"


class LLM2Output(BaseModel):
    tts_text: str  # 扬声器播报文本 / text for the speaker (TTS)
    emotion: Emotion  # 屏幕/舵机情绪 / emotion for display & servos
    ha_actions: list[HAAction] = Field(default_factory=list)  # HA 调用 / HA calls


# 传给 OpenAI 的严格 JSON Schema（必须与上面的模型保持一致）
# Strict JSON Schema sent to OpenAI (must stay in sync with the models above)
OUTPUT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tts_text": {"type": "string"},
        "emotion": {"type": "string", "enum": EMOTIONS},
        "ha_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "data_json": {"type": "string"},
                },
                "required": ["service", "entity_id", "data_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tts_text", "emotion", "ha_actions"],
    "additionalProperties": False,
}
