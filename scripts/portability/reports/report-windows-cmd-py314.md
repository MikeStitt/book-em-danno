# danno portability probe — Windows / cmd

- **when (UTC):** 2026-07-09T20:29:59.275913+00:00
- **platform / arch:** `win32` / `AMD64`  
- **os:** Windows 11
- **python:** 3.14.2 (`C:\projects\try-danno\cmd\book-em-danno\.venv\Scripts\python.exe`)
- **cwd:** `C:\projects\try-danno\cmd\book-em-danno`
- **danno.toml in cwd:** False

## Tooling on PATH

| tool | path |
|---|---|
| danno | C:\projects\try-danno\cmd\book-em-danno\.venv\Scripts\danno.EXE |
| uv | C:\Users\mike\.local\bin\uv.EXE |
| docker | C:\Program Files\Docker\Docker\resources\bin\docker.EXE |
| git | C:\Program Files\Git\cmd\git.EXE |
| bash | C:\WINDOWS\system32\bash.EXE |
| ollama | **MISSING** |

## Surfaces (S0-S2)

| surface | ok | exit | note |
|---|---|---|---|
| S0 danno --help | ✅ | 0 |  |
| S0 danno doctor --help | ✅ | 0 |  |
| S1 danno doctor | ❌ | 1 |  |
| S2 danno install --help | ✅ | 0 |  |

## Run-leg preflight (Tier 1, P3+)

- **docker:** ✅ (exit 0)
- **docker sandbox subcommand:** ❌ (exit 1) — R1/I5 signal
- **ollama http://192.168.1.5:11434/api/tags:** ✅ reachable
  - models: qwen3-coder-next-65k:latest, gpt-qwen3-coder-next:latest, qwen3-coder-next-ud-q4-65k:latest, Qwen3-Coder-Next-GGUF:UD-Q4_K_M, hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_M, qwen3-coder-next:latest, gpt-oss:20b, gemma3:27b, gemma4:26b, qwen3.6:27b-q4_K_M, gemma4:31b-mlx, gemma4:26b-mlx, gemma3:1b, llama3.3:latest, llama3.2:latest, llama2:13b
- **run-leg ready:** ❌ no

## Hazard notes

- H1 MASKED: Git Bash present (C:\WINDOWS\system32\bash.EXE) -> danno's host bash subprocess (tools.py:107) will SUCCEED. This is a cmd+GitBash result, NOT pristine cmd.
- H4: os.chmod(0o600) is a no-op on native Windows -> secret env-files are NOT owner-restricted. Do NOT run cloud-auth benches here until a Windows-ACL path exists.
