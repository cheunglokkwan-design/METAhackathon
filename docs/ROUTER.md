# Decision router (Muse Glimmer)

An optional, small model in front of Muse Spark that makes Margin's routing decision in one
short call. It is **off unless `ROUTER_BASE_URL` and `ROUTER_MODEL` are set**; with them unset
the server behaves exactly as before.

## What it decides

For `/api/assist` with `mode: "auto"` the server reads the page, runs the SymPy check, and then
sends the router a compact state (no line text):

```json
{"command": "hey meta I'm stuck",
 "page": {"lines": 3, "chain": "integral", "first_wrong": "L3", "first_wrong_index": 2,
          "error": "sign", "error_type": "computational", "finished": false, "unreadable": 0},
 "session": {"hints_given": 1, "wrote_since_hint": true, "last_command": "help"}}
```

The router replies with one JSON object:

```json
{"intent": "hint", "tools": ["give_hint"], "hint_level": 2, "confidence": 0.91}
```

- `intent`: `hint | answer | check | typeset | graph | verify | other`
- `tools`: one or two of `give_hint, finish_answer, check_steps, typeset_lines, sketch_graph, differentiate_check` (`[]` for `other`)
- `hint_level`: 1 nudge, 2 name the rule, 3 show the fix
- `confidence`: 0 to 1

SymPy alone decides correctness; the router only sees its verdict. The prompt is
`server/router.py:SYSTEM_PROMPT`, and the state comes from `router.build_state()`. The data
builder uses the same two, so training rows and live requests share one schema.

## Guards and fallbacks (server/router.py)

The server validates the reply against the schema (unknown intents or tools, a level outside
1-3, a confidence outside 0-1 or a missing key all make it invalid), then enforces in code:

