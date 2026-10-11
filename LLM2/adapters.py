# 适配器层：把 LLM2 的输出分发给硬件和 Home Assistant。
# Adapter layer: dispatches LLM2 output to hardware and Home Assistant.
#
# 硬件接口（扬声器、屏幕/舵机）只定义抽象类 + 控制台实现，真机到了只需继承并替换。
# Hardware interfaces (speaker, display/servo) are abstract classes + console implementations;
# subclass and swap them when the real hardware arrives.
# Home Assistant 是真实的 REST 调用（未配置令牌时自动 dry-run）。
# Home Assistant is a REAL REST client (automatic dry-run when no token is configured).
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import httpx

from affect import Affect
from schemas import HAAction

log = logging.getLogger("pebble.adapters")


# ---------------- 扬声器 / Speaker ----------------
class Speaker(ABC):
    @abstractmethod
    def speak(self, text: str, affect: Affect) -> None:
        """播放语音；affect（效价、唤醒度）可用来调语速/音色。
        Play speech; `affect` (valence, arousal) may tune rate/voice."""


class ConsoleSpeaker(Speaker):
    def speak(self, text: str, affect: Affect) -> None:
        print(f"[SPEAKER/{affect.label}] {text}")


# 真机示例 / Real-hardware sketch:
# class Pyttsx3Speaker(Speaker):
#     def __init__(self): import pyttsx3; self.e = pyttsx3.init()
#     def speak(self, text, affect): self.e.say(text); self.e.runAndWait()


# ---------------- 屏幕 + 舵机 / Display + servos ----------------
class Face(ABC):
    @abstractmethod
    def show_emotion(self, affect: Affect) -> None:
        """按 Russell 坐标切换屏幕表情并驱动舵机。
        Show the affect (Russell valence x arousal) on the screens and servos."""


class ConsoleFace(Face):
    def show_emotion(self, affect: Affect) -> None:
        print(f"[FACE] {affect}")


# 真机示例 / Real-hardware sketch:
# class ServoFace(Face):
#     def show_emotion(self, affect):
#         servo.angle = 90 + 15 * affect.valence      # head up when pleasant
#         screen.draw(openness=0.5 + 0.5 * affect.arousal, smile=affect.valence)


# ---------------- Home Assistant（真实 REST）/ Home Assistant (real REST) ----------------
class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        allowed_domains: frozenset[str],
        allowed_entities: frozenset[str] | None = None,
        timeout: float = 5.0,
    ):
        self.base_url = base_url
        self.token = token
        self.allowed_domains = allowed_domains
        # 非空时，只允许调用清单里的实体 / when non-empty, only listed entities are callable
        self.allowed_entities = allowed_entities or frozenset()
        self.dry_run = not (base_url and token)
        # 长驻连接池 / long-lived connection pool
        self._http = httpx.Client(timeout=timeout)

    def execute(self, action: HAAction) -> bool:
        """执行一个动作；返回是否成功。永不抛异常，避免一个动作失败拖垮整条流水线。
        Run one action; returns success. Never raises, so one failure cannot break the pipeline."""
        try:
            domain, _, service = action.service.partition(".")
            if not domain or not service:
                log.warning("非法 service / invalid service: %r", action.service)
                return False
            # 安全检查 / Safety checks
            if domain not in self.allowed_domains:
                log.warning("domain 不在白名单 / domain not allowed: %s", domain)
                return False
            if self.allowed_entities and action.entity_id not in self.allowed_entities:
                log.warning("实体不在清单 / entity not in catalogue: %s", action.entity_id)
                return False
            extra = json.loads(action.data_json or "{}")
            if not isinstance(extra, dict):
                log.warning("data_json 必须是对象 / data_json must be an object")
                return False
            payload = {**extra, "entity_id": action.entity_id}

            if self.dry_run:
                print(f"[HA dry-run] POST /api/services/{domain}/{service} {payload}")
                return True
            resp = self._http.post(
                f"{self.base_url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            resp.raise_for_status()
            print(f"[HA] {domain}.{service} -> {action.entity_id} OK")
            return True
        except Exception as e:  # noqa: BLE001
            log.error("HA 调用失败 / HA call failed: %s", e)
            return False
