# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""无硬件全链路冒烟测试：通过 HTTP 模拟 刷卡 -> 语音 -> HA 通知，检查输出结构。
Hardware-free end-to-end smoke test: simulates NFC scan -> speech -> HA notification over HTTP
and checks the output structure.

先在另一个终端启动服务 / Start the server in another terminal first:
    uv run --env-file LLM2/.env LLM2/server.py
再运行 / Then run:
    uv run LLM2/smoke_test.py [--base http://localhost:8000] [--token change-me] [--tag marvin-demo-01]
"""
import argparse
import sys

import httpx

EMOTIONS = {"happy", "sad", "angry", "neutral", "excited", "sleepy", "worried"}


def check_output(name: str, data: dict) -> bool:
    ok = (
        isinstance(data.get("tts_text"), str)
        and data["tts_text"].strip() != ""
        and data.get("emotion") in EMOTIONS
        and isinstance(data.get("ha_actions"), list)
    )
    print(("PASS" if ok else "FAIL"), name, data)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--tag", default="marvin-demo-01")
    a = ap.parse_args()
    headers = {"X-Pebble-Token": a.token} if a.token else {}
    results: list[bool] = []
    with httpx.Client(base_url=a.base, headers=headers, timeout=60) as c:
        r = c.get("/health")
        results.append(r.status_code == 200)
        print("health", r.status_code, r.text)

        # 1. 刷卡 / NFC scan
        r = c.post("/nfc", json={"nfc_tag_id": a.tag})
        results.append(r.status_code == 200)
        print("nfc", r.status_code, r.text)

        # 2. 未知标签应返回 404 / unknown tag must return 404
        r = c.post("/nfc", json={"nfc_tag_id": "does-not-exist"})
        results.append(r.status_code == 404)
        print("unknown tag ->", r.status_code)
        # 恢复正确角色 / restore the correct character
        c.post("/nfc", json={"nfc_tag_id": a.tag})

        # 3. 用户语音 / user speech
        r = c.post("/stt", json={"text": "Turn on the bedroom light"})
        results.append(r.status_code == 200 and check_output("stt", r.json()))

        # 4. HA 通知 / HA notification
        r = c.post("/ha-notification", json={"title": "Reminder", "message": "Time to take your medicine"})
        results.append(r.status_code == 200 and check_output("ha-notification", r.json()))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
