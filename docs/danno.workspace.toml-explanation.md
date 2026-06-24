# `danno.workspace.toml` — Workspace-Level Configuration Inheritance

## What it is

`danno.workspace.toml` is a configuration inheritance mechanism that allows projects to share `[sandbox]` settings from a parent directory. It's **not** a replacement for `danno.toml`; rather, it supplements it.

## How it works

### The inheritance chain

When danno needs to resolve `[sandbox] agent_home` for a project at `target_abs`:

1. **First:** Check `<target>/danno.toml` — if it has a `[sandbox]` section, use it
2. **Fallback:** Walk up the directory tree looking for `danno.workspace.toml`
   - If found, inherit its `[sandbox]` section
   - Relative paths in `agent_home` resolve against theworkspace file's directory**
3. **Default:** Use defaults if neither config exists

### Key implementation details (from `sandbox.py:155-209`)

```python
def _find_workspace(target_abs: Path) -> tuple[Path | None, Sandbox | None]:
    """Walk up to the nearest `danno.workspace.toml`; return its dir and `[sandbox]`."""
    for d in [target_abs, *target_abs.parents]:
        if (d / "danno.workspace.toml").is_file():
            return d, _read_sandbox(d / "danno.workspace.toml")
    return None, None
```

The function:
- Searches **upward** from the target directory
- Returns both the workspace **directory path** and the parsed `[sandbox]` block
- Stops at the first `danno.workspace.toml` found (nearest ancestor)

### The `_read_sandbox` helper (`sandbox.py:134-152`)

```python
def _read_sandbox(toml_path: Path) -> Sandbox | None:
    """Parse just the `[sandbox]` block from a danno.toml / danno.workspace.toml."""
    if not toml_path.is_file():
        return None
    # ... parses TOML, validates sandbox section with Pydantic ...
```

- Parses **only** the `[sandbox]` section (ignores other config)
- Validates agent_home against known patterns
- Returns `None` if file missing or no `[sandbox]` section

### Path resolution (`resolve_agent_home`, `sandbox.py:105-131`)

When a relative path is inherited from a workspace file:

```python
if not home.is_absolute():
    home = (relative_base or target_abs) / home  # relative_base is the workspace dir
```

Example:
```
~/work/acme/
├── danno.workspace.toml   ← [sandbox] agent_home = ".shared/agent-home"
├── main/                  (worktree)
└── login/                 (worktree)
```

- `main/danno.toml` → no `[sandbox]` block
- Find workspace at `~/work/acme/danno.workspace.toml`
- Inherited value: `.shared/agent-home`
- Resolved path: `~/work/acme/.shared/agent-home` (relative to workspace dir)

## Use cases

### 1. Worktrees of one repo (`per-repo` - automatic)

Every git worktree shares one git CommonDir, so danno automatically keys agent home on it:

```toml
# In each worktree's danno.toml (or omit [sandbox] entirely):
[sandbox]
agent_home = "per-repo"
```

Result: `acme`, `acme-login`, `acme-billing` all share one home.

### 2. Named groups (`group:<name>`)

For disparate projects that should share a home:

```toml
# ~/project-a/danno.toml:
[sandbox]
agent_home = "group:my-project-family"

# ~/project-b/danno.toml:
[sandbox]
agent_home = "group:my-project-family"
```

Both resolve to `~/.danno/agent-home/groups/my-project-family/`.

### 3. Explicit shared path

```toml
# danno.toml in multiple projects:
[sandbox]
agent_home = "~/shared-agent-home"
```

### 4. Workspace file for DRY configuration

Instead of copying `[sandbox]` into every project:

```
~/acme-corp/
├── danno.workspace.toml      ← [sandbox] agent_home = ".agent-home"
├── service-a/
│   └── danno.toml           ← no [sandbox] needed; inherits parent
└── service-b/
    └── danno.toml           ← inherits too
```

## What it DOESN'T do

### ❌ Not a config merging mechanism

Only `[sandbox]` is inherited. Other sections (`[backends]`, `[models]`, `[agents]`) must be defined in each project's `danno.toml`.

```toml
# danno.workspace.toml:
[sandbox]
agent_home = "per-project"

# service-a/danno.toml:
[backends.danno-ollama]
kind = "ollama"
# ... model/backend configuration MUST be repeated ...
```

### ❌ Not for multi-level inheritance

Only the **nearest** workspace file is used. Nested workspace files don't chain:

```
~/acme/
├── danno.workspace.toml     ← This one wins (nearest)
└── service-a/
    ├── danno.workspace.toml ← Ignored
    └── danno.toml
```

### ❌ Not validated as a standalone config

There's no `danno validate --config danno.workspace.toml` command. The workspace file is only read in the context of resolving `agent_home`.

## Documentation gaps / inconsistencies

### 1. Missing from README.md

The README documents the feature conceptually (lines 657-689) but provides **no example file** and the `danno.toml.example` doesn't include a workspace file.

### 2. Inconsistent with docs/ux-requirements.md

docs/ux-requirements.md mentions it on lines 71 and 133:
> A relative path inherited from a `danno.workspace.toml` resolves against that file's directory.

But the spec doesn't clarify:
- What happens if both workspace file AND project have `[sandbox]` (workspace wins for inheritance, but project overrides with its own)
- Whether workspace files can contain non-sandbox sections
- If workspace inheritance works across filesystem boundaries

### 3. No tests for workspace inheritance

Searching `tests/` reveals no fixtures or test cases Exercises specifically for the workspace inheritance path.

## Example `danno.workspace.toml`

```toml
# danno.workspace.toml - shared sandbox config for a project family
[sandbox]
agent_home = ".shared/agent-home"
```

Or with group naming:

```toml
[sandbox]
agent_home = "group:acme-corp-services"
```

## When to use it

**Use `danno.workspace.toml` when:**
- You have multiple projects/worktrees that should share sandbox settings
- You want to avoid duplicating `[sandbox]` blocks across many `danno.toml` files
- Your project hierarchy has a natural "root" directory

**Prefer per-project config when:**
- Each project needs different `agent_home` behavior
- Projects are at the same tree level (no natural parent)
- You want explicit, visible configuration in each project
