# LLM2 — 运行时角色引擎 / Runtime Character Engine

读取 LLM1 生成的角色，扮演该角色，处理用户语音（STT）和 Home Assistant 通知，并输出
扬声器文本、情绪、Home Assistant 调用。
Reads the character produced by LLM1, role-plays it, handles user speech (STT) and Home
Assistant notifications, and outputs speaker text, an emotion, and Home Assistant calls.

情绪采用 Russell 环形模型：效价 × 唤醒度，各为 -1..1（`affect.py`）。
Emotion follows Russell's circumplex model: valence × arousal, each -1..1 (`affect.py`);
a label (happy, excited, tense, sad, sleepy, calm, neutral) is derived
from the point's angle. Russell, J. A. (1980). A circumplex model of affect. *Journal of
Personality and Social Psychology, 39*(6), 1161–1178.

## 数据流 / Data flow

```
NFC tag ──► set_active_tag ──► SQLite (LLM1/data/characters.db)
                                   │ profile_json
STT / HA notification ──► prompt_builder ──► OpenAI (strict JSON) ──► LLM2Output
                                                                         │
                          ┌──────────────────────────────────────────────┤
                          ▼                      ▼                       ▼
                    Speaker(TTS)           Face(screen/servo)     HomeAssistantClient
                    [硬件预留/stub]          [硬件预留/stub]         [真实 REST / real REST]
```

## 文件 / Files

| 文件 / File | 作用 / Purpose |
|---|---|
| `settings.py` | 环境变量配置 / env-based settings |
| `schemas.py` | 输入输出结构与 JSON Schema / I/O models and JSON Schema |
| `affect.py` | Russell 环形情绪模型 / Russell circumplex emotion model |
| `character_store.py` | 按 `nfc_tag_id` 读库（只读）/ read-only DB lookup |
| `prompt_builder.py` | 角色 JSON → System Prompt / character JSON → system prompt |
| `adapters.py` | 扬声器、屏幕舵机接口 + HA REST 客户端 / speaker, face interfaces + HA REST client |
| `pipeline.py` | 常驻流水线 / long-lived pipeline |
| `main.py` | 终端测试入口 / terminal test entry |
| `server.py` | HTTP 服务入口 / HTTP server entry |
| `smoke_test.py` | 无硬件全链路冒烟测试 / hardware-free end-to-end smoke test |
| `ha_entities.json` | 机器人可控设备清单（按你家修改）/ controllable devices (edit for your home) |

## 快速开始 / Quick start

```bash
# 1. 写入预设角色（只需一次）/ seed the preset characters (once)
cd LLM1 && uv run seed_characters.py && cd ..

# 2. 配置 / configure
cp LLM2/.env.example LLM2/.env     # 填入 OPENAI_API_KEY / fill in OPENAI_API_KEY

# 3a. 终端测试 / terminal test
uv run --env-file LLM2/.env LLM2/main.py
#   > /tag marvin-demo-01
#   > Turn on the bedroom light
#   > /ha Time to take your medicine

# 3b. HTTP 服务 / HTTP server  (docs: http://localhost:8000/docs)
uv run --env-file LLM2/.env LLM2/server.py
```

## 无硬件全链路测试 / Hardware-free end-to-end test

```bash
# 终端 A / Terminal A
uv run --env-file LLM2/.env LLM2/server.py
# 终端 B / Terminal B
uv run LLM2/smoke_test.py --token change-me --tag marvin-demo-01
```

测试覆盖 / Covers: 健康检查 health → 刷卡 NFC → 未知标签 404 → 语音 STT → HA 通知 notification。
默认 `HA_TOKEN` 为空即 dry-run，HA 调用只打印；扬声器和屏幕由控制台实现代替。
With `HA_TOKEN` empty the pipeline runs in dry-run: HA calls are printed, and the speaker and
face are console stand-ins.

## HTTP 接口 / HTTP API

| 方法 / Method | 路径 / Path | 请求体 / Body | 说明 / Description |
|---|---|---|---|
| POST | `/nfc` | `{"nfc_tag_id": "..."}` | 刷卡切换角色 / switch character |
| POST | `/stt` | `{"text": "..."}` | 用户语音 / user speech |
| POST | `/ha-notification` | `{"message": "...", "title": ""}` | HA 通知 / HA notification |
| GET | `/health` | — | 状态 / status |

若设置了 `PEBBLE_API_TOKEN`，请求需带 `X-Pebble-Token` 头。
If `PEBBLE_API_TOKEN` is set, requests must carry the `X-Pebble-Token` header.

## 连接 Home Assistant / Connecting Home Assistant

**机器人 → HA（控制设备）/ Robot → HA (device control):**
在 HA 个人资料页创建「长期访问令牌」，填入 `HA_TOKEN` 和 `HA_BASE_URL`。
Create a Long-Lived Access Token in your HA profile and set `HA_TOKEN` and `HA_BASE_URL`.
未填写时为 dry-run（只打印）。/ Without them it runs in dry-run mode (log only).

**HA → 机器人（提醒/消息）/ HA → Robot (reminders/messages):**
在 HA 的 `configuration.yaml` 加入 / add to HA's `configuration.yaml`:

```yaml
rest_command:
  pebble_notify:
    url: "http://<机器人IP / robot-ip>:8000/ha-notification"
    method: POST
    headers:
      Content-Type: application/json
      X-Pebble-Token: "change-me"
    payload: '{"message": "{{ message }}"}'
```

然后在自动化里调用 `rest_command.pebble_notify`。
Then call `rest_command.pebble_notify` from any automation.

