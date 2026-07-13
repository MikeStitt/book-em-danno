# danno portability probe — Linux / wsl

- **when (UTC):** 2026-07-09T20:43:50.376428+00:00
- **platform / arch:** `linux` / `x86_64`  
- **os:** Linux 6.6.87.2-microsoft-standard-WSL2
- **python:** 3.14.3 (`/mnt/c/projects/try-danno/wsl/book-em-danno/.venv/bin/python3`)
- **cwd:** `/mnt/c/projects/try-danno/wsl/book-em-danno`
- **danno.toml in cwd:** False

## Tooling on PATH

| tool | path |
|---|---|
| danno | /mnt/c/projects/try-danno/wsl/book-em-danno/.venv/bin/danno |
| uv | /home/mike/.local/bin/uv |
| docker | /mnt/c/Program Files/Docker/Docker/resources/bin/docker |
| git | /usr/bin/git |
| bash | /usr/bin/bash |
| ollama | **MISSING** |

## Surfaces (S0-S2)

| surface | ok | exit | note |
|---|---|---|---|
| S0 danno --help | ✅ | 0 |  |
| S0 danno doctor --help | ✅ | 0 |  |
| S1 danno doctor | ❌ | 1 |  |
| S2 danno install --help | ✅ | 0 |  |

## Run-leg preflight (Tier 1, P3+)

- **docker:** ❌ (exit 1)
- **docker sandbox subcommand:** ❌ (exit 1) — R1/I5 signal
- **ollama http://192.168.1.5:11434/api/tags:** ✅ reachable
  - models: qwen3-coder-next-65k:latest, gpt-qwen3-coder-next:latest, qwen3-coder-next-ud-q4-65k:latest, Qwen3-Coder-Next-GGUF:UD-Q4_K_M, hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_M, qwen3-coder-next:latest, gpt-oss:20b, gemma3:27b, qwen3.6:27b-q4_K_M, gemma4:26b, gemma4:31b-mlx, gemma4:26b-mlx, gemma3:1b, llama3.3:latest, llama3.2:latest, llama2:13b
- **run-leg ready:** ❌ no
