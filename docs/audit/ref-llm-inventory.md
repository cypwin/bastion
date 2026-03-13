# BASTION LLM Inventory — SWARM_BRAIN Client Workloads

> Complete inventory of all local LLM usage patterns, model requirements,
> parallelism rules, Ollama options, load/unload lifecycle, and I/O profiles.
> Generated for BASTION proxy optimization.

---

## 1. Model Inventory

### Active Pipeline Models

| Model | Disk Size | VRAM Est. | Role | Residency |
|-------|-----------|-----------|------|-----------|
| `granite3.1-dense:8b` | 4.9 GB | ~5.2 GB | Council juror #1 | on_demand |
| `llama3.1:8b` | 4.9 GB | ~5.2 GB | Council juror #2 | on_demand |
| `mistral-nemo:12b` | 7.1 GB | ~7.4 GB | Council juror #3 | on_demand |
| `qwen2.5-coder:7b` | 4.7 GB | ~5.0 GB | Backup code juror (conditional) | on_demand |
| `qwen3:30b-a3b-instruct-2507-q4_K_M` | 18.6 GB | ~19.5 GB | Secondary reviewer, primary LLM | on_demand |
| `qwen3:14b` | 9.3 GB | ~9.5 GB | Fast tasks (summarization, research) | on_demand |
| `qwen3:8b` | 5.2 GB | ~5.5 GB | Ultra-fast (HyDE, classification) | on_demand |
| `nuextract` | 2.2 GB | ~2.5 GB | Triplet extraction (template-based) | on_demand |
| `nomic-embed-text` | 0.3 GB | ~0.4 GB | All embeddings (256d Matryoshka) | **always resident** |

### VRAM Coexistence Profiles (32 GB card)

| Profile | Models | Total VRAM | Conflicts With |
|---------|--------|------------|----------------|
| `council` | granite + llama + mistral-nemo (+ optional qwen2.5-coder) | 17.8–22.8 GB | qwen3:30b, qwen3:14b, phi4:14b |
| `primary` | qwen3:30b-a3b-instruct-2507-q4_K_M | ~19.5 GB | council models |
| `fast_batch` | qwen3:14b + qwen3:8b | ~15 GB | qwen3:30b |
| `extraction_pair` | qwen3:30b + nuextract | ~22 GB | council models |

`nomic-embed-text` (0.4 GB) coexists with ALL profiles — always allowed.

---

## 2. Workload Definitions

### Workload A: Memory Jury Council (Primary Quality Gate)

**Source:** `memory-server/jury.py` — `_check_relevance_cpu_council()` and `evaluate_council_batch()`

**Models (parallel):**
- `granite3.1-dense:8b`
- `llama3.1:8b`
- `mistral-nemo:12b`

**Parallelism:** All 3 models infer concurrently via `ThreadPoolExecutor(max_workers=6)`. With `OLLAMA_NUM_PARALLEL=1`, 3 active + 3 queued. Each model processes one request at a time.

**Per-request Ollama options:**
```json
{
  "model": "<council_model>",
  "prompt": "<rubric evaluation prompt — ~500-800 tokens>",
  "system": "<RELEVANCE_SYSTEM_PROMPT or FACTUAL_RELEVANCE_SYSTEM_PROMPT — ~400 tokens>",
  "stream": false,
  "format": "json",
  "keep_alive": "5m",
  "options": {
    "use_mmap": false,
    "temperature": 0.1,
    "num_predict": 200
  }
}
```
- `keep_alive` overridden to `-1` (int) in batch mode (reassess script)
- No `num_ctx` set — uses model default
- No `think` option — none of these are qwen3

**Input:** Memory content (typically 50–300 tokens) + rubric prompt template
**Output:** JSON with verdict, score (1-10), reason, dimension scores (actionability, specificity, novelty on 1-5 scale). ~100-200 tokens output.

**Trigger:** Every `store_memory()` MCP call (interactive), or batch via `reassess_memories.py`

