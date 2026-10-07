# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Stand-in for the OpenAI API, for testing the voice pipeline without an API key.

Speaks just enough of the OpenAI chat-completions protocol for LLM2: it returns a canned reply
in LLM2's JSON output format, in the active character's style. Not a language model — it only
exists so the real LLM2 code can run end to end offline.

    uv run fake_openai.py                      # http://127.0.0.1:8790/v1
    # then run LLM2 with OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake
"""
import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# (keywords in the user's text, emotion) — first match wins
EMOTION_RULES = [
    (("tired", "sleep", "bed"), "sleepy"),
    (("sad", "lonely", "miss"), "sad"),
    (("worried", "scared", "medicine", "medication", "pain"), "worried"),
    (("angry", "annoying", "hate"), "angry"),
    (("great", "awesome", "yay", "birthday"), "excited"),
    (("thanks", "thank you", "hello", "hi"), "happy"),
]

REPLIES = {
    "Baymax": "I heard you say: {text} I am here to help. How are you feeling?",
    "Marvin": "You said: {text} Noted. Riveting.",
    "R2-D2": "Beep boop. {text} Beep.",
}


def fake_reply(messages: list[dict]) -> dict:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    name = (re.search(r"Name: (.+)", system) or [None, "Robot"])[1].strip()
    try:
        text = json.loads(messages[-1]["content"])["text"]
    except (KeyError, ValueError, TypeError):
        text = str(messages[-1].get("content", ""))
    lower = f" {text.lower()} "
    words_in = set(re.findall(r"[a-z']+", lower))
    emotion = next((e for words, e in EMOTION_RULES
                    if any((w in lower) if " " in w else (w in words_in) for w in words)), "neutral")
    actions = []
    light = re.search(r"- (light\.\w+):", system)
    if "light" in lower and light:
        service = "light.turn_off" if " off" in lower else "light.turn_on"
        actions.append({"service": service, "entity_id": light[1], "data_json": "{}"})
    template = REPLIES.get(name, "You said: {text}")
    return {"tts_text": template.format(text=text.rstrip(".") + "."), "emotion": emotion,
            "ha_actions": actions}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        content = json.dumps(fake_reply(req["messages"]))
        body = json.dumps({
            "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
            "model": req.get("model", "fake"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.log_message("reply: %s", content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OpenAI endpoint for offline LLM2 tests.")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    print(f"fake OpenAI on http://127.0.0.1:{args.port}/v1 (OPENAI_BASE_URL)")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