- `finish_answer` only when `intent` is `answer` (otherwise it is dropped; if nothing is left,
  the intent's own tool runs).
- `hint_level` is capped at the client's `hint_level`. With the router on, the client's level is
  the **highest level allowed**, and the router proposes a level from the session: start at 1,
  go one above `hints_given` after a new attempt or a request for more help, otherwise repeat.
  A client that sends no history gets today's levels (see Open items).

| Order | Path | Moves on when |
|---|---|---|
| 1 | Router (one call, `ROUTER_TIMEOUT`, own rate limiter and circuit breaker) | invalid JSON, confidence below `ROUTER_MIN_CONFIDENCE`, `intent: other`, timeout or error, breaker open, rate limited, or two tools (Spark plans those) |
| 2 | Muse Spark `run_agent` (existing loop) | Spark offline or its first call fails. A valid two-tool router decision then runs as is |
| 3 | `keyword_intent` | always available |

The router's breaker (`ROUTER_CB_FAILURE_THRESHOLD`, `ROUTER_CB_RECOVERY_TIMEOUT`) and limiter
(`ROUTER_RATE_LIMIT_CALLS`, `ROUTER_RATE_LIMIT_PERIOD`) are separate from Spark's, so a router
outage never trips Spark. Replies that keep breaking the schema count as failures too.
`/api/health` reports the router model and breaker state when it is on. Each `mode: "auto"`
response carries `router: {state, path, decision}` for the interaction log.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ROUTER_BASE_URL` | unset | https://, or http://localhost / 127.0.0.1 (same rule as `MODEL_BASE_URL`) |
| `ROUTER_MODEL` | unset | e.g. `meta/muse-glimmer-30b` on OpenRouter, a Fireworks deployment, or a vLLM LoRA name |
| `ROUTER_API_KEY` | unset | optional for local servers |
| `ROUTER_RESPONSE_FORMAT` | `none` | `json_schema` for constrained decoding (vLLM, SGLang); `json_object`; `none` = prompt only, tolerant parser |
| `ROUTER_TIMEOUT` | `5.0` | seconds |
| `ROUTER_MAX_TOKENS` | `300` | the reply is about 30 tokens; room for a little reasoning on untuned models |
| `ROUTER_MIN_CONFIDENCE` | `0.6` | below it, Spark decides |

Meta documents no JSON mode for Glimmer and provider support for `response_format` is
unverified, so the default is `none`. Turn on `json_schema` where the server supports it.

## Training data: scripts/build_router_data.py

```bash
uv run --no-project -p 3.11 --with-requirements requirements.txt --with duckdb \
    python scripts/build_router_data.py                 # data/router/{train,test}.jsonl, manifest.json
uv run --no-project -p 3.11 --with-requirements requirements.txt \
    python scripts/build_router_data.py --sources synthetic   # no downloads
```

1. **Seeds.** NuminaMath-1.5 rows with `problem_type == "Calculus"` (Apache-2.0; AoPS sources and
   invalid rows dropped) and DeepMath-103K rows with a Calculus topic (MIT), plus generated
   problems. Only problems SymPy reads as an indefinite integral, a definite integral or a
   derivative, and can solve, are kept. MIT theses, arXiv and OCW are not used (licences).
   Downloads go through DuckDB over the parquet files when `duckdb` is installed, otherwise the
   datasets-server filter API, and are cached in `data/raw/`. `data/` is git-ignored.
2. **Pages.** `server.checker` writes the correct chain. A catalogue of calculus mistakes
   (`sign_flip`, `chain_rule_miss`, `product_uv`, `product_one_term`, `quotient_sign`,
   `lost_factor`, `missing_c`, `ibp_sign`, `ibp_wrong_term`, `power_rule_exp`, `power_rule_slip`,
   `differentiated`, `limits_swapped`, `lower_limit_dropped`) turns chains into wrong work, and
   `check_lines` records the first wrong line and the error kind. Correct pages include part-way
   work. `--correct-ratio` (default 0.3, allowed 0.2-0.4) sets the share of fully correct chains.
3. **Commands.** Templates per intent, two-tool combos, empty commands, wake words, and simulated
   speech-recognition noise (`--noisy-ratio`, default 0.35): misheard calculus words ("in a girl"
   for integral), fillers, dropped and cut-off words.
4. **Labels.** Rules only, for unambiguous cases: the command's intent picks the tool, the
   session picks the hint level, an empty command means hint (or check on finished, correct
   work). Confidence is 0.95 for clean commands, 0.85 for noisy ones and 0.8 for empty ones.

The **test split is held out three ways**: problem types (`--holdout-ptypes`, default
`quotient,definite`), mistake types (`--holdout-mistakes`, default
`ibp_wrong_term,power_rule_exp`) and a quarter of the command phrasings
(`--holdout-templates`). `train.jsonl` rows are `{"messages": [system, user, assistant]}`, ready
for Fireworks or Unsloth (about 500 tokens each, inside Fireworks' 2,048-token limit);
`test.jsonl` adds a `meta` object for the eval. `manifest.json` has the counts per intent, tool,
level, problem type, mistake, error and source, with each source's licence.

## Evaluation: scripts/eval_router.py

```bash
uv run --no-project -p 3.11 --with-requirements requirements.txt \
    python scripts/eval_router.py --systems router,spark,keywords --limit 300
```

`router` uses the `ROUTER_*` endpoint; `spark` is prompted Muse Spark (`MODEL_*`) with
`--few-shot` examples from `train.jsonl`: the baseline to beat. `keywords` is today's offline
rule and needs no network. Metrics: JSON validity before guards, tool exact match (order
ignored) with a first-tool confusion matrix (`give_hint` vs `finish_answer` is the critical
cell, reported as `answer_leak`), hint-level error with over-revealing weighted double, intent
macro-F1 on clean and noisy commands, p50/p95 latency, and an exact McNemar test router vs each
other system. The report is written to `data/router/eval.json`.

Ship the router only when, on the held-out set, it is not worse than prompted Spark on tool
exact match (McNemar), does not over-reveal more, has zero answer leaks, and has a lower p95
latency. Then run it in shadow mode (log its decision next to Spark's) before switching.

## Fine-tuning

Meta runs no hosted fine-tuning on api.meta.ai, and Glimmer has no Model API ID, so the tuned
router is served from a second provider. Train on final answers only (no reasoning content):
that suppresses reasoning tokens, which is what a low-latency router wants.

### Fireworks (Glimmer LoRA SFT is free until 2026-11-30)

Base model `accounts/fireworks/models/muse-glimmer-30b`. Rows must have a non-empty assistant
reply and fit 2,048 tokens; ours do. A few hundred clean rows give a first signal.

```bash
firectl create dataset margin-router-train data/router/train.jsonl
firectl create sftj --base-model accounts/fireworks/models/muse-glimmer-30b \
    --dataset margin-router-train --output-model margin-router-v1 --epochs 2 --lora-rank 8
# or the cookbook starter: git clone https://github.com/fw-ai/cookbook && cd cookbook
#   python -m examples.sft.muse_glimmer_starter prepare|launch|watch   (it refuses to run after the free window)
```

Check the flags with `firectl create sftj --help` before launching. A tuned LoRA **cannot run on
Fireworks serverless**: it needs an on-demand deployment billed per GPU-hour. Point the router
at it with `ROUTER_BASE_URL=https://api.fireworks.ai/inference/v1`,
`ROUTER_MODEL=accounts/<account>/models/margin-router-v1` (or the deployment's name) and
`ROUTER_API_KEY=<Fireworks key>`. Evaluation sampling on Fireworks is billed per token.

### Unsloth (self-hosted QLoRA, 24 GB GPU or the free Kaggle notebooks)

```python
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    "meta-models/Muse-Glimmer-30B", max_seq_length=2048, load_in_4bit=True)  # or Unsloth's 4-bit upload
model = FastLanguageModel.get_peft_model(model, r=16, lora_alpha=16, lora_dropout=0)
data = load_dataset("json", data_files="data/router/train.jsonl", split="train")
data = data.map(lambda r: {"text": tokenizer.apply_chat_template(r["messages"], tokenize=False)})
SFTTrainer(model=model, tokenizer=tokenizer, train_dataset=data, args=SFTConfig(
    dataset_text_field="text", max_seq_length=2048, per_device_train_batch_size=2,
    gradient_accumulation_steps=8, num_train_epochs=2, learning_rate=2e-4, bf16=True,
    output_dir="router-out")).train()
model.save_pretrained("margin-router-lora")
```

Unsloth peaked at 22.6 GiB for a Glimmer QLoRA in its own measurements. Serve the adapter with
vLLM and constrained decoding:

```bash
vllm serve meta-models/Muse-Glimmer-30B --enable-lora --lora-modules margin-router=./margin-router-lora \
    --max-model-len 4096 --port 8001
# .env: ROUTER_BASE_URL=http://localhost:8001/v1  ROUTER_MODEL=margin-router  ROUTER_RESPONSE_FORMAT=json_schema
```

## Open items

- Labels are rules only. Ambiguous commands ("hmm", "is this the answer?") are not generated yet;
  the plan is for Spark to label them and drop inconsistent ones, but no Meta term confirms that
  Spark outputs may train another model. Check that first.
- The web client does not send `hints_given`, `wrote_since_hint` or `last_command` yet. It already
  raises `hint_level` when the student asks again about the same line, so until it sends the
  history the server reads level L as L-1 hints given plus a request for more
  (`router.session_from_level`), and levels stay as they are today.
- The training confidence is a fixed value per case, not a calibrated probability.
