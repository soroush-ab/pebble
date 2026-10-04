# LLM1 — Character Generator

Takes a short character concept (typed by a user, or pasted from a character-description
website) and generates a structured personality profile — Big Five (OCEAN) trait levels, tone,
behaviour rules, example dialogue — stored in a database keyed by NFC tag ID. This is the
"Character Generator" half of the project's two-LLM pipeline; the runtime assistant (LLM2,
separate component) reads a profile back by tag ID and adopts that persona at conversation time.

Inference is local and standalone — a GGUF model via `llama-cpp-python`, no external daemon.

## Quick start

```bash
# 1. put a GGUF model file in models/ (see "Get a model" below)
# 2. seed the 3 known characters — instant, no LLM call
uv run seed_characters.py
# 3. generate a new one
uv run character_generator.py --concept "a cheerful kitchen-helper robot" --nfc-tag-id kitchen-01
```

If step 3 fails to build `llama-cpp-python`, you're missing a C compiler/CMake — see Setup.

## Contents

| Path | Purpose |
|---|---|
| `character_generator.py` | CLI: concept text -> LLM -> validated JSON -> SQLite |
| `seed_characters.py` | Hand-written profiles for Baymax/Marvin/R2-D2 (see Notes) |
| `models/` | Put your GGUF model file here (not committed) |
| `data/characters.db` | SQLite output, one row per `nfc_tag_id` |

## Setup

**Requirements:** Python ≥3.11 + [uv](https://docs.astral.sh/uv/) (dependencies are declared
inline in each script and installed automatically by `uv run`), and a C/C++ compiler + CMake —
`llama-cpp-python` compiles llama.cpp from source the first time it's installed.

The install flow is the same on every OS, only *how you get a compiler* differs:

1. **Get a compiler + CMake.** Linux: your package manager's usual build toolchain (e.g.
   `build-essential`/`base-devel` + `cmake`). macOS: Xcode command line tools
   (`xcode-select --install`). Windows: WSL2 + the Linux instructions (recommended), or Visual
   Studio Build Tools' C++ workload + CMake natively.
2. **(Optional) Enable GPU.** GPU support is a *build-time* choice, set via `CMAKE_ARGS` before
   the first `uv run`:

   | Backend | Set before first run |
   |---|---|
   | CPU only | nothing — this is the default |
   | NVIDIA / CUDA | `CMAKE_ARGS="-DGGML_CUDA=on"` |
   | AMD / ROCm | `CMAKE_ARGS="-DGGML_HIP=on"` |
   | Apple Silicon | nothing — Metal is on by default |

   ```bash
   CMAKE_ARGS="-DGGML_CUDA=on" uv run character_generator.py --concept "..." --nfc-tag-id "..."
   ```

   Already ran once CPU-only and want to switch? Clear the build cache first, or the old wheel
   gets reused: `uv cache clean llama-cpp-python`.