## 安全 / Safety

- LLM 只能调用 `HA_ALLOWED_DOMAINS` 白名单内的 domain（默认不含门锁、报警器）。
  The LLM can only call domains in `HA_ALLOWED_DOMAINS` (locks and alarms excluded by default).
- 若存在 `ha_entities.json`，LLM 只能控制清单内的实体。
  If `ha_entities.json` exists, only listed entities can be controlled.
- LLM2 以只读方式打开数据库，不会修改 LLM1 的数据。
  LLM2 opens the database read-only and never modifies LLM1's data.
- 不要提交 `LLM2/.env`（含密钥）。
  Never commit `LLM2/.env` (it contains secrets).

## 接入真实硬件 / Plugging in real hardware

在 `adapters.py` 继承 `Speaker` / `Face`，实现一个方法，然后传给 `LLM2Pipeline`：
Subclass `Speaker` / `Face` in `adapters.py`, implement one method, then pass it to `LLM2Pipeline`:

```python
pipe = LLM2Pipeline(settings, speaker=MySpeaker(), face=MyServoFace())
```

流水线其余部分无需改动。/ Nothing else in the pipeline needs to change.

## 投入实际使用前需要修改或增加的内容 / Changes needed before real-world use

| # | 事项 / Item | 说明 / Details |
|---|---|---|
| 1 | 填写 `.env` / Fill in `.env` | 必填 `OPENAI_API_KEY`；真机部署时填 `HA_BASE_URL`、`HA_TOKEN`、`PEBBLE_API_TOKEN`。 / `OPENAI_API_KEY` is required; set `HA_BASE_URL`, `HA_TOKEN`, `PEBBLE_API_TOKEN` for a real deployment. |
| 2 | 修改 `ha_entities.json` / Edit `ha_entities.json` | 换成你家真实的 `entity_id` 和描述，否则模型不知道有哪些设备。 / Replace with your real `entity_id`s and descriptions, otherwise the model does not know your devices. |
| 3 | 生成角色数据库 / Create the character DB | `data/characters.db` 未提交到仓库，需先运行 `seed_characters.py`；新角色用 `character_generator.py`。 / `data/characters.db` is not committed; run `seed_characters.py` first, and use `character_generator.py` for new characters. |
| 4 | NFC 读卡驱动 / NFC reader driver | 读到标签后 `POST /nfc {"nfc_tag_id": ...}`。本目录未包含。 / After a scan, `POST /nfc {"nfc_tag_id": ...}`. Not included here. |
| 5 | STT 驱动 / STT driver | 识别出文字后 `POST /stt {"text": ...}`。本目录未包含。 / After recognition, `POST /stt {"text": ...}`. Not included here. |
| 6 | TTS 与扬声器 / TTS and speaker | 实现 `Speaker.speak()` 替换 `ConsoleSpeaker`。 / Implement `Speaker.speak()` to replace `ConsoleSpeaker`. |
| 7 | 屏幕与舵机 / Screen and servos | 实现 `Face.show_emotion(affect)` 替换 `ConsoleFace`，把效价/唤醒度映射到屏幕和舵机。 / Implement `Face.show_emotion(affect)` to replace `ConsoleFace`, mapping valence/arousal to the screens and servos (see `digital_twin/brain/llm2_brain.py`). |
| 8 | HA 通知回路 / HA notification path | 按上文 `rest_command` 配置，并确认机器人 IP 对 HA 可达。 / Configure `rest_command` as above and make sure the robot IP is reachable from HA. |
| 9 | 网络安全 / Network security | 设置 `PEBBLE_API_TOKEN`；服务只在局域网暴露，不要映射到公网；必要时 `SERVER_HOST=127.0.0.1`。 / Set `PEBBLE_API_TOKEN`; keep the server on the LAN only; use `SERVER_HOST=127.0.0.1` if all clients are local. |
| 10 | 进程常驻 / Run as a service | 用 systemd 或 Docker 让 `server.py` 开机自启、崩溃重启。 / Use systemd or Docker so `server.py` starts on boot and restarts on crash. |
| 11 | 延迟优化 / Latency | 当前为非流式调用；若语音交互感觉慢，可换更快的模型或改为流式输出。 / Calls are non-streaming; if voice interaction feels slow, use a faster model or switch to streaming. |
| 12 | 调参 / Tuning | 按需调整 `LLM2_MODEL`、`HISTORY_TURNS`、`HA_ALLOWED_DOMAINS`。 / Adjust `LLM2_MODEL`, `HISTORY_TURNS`, `HA_ALLOWED_DOMAINS` as needed. |
| 13 | 自动化测试 / Automated tests | 目前只有冒烟测试，没有单元测试；建议为 `character_store` 和 `HomeAssistantClient` 的白名单逻辑补充。 / Only a smoke test exists, no unit tests; add some for `character_store` and the `HomeAssistantClient` allowlist logic. |
| 14 | 当前角色持久化 / Persist the active character | 重启后需重新刷卡，或设置 `DEFAULT_NFC_TAG_ID`。 / After a restart the user must scan again, or set `DEFAULT_NFC_TAG_ID`. |

## 已知限制 / Known limitations

- 这些代码尚未在真实 OpenAI 密钥和 HA 实例上运行过，首次运行请先看终端输出并按需调整。
  This code has not been run against a real OpenAI key or HA instance; check the output on first run and adjust as needed.
- 刷卡读取、STT、TTS 驱动不在本模块内。
  NFC reading, STT and TTS drivers are not part of this module.