**Volume:**
- Interactive: 1-5 memories per session
- Batch reassess: ~3,400 memories (one-time backlog)

---

### Workload B: Backup Code Juror (Conditional)

**Source:** `memory-server/jury.py` — within council flow

**Model:** `qwen2.5-coder:7b`

**Parallelism:** Sequential (1 request). Only runs after all 3 council models vote.

**Trigger condition:** llama3.1:8b returns `QUARANTINE` AND memory content contains code (function defs, imports, etc.)

**Per-request Ollama options:**
```json
{
  "model": "qwen2.5-coder:7b",
  "prompt": "Rate 1-10 how CRITICAL this insight is. Only give 7+ if it's essential knowledge that would cause major problems if forgotten. Be harsh. Reply with ONLY a number.\n\nMemory:\n<content>",
  "stream": false,
  "keep_alive": "5m",
  "options": {
    "use_mmap": false,
    "temperature": 0.1,
    "num_predict": 20,
    "think": false
  }
}
```

**Input:** Memory content (~50-300 tokens)
**Output:** Single digit 1-10. ~1-5 tokens.

**Volume:** ~10-20% of council evaluations (only when llama quarantines code)

---

### Workload C: Secondary Reviewer (Pass 2)

**Source:** `memory-server/jury.py` — `evaluate_secondary()`, `_check_relevance_with_secondary()`

**Model:** `qwen3:30b-a3b-instruct-2507-q4_K_M`

**Parallelism:** `ThreadPoolExecutor(max_workers=2)` in reassess batch. Sequential in interactive path.

**CRITICAL LIFECYCLE:** Runs AFTER council models are fully unloaded. Council (17.8 GB) and secondary (19.5 GB) cannot coexist in 32 GB VRAM.

**Per-request Ollama options:**
```json
{
  "model": "qwen3:30b-a3b-instruct-2507-q4_K_M",
  "prompt": "<secondary rubric prompt with council context — ~600-1000 tokens>",
  "system": "<RELEVANCE_SYSTEM_PROMPT — ~400 tokens>",
  "stream": false,
  "format": "json",
  "keep_alive": "5m",
  "options": {
    "use_mmap": false,
    "temperature": 0.1,
    "num_predict": 200,
    "think": false
  }
}
```

**Input:** Memory content + council verdict context (~200-500 tokens)
**Output:** JSON verdict (same schema as council). ~100-200 tokens.

**Trigger:** Only for memories that passed council with APPROVE verdict
**Volume:** ~40-60% of total memories (those approved by council)

---

### Workload D: Embeddings

**Source:** `src/embedding_service.py`

**Model:** `nomic-embed-text` (always resident)

**Parallelism:** GPU semaphore limits to 2 concurrent requests. Batch endpoint accepts up to 32 texts per HTTP call.

**Per-request Ollama payload:**
```json
{
  "model": "nomic-embed-text",
  "input": "search_document: <text>"
}
```
Or batch:
```json
{
  "model": "nomic-embed-text",
  "input": ["search_document: <text1>", "search_document: <text2>", ...]
}
```

**Prefix conventions:**
- `search_document:` — for storage/indexing
- `search_query:` — for retrieval queries

**Output:** 768-dim float32 vector (client truncates to 256d Matryoshka + L2 norm)

**Volume:** Every memory store, every retrieval query, batch pre-computation in reassess. High frequency.

---

### Workload E: Pattern Extraction (Pipeline)

**Source:** `src/pipeline/extract_patterns.py` — uses `requests.post()` directly (NOT OllamaClient)

**Model:** `qwen3:30b-a3b-instruct-2507-q4_K_M`

**Parallelism:** Sequential (1 request at a time)

