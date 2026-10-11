# 数据结构：LLM2 的输入与输出格式。
# Data schemas: input and output formats of LLM2.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from affect import Affect

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
    # Russell 环形模型 / Russell circumplex: 屏幕、舵机和语速都由它驱动
    # drives the eyes, the motions and the speaking rate
    valence: float = 0.0  # -1 不愉快 / unpleasant .. +1 愉快 / pleasant
    arousal: float = 0.0  # -1 低落困倦 / deactivated .. +1 激动警觉 / activated
    ha_actions: list[HAAction] = Field(default_factory=list)  # HA 调用 / HA calls

    @field_validator("valence", "arousal")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(-1.0, min(1.0, v))

    @property
    def affect(self) -> Affect:
        return Affect(self.valence, self.arousal)

    @computed_field  # 由坐标推出的标签，便于日志和显示 / label derived from the point
    @property
    def emotion(self) -> str:
        return self.affect.label


# 传给 OpenAI 的严格 JSON Schema（必须与上面的模型保持一致）
# Strict JSON Schema sent to OpenAI (must stay in sync with the models above)
OUTPUT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tts_text": {"type": "string"},
        "valence": {
            "type": "number",
            "description": "-1 unpleasant .. 0 neutral .. +1 pleasant (Russell circumplex)",
        },
        "arousal": {
            "type": "number",
            "description": "-1 sleepy/deactivated .. 0 .. +1 excited/alert (Russell circumplex)",
        },
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
    "required": ["tts_text", "valence", "arousal", "ha_actions"],
    "additionalProperties": False,
}