3. **Get a model.** Download a GGUF quant into `models/`, e.g. from
   [unsloth/Ministral-3-3B-Reasoning-2512-GGUF](https://huggingface.co/unsloth/Ministral-3-3B-Reasoning-2512-GGUF):
   `UD-Q4_K_XL` (2.19GB, recommended) · `Q3_K_M` (1.8GB, if RAM/VRAM is tight) ·
   `UD-Q5_K_XL` (2.48GB, more headroom, needs 16GB+ RAM). `character_generator.py` auto-detects
   the model if exactly one `.gguf` file is in `models/`; pass `--model-path` if you keep more
   than one.

> **Only actually run end-to-end on NixOS with an NVIDIA GPU** (see Troubleshooting for the two
> NixOS-specific gotchas hit there). The flow above is the standard, documented
> `llama-cpp-python` build process and should work the same everywhere else, but hasn't been
> verified on Ubuntu/macOS/Windows by this project — if something differs, fix forward and
> update this doc.

## Usage

```bash
uv run seed_characters.py                                              # the 3 known characters
uv run character_generator.py --concept "..." --nfc-tag-id my-tag-01   # a new one
uv run character_generator.py --concept-file desc.txt --nfc-tag-id my-tag-02  # from a pasted file
```

Runtime GPU/CPU switch (independent of the build-time choice above):
`--n-gpu-layers -1` (default, use GPU if available) or `--n-gpu-layers 0` (force CPU).

## JSON schema / example output

```json
{
  "character_id": "baymax_v1",
  "nfc_tag_id": "baymax-demo-01",
  "display_name": "Baymax",
  "archetype": "Introvert / Nurturing Caregiver",
  "personality": {
    "model": "Big Five (OCEAN)",
    "traits": {
      "openness": "High",
      "conscientiousness": "High",
      "extraversion": "Low",
      "agreeableness": "High",
      "neuroticism": "Low"
    }
  },
  "tone": "soft, slow, reassuring",
  "behaviour_rules": [
    "Prioritise user safety/comfort over task-completion speed",
    "Proactively initiate wellbeing check-ins",
    "Use calm, literal, non-judgemental language"
  ],
  "example_dialogue": [
    {
      "trigger": "Turn on the lights, I can't sleep",
      "response": "Of course — I'll dim them low so they're easier on your eyes. Is something keeping you up?"
    },
    {
      "trigger": "A scheduled dose is still untaken after the first reminder",
      "response": "Your medication is overdue. Are you feeling okay? I can remind you again."
    }
  ],
  "design_notes": null
}
```

Every trait is `"Low" | "Medium" | "High"`. `design_notes` is non-null only when a character
deviates from the Openness/Conscientiousness/Agreeableness-high, Neuroticism-low default (see
Marvin in `seed_characters.py`).

DB row (`data/characters.db`, table `characters`): `nfc_tag_id` (primary key), `character_id`,
`profile_json` (the object above), `created_at`, `version` (auto-increments on re-write of the
same `nfc_tag_id`).

## Troubleshooting

**Generation is slow, CPU usage ~800%+ instead of ~100%:** GPU isn't actually being used — check
for `ggml_cuda_init: failed to initialize CUDA: CUDA driver is a stub library` by re-running with
`Llama(..., verbose=True)`.

- **NixOS** (the only platform this was actually hit on): building needs the full CUDA toolkit
  headers, not just `nvcc` on `PATH` — `nix-shell -p gcc cmake ninja cudaPackages.cudatoolkit`.
  At runtime, fix the stub-vs-real-driver linking issue with:
  `LD_LIBRARY_PATH=/run/opengl-driver/lib uv run character_generator.py ...`
- **Other platforms:** this normally shouldn't happen if the GPU driver is installed the regular
  way — if it does, something is shadowing the real driver lib on your linker path.
- Building for every GPU architecture is slow; pin your actual one if rebuilding, e.g.
  `CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=86"` for an Ampere card (RTX 30xx).

**`LLM did not produce valid schema after 3 attempts`:** the concept text is probably ambiguous
about Extraversion vs. Agreeableness — restate it with the intended extraversion level
(talkative/assertive vs. reserved) stated directly, separately from warmth/tone.

## Notes

- **Known characters bypass the LLM.** Baymax/Marvin/R2-D2 have exact target traits from the
  project's OCEAN table — `seed_characters.py` writes their JSON directly. The LLM path
  (`character_generator.py`) is for the actual generative requirement: a user describing a *new*
  character on the spot.
- **Grammar is schema-constrained, not generic-JSON.** Slower (~12 vs. ~90 tokens/sec measured
  here) but reliable — a generic "any valid JSON" grammar was tried and let the model invent a
  1-10 trait scale and drop required fields on a freer-form concept, failing all 3 retries. Worth
  it since this runs once per character, not per conversation turn.
- **Extraversion ≠ warmth.** They're different Big Five axes; a concept described only in
  warmth/tone terms ("cold", "dry") got misread as low-extraversion once. The system prompt now
  states the distinction explicitly.
- **Speed context:** ~12.5 tokens/sec here (schema-constrained) vs. ~95 unconstrained — both on
  an RTX 3060 Laptop (6GB VRAM). A cloud benchmark quoting ~220 tokens/sec for this model is a
  different thing entirely (datacenter GPU, full precision, batched serving), not a sign of
  misconfiguration.

## Status / handoff

- Generator + seed script: working, tested on all 3 known characters plus several ad-hoc
  concepts.
- Pending: confirm this JSON shape with whoever builds LLM2 (runtime assistant) — it should read
  a profile by `nfc_tag_id` and consume this schema as-is.
- For whoever builds LLM2: each run here is "start process, load model, generate, exit" — model
  load dominates the ~15-20s wall time. A per-turn pipeline needs the model resident in one
  long-lived process, not reloaded per call.