**Direct HTTP payload:**
```json
{
  "model": "qwen3:30b-a3b-instruct-2507-q4_K_M",
  "messages": [{"role": "system", "content": "<extraction prompt>"}, {"role": "user", "content": "<JSONL chunk>"}],
  "stream": false,
  "format": "json",
  "options": {
    "num_ctx": 16384,
    "temperature": 0.0,
    "num_predict": 2048
  }
}
```
Endpoint: `POST http://localhost:11434/api/chat`

**Input:** JSONL transcript chunks (~2000-8000 tokens per chunk)
**Output:** JSON array of extracted findings. ~500-2000 tokens.

**Note:** Uses `requests` library, not `httpx`/`OllamaClient`. Does NOT inject `use_mmap: false` — relies on BASTION to inject it.

---

### Workload F: Fact Extraction (Knowledge Harvest)

**Source:** `src/knowledge_harvest/fact_extractor.py`

**Model:** Resolved via registry (default: `qwen3:30b-a3b-instruct-2507-q4_K_M`)

**Per-request Ollama options:**
```json
{
  "model": "<resolved model>",
  "prompt": "<fact extraction prompt — ~1000-4000 tokens>",
  "system": "<extraction system prompt>",
  "stream": false,
  "format": "json",
  "options": {
    "use_mmap": false,
    "temperature": 0.0,
    "num_predict": 4096,
    "num_ctx": 16384,
    "think": false
  }
}
```

**Input:** Research documents, session transcripts (~2000-12000 tokens)
**Output:** Structured JSON with atomic facts. ~1000-4000 tokens.

---

### Workload G: Triplet Extraction (Knowledge Graph)

**Source:** `src/triplet_extraction.py`

**Model:** Resolved via registry (default: primary or nuextract)

**Two-phase:**
1. **NER phase:** `temperature=0.0, max_tokens=300, format="json"`
2. **Relation phase:** `temperature=0.0, max_tokens=4096, format="json"`

---

### Workload H: Consolidation & Synthesis

**Source:** `src/consolidation.py`, `src/memory_consolidator.py`

**Model:** Primary (`qwen3:30b`) or fast (`qwen3:14b`)

**Per-request:**
```json
{
  "options": {
    "use_mmap": false,
    "temperature": 0.3,
    "num_predict": 300
  }
}
```
No `format`, no `num_ctx` override. Uses model defaults.

---

### Workload I: HyDE Generation

**Source:** `src/hipporag.py`

**Model:** `qwen3:8b` (ultra_fast)

**Per-request:**
```json
{
  "options": {
    "use_mmap": false,
    "temperature": 0.3,
    "num_predict": 300
  }
}
```

**Input:** User query (~20-100 tokens)
**Output:** Hypothetical answer for embedding. ~100-300 tokens.

---

### Workload J: Domain Classification

**Source:** `src/domain_classifier.py`

**Model:** Fast (`qwen3:14b` or passed in)

**Per-request:**
```json
{
  "model": "<model>",
  "prompt": "<session summary>",
  "stream": false,
  "keep_alive": "5m",
  "options": {
    "use_mmap": false,
    "temperature": 0.2,
    "num_predict": 32,
    "think": false
  }
}
```

**Input:** Session summary (~100-500 tokens)
**Output:** Single domain label. ~1-5 tokens.

---

### Workload K: Research Decomposition & Introspection

**Source:** `src/research_processor.py`, `src/introspection.py`

**Model:** Fast (`qwen3:14b`)

**Per-request:**
```json
{
  "options": {
    "use_mmap": false,
    "temperature": 0.3,
    "num_predict": 2048
  },
  "format": "json"
}
```

---

## 3. Load/Unload Lifecycle

### Unload Mechanisms

| Method | Path | Payload | Bypass Scheduler? |
|--------|------|---------|-------------------|
| `OllamaClient.unload_model()` | `POST /api/generate` | `{"model": X, "keep_alive": 0}` | No (scheduler queue, 2s cooldown) |
| `BastionClient.unload()` | `POST /broker/unload` | `{"model": X}` | Yes (admin API, instant) |
| `gpu_guard.unload_ollama()` | Delegates to above, fallback to `curl` | Same | No |

