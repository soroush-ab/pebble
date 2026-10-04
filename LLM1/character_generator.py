# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "llama-cpp-python"]
# ///
"""LLM1 — Character Generator. Concept text -> validated OCEAN profile JSON -> SQLite.

See README.md for setup, usage, and platform-specific notes.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

MODELS_DIR = Path(__file__).parent / "models"
DB_PATH = Path(__file__).parent / "data" / "characters.db"


def find_default_model(models_dir: Path) -> Path:
    candidates = sorted(models_dir.glob("*.gguf"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"No .gguf file in {models_dir}. See README.md setup.")
    raise FileNotFoundError(
        f"Multiple .gguf files in {models_dir}, pass --model-path explicitly: {candidates}"
    )


Level = Literal["Low", "Medium", "High"]


class Traits(BaseModel):
    openness: Level
    conscientiousness: Level
    extraversion: Level
    agreeableness: Level
    neuroticism: Level


class Personality(BaseModel):
    model: str = "Big Five (OCEAN)"
    traits: Traits


class DialogueExample(BaseModel):
    trigger: str
    response: str


class CharacterProfile(BaseModel):
    character_id: str
    nfc_tag_id: str
    display_name: str
    archetype: str
    personality: Personality
    tone: str
    behaviour_rules: list[str]
    example_dialogue: list[DialogueExample]
    design_notes: Optional[str] = None


SYSTEM_PROMPT = """You are the Character Generator for a customizable home-assistance robot.
Given a short character concept, output ONE JSON object matching the required schema.

Design rule: keep Openness, Conscientiousness, Agreeableness High and Neuroticism Low by
default. Extraversion is the primary differentiating trait: set it according to how
outgoing/expressive vs. reserved/understated the requested character should be. Only deviate
from the O/C/A/N defaults if the concept explicitly calls for it, and note the deviation in
design_notes.

Extraversion and Agreeableness are different axes: extraversion is sociability/energy/
talkativeness, agreeableness is warmth/friendliness. A highly extraverted character can still be
cold or blunt; a reserved, low-extraversion character can still be warm. Judge each
independently — don't infer one from the other.

behaviour_rules: 3-5 short strings. example_dialogue: 1-2 {trigger, response} items."""

# Grammar is built from the full Pydantic schema, not a generic "valid JSON" grammar — slower
# (~12 tok/s vs ~90 tok/s) but reliable. A generic grammar lets the model drift off-schema (e.g.
# a 1-10 trait scale instead of Low/Medium/High) with nothing to catch it until all retries are
# spent. Worth it: this runs once per character, not per conversation turn.


def load_model(model_path: Path, n_ctx: int, chat_format: str | None, n_gpu_layers: int):
    from llama_cpp import Llama

    return Llama(
        model_path=str(model_path),
        n_ctx=n_ctx,
        chat_format=chat_format,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
    )


def call_llm(llm, concept: str, nfc_tag_id: str, attempts: int = 3) -> CharacterProfile:
    from llama_cpp import LlamaGrammar

    grammar = LlamaGrammar.from_json_schema(json.dumps(CharacterProfile.model_json_schema()))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f'Character concept: "{concept}"\nnfc_tag_id: {nfc_tag_id}'},
    ]
    last_error: Exception | None = None
    for _ in range(attempts):
        result = llm.create_chat_completion(
            messages=messages, grammar=grammar, temperature=0.3, max_tokens=800
        )
        raw = result["choices"][0]["message"]["content"]
        try:
            return CharacterProfile.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = e
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": f"That JSON was invalid: {e}. Fix it and output the corrected JSON object only."}
            )
    raise RuntimeError(f"LLM did not produce valid schema after {attempts} attempts: {last_error}")


def write_to_db(profile: CharacterProfile, db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS characters (
            nfc_tag_id TEXT PRIMARY KEY,
            character_id TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        )"""
    )
    existing = conn.execute(
        "SELECT version FROM characters WHERE nfc_tag_id = ?", (profile.nfc_tag_id,)
    ).fetchone()
    version = (existing[0] + 1) if existing else 1
    conn.execute(
        """INSERT INTO characters (nfc_tag_id, character_id, profile_json, created_at, version)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(nfc_tag_id) DO UPDATE SET
             character_id=excluded.character_id,
             profile_json=excluded.profile_json,
             created_at=excluded.created_at,
             version=excluded.version""",
        (
            profile.nfc_tag_id,
            profile.character_id,
            profile.model_dump_json(),
            datetime.now(timezone.utc).isoformat(),
            version,
        ),
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and store a character personality profile.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--concept", help="Character concept, typed directly")
    group.add_argument("--concept-file", type=Path, help="Path to a text file with a pasted character description")
    parser.add_argument("--nfc-tag-id", required=True)
    parser.add_argument("--model-path", type=Path, default=None, help="Defaults to the single .gguf file in models/")
    parser.add_argument("--chat-format", default=None, help="Override if llama.cpp can't auto-detect the GGUF's chat template")
    parser.add_argument("--n-ctx", type=int, default=4096)
    parser.add_argument(
        "--n-gpu-layers",
        type=int,
        default=-1,
        help="-1 = offload all layers to GPU if a GPU backend was compiled in, 0 = force pure CPU",
    )
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    concept = args.concept or args.concept_file.read_text().strip()
    model_path = args.model_path or find_default_model(MODELS_DIR)
    llm = load_model(model_path, args.n_ctx, args.chat_format, args.n_gpu_layers)
    profile = call_llm(llm, concept, args.nfc_tag_id)
    write_to_db(profile, args.db)
    print(profile.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
