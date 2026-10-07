# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40", "pydantic>=2", "httpx>=0.27", "fastapi>=0.110", "uvicorn>=0.29"]
# ///
"""HTTP 服务入口：让 NFC 读卡程序、STT 程序、Home Assistant 通过 HTTP 把事件送进来。
HTTP server entry: lets the NFC reader, the STT service and Home Assistant push events via HTTP.

用法 / Usage:  uv run --env-file LLM2/.env LLM2/server.py
接口文档 / API docs: http://localhost:8000/docs
"""
import logging

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from character_store import CharacterNotFound
from pipeline import LLM2Pipeline, NoActiveCharacter
from schemas import LLM2Input, LLM2Output
from settings import load_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
settings = load_settings()
# 进程启动时创建一次，常驻内存 / created once at startup, stays in memory
pipeline = LLM2Pipeline(settings)
app = FastAPI(title="Pebble LLM2")


def check_token(x_pebble_token: str = Header(default="")) -> None:
    """若配置了 PEBBLE_API_TOKEN，则校验请求头 / Validate the header if PEBBLE_API_TOKEN is set."""
    if settings.api_token and x_pebble_token != settings.api_token:
        raise HTTPException(401, "invalid token")


class NfcReq(BaseModel):
    nfc_tag_id: str


class TextReq(BaseModel):
    text: str


class HaReq(BaseModel):
    message: str
    title: str = ""


def _run(event: LLM2Input) -> LLM2Output:
    try:
        return pipeline.handle(event)
    except NoActiveCharacter as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.get("/health")
def health():
    a = pipeline.active
    return {"ok": True, "active": a.profile.display_name if a else None}


@app.post("/nfc", dependencies=[Depends(check_token)])
def nfc(req: NfcReq):
    """刷卡 / NFC scan"""
    try:
        c = pipeline.set_active_tag(req.nfc_tag_id)
    except CharacterNotFound:
        raise HTTPException(404, f"unknown nfc_tag_id: {req.nfc_tag_id}")
    return {"character": c.profile.display_name, "version": c.version}


@app.post("/stt", response_model=LLM2Output, dependencies=[Depends(check_token)])
def stt(req: TextReq):
    """用户语音转文字 / user speech-to-text"""
    return _run(LLM2Input(source="stt", text=req.text))


@app.post("/ha-notification", response_model=LLM2Output, dependencies=[Depends(check_token)])
def ha_notification(req: HaReq):
    """Home Assistant 提醒/消息 / Home Assistant reminder or message"""
    text = f"{req.title}: {req.message}" if req.title else req.message
    return _run(LLM2Input(source="ha_notification", text=text))


if __name__ == "__main__":
    uvicorn.run(app, host=settings.server_host, port=settings.server_port)