### Load Mechanisms

| Method | How | Details |
|--------|-----|---------|
| Implicit (first request) | Send generate/embed request | Ollama auto-loads if not resident |
| `BastionClient.preload()` | `POST /broker/preload {"model": X}` | Admin API, bypasses scheduler |
| `keep_alive=-1` ping | `generate(prompt="ping", max_tokens=1, keep_alive=-1)` | Forces model to stay resident forever |

### keep_alive Values Used

| Value | Type | Meaning | Where Used |
|-------|------|---------|------------|
| `"5m"` | string | Unload after 5 min idle | Council (interactive), domain classifier |
| `"10m"` | string | Unload after 10 min idle | Corpus analysis (nemotron) |
| `-1` | int | Never unload (resident forever) | Council batch mode, primary pin |
| `0` | int | Unload immediately | `unload_model()` calls |
| Not set | — | Ollama server default | Most generate/embed calls |

### Model Transition Sequences

**Sequence 1: Reassess Pipeline (batch)**
```
1. PRELOAD granite + llama + mistral-nemo (via broker/preload)
2. Set keep_alive = -1 (all council models stay resident)
3. PASS 1: All 3 models evaluate all memories concurrently
   - ThreadPoolExecutor(max_workers=6)
   - 3 active inferences + 3 queued (OLLAMA_NUM_PARALLEL=1)
4. Optional: qwen2.5-coder backup for llama-quarantined code items
5. UNLOAD granite + llama + mistral-nemo + qwen2.5-coder (via broker/unload)
6. Wait for BASTION GPU health
7. PRELOAD qwen3:30b secondary (via broker/preload)
8. PASS 2: Secondary reviews all APPROVE items
   - ThreadPoolExecutor(max_workers=2)
9. UNLOAD qwen3:30b (via broker/unload)
```

**Sequence 2: Interactive Memory Store (MCP)**
```
1. Embeddings via nomic-embed-text (always resident)
2. Static checks (no LLM)
3. Council: 3 models concurrently (ThreadPoolExecutor(max_workers=3))
   - keep_alive="5m" — models linger for next store
4. Optional: backup code juror (sequential)
5. If APPROVE: unload council → sleep(1s) → secondary (qwen3:30b)
6. Unload secondary LLM after verdict
```

**Sequence 3: Pattern Extraction Pipeline**
```
1. Load qwen3:30b (implicit, first request)
2. Sequential extraction over JSONL chunks
   - num_ctx=16384, temperature=0.0
3. No explicit unload (relies on keep_alive timeout)
```

---

## 4. Global Ollama Options (Injected by OllamaClient)

Every `generate()` and `chat()` call through `OllamaClient` merges these base options:

```python
_BASE_RUNNER_OPTIONS = {
    "use_mmap": False,  # RTX 5090 Blackwell crash fix — MANDATORY
}

# Final merged options for every request:
merged_options = {
    "use_mmap": False,       # Always injected
    "temperature": <caller>,  # From caller (0.0-0.3)
    "num_predict": <caller>,  # From caller's max_tokens
    **caller_options,          # Caller can add: think, num_ctx, num_gpu
}
```

**Exception:** `extract_patterns.py` uses `requests.post()` directly — does NOT go through OllamaClient. Relies on BASTION to inject `use_mmap: false`.

### Qwen3 Think Mode Suppression

All qwen3 model calls include `"think": false` in options. Without this, qwen3 consumes all tokens in `<think>` tags, leaving the response field empty.

```python
# Applied conditionally:
if "qwen3" in model.lower():
    options["think"] = False
```

Affected workloads: C (secondary), F (fact extraction), I (HyDE if using qwen3:8b), J (domain classification), K (research/introspection)

---

## 5. Context Size Requirements

| Workload | num_ctx | Input Size | Output Size | Total Context |
|----------|---------|------------|-------------|---------------|
| A: Council jury | default (~4096) | ~800-1200 tok | ~200 tok | ~1400 tok |
| B: Backup juror | default (~4096) | ~300-500 tok | ~5 tok | ~500 tok |
| C: Secondary | default (~4096) | ~1000-1500 tok | ~200 tok | ~1700 tok |
| D: Embeddings | N/A (embed API) | ~50-500 tok | 768-dim vector | N/A |
| E: Pattern extraction | **16384** | ~2000-8000 tok | ~500-2048 tok | ~10000 tok |
| F: Fact extraction | **16384** | ~2000-12000 tok | ~1000-4096 tok | ~16000 tok |
| G: Triplet extraction | default (~4096) | ~500-2000 tok | ~300-4096 tok | ~6000 tok |
| H: Consolidation | default (~4096) | ~500-1500 tok | ~300 tok | ~1800 tok |
| I: HyDE | default (~4096) | ~50-200 tok | ~300 tok | ~500 tok |
| J: Classification | default (~4096) | ~100-500 tok | ~5 tok | ~500 tok |
| K: Research/Introspection | default (~4096) | ~500-2000 tok | ~2048 tok | ~4000 tok |

---

## 6. Concurrency Summary

| Workload | Concurrent Requests | Models Active | Thread Pool |
|----------|-------------------|---------------|-------------|
| A: Council (batch) | 6 workers (3 active per OLLAMA_NUM_PARALLEL=1) | 3 | `ThreadPoolExecutor(max_workers=6)` |
| A: Council (interactive) | 3 workers | 3 | `ThreadPoolExecutor(max_workers=3)` |
| B: Backup juror | 1 | 1 | Sequential |
| C: Secondary (batch) | 2 | 1 | `ThreadPoolExecutor(max_workers=2)` |
| C: Secondary (interactive) | 1 | 1 | Sequential |
| D: Embeddings | 2 (GPU semaphore) | 1 | `BoundedSemaphore(2)` |
| E-K: All others | 1 | 1 | Sequential |

---

## 7. System-Level Ollama Configuration

```bash
# From ollama.service systemd override
OLLAMA_HOST=127.0.0.1:11435           # Raw Ollama (BASTION proxies 11434→11435)
OLLAMA_NUM_PARALLEL=1                  # 1 concurrent request per loaded model
OLLAMA_MAX_LOADED_MODELS=3             # Max 3 models in VRAM simultaneously
OLLAMA_FLASH_ATTENTION=1               # Required for q8_0 KV cache
OLLAMA_KV_CACHE_TYPE=q8_0              # Halves KV cache memory vs fp16
OLLAMA_KEEP_ALIVE=-1                   # Server default: keep models forever
```

**Hardware:** RTX 5090, 32 GB VRAM, 123 GB RAM

---

## 8. Known Issues & Constraints

1. **`use_mmap: false` is MANDATORY** — memory-mapped tensor loading causes RTX 5090 Blackwell GPU crashes. Must be injected on every request.

2. **`extract_patterns.py` bypasses OllamaClient** — uses `requests.post()` directly. BASTION must inject `use_mmap: false` for this path.

3. **`keep_alive="-1"` (string) is a bug** — found in `model_router.py:196`. Ollama expects int `-1` for "forever", string `"-1"` causes 400 Bad Request. Only hits the `restore_after_gpu_batch` path.

4. **Council + secondary cannot coexist** — council models (~18 GB) + qwen3:30b (~19.5 GB) = 37.5 GB, exceeds 32 GB. Must unload council before loading secondary.

5. **GPU semaphore for embeddings** — `BoundedSemaphore(2)` limits embedding concurrency. Paired with `OLLAMA_NUM_PARALLEL=1` this means 1 active + 1 queued.

6. **qwen3:8b + GBNF format bug** — thinking mode (`think: true`) corrupts JSON output. Always pass `think: false` for qwen3 family.
