# `danno.workspace.toml` — Workspace-Level Configuration Inheritance

## What it is

`danno.workspace.toml` is a configuration-inheritance mechanism that lets a group
of projects share one `[sandbox]` block from a common parent directory. It is
**not** a replacement for `danno.toml` — it supplements it, so you can avoid
copying the same `[sandbox]` settings into every project.

Only the `[sandbox]` section is inherited. Nothing else (`[backends]`, `[models]`,
`[agents]`, …) is read from a workspace file.

## How inheritance resolves

When danno needs the `[sandbox]` settings (notably `agent_home`) for a target
directory, it resolves them in this order:

1. **The target's own `danno.toml`.** If `<target>/danno.toml` has a `[sandbox]`
   section, danno uses it and stops. A relative `agent_home` resolves against the
   target directory.
2. **The nearest `danno.workspace.toml`.** Otherwise danno walks **up** the
   directory tree from the target and uses the `[sandbox]` from the first
   `danno.workspace.toml` it finds. A relative `agent_home` then resolves against
   **that workspace file's directory**, not the target.
3. **Built-in defaults.** If neither is found, danno uses its defaults
   (`agent_home = "per-project"`).

A project's own `[sandbox]` always takes precedence over an inherited one — the
workspace file only fills in for projects that declare none.

### Path resolution

A relative `agent_home` inherited from a workspace file resolves against the
directory that holds the workspace file. For example:

```text
~/work/acme/
├── danno.workspace.toml   ← [sandbox] agent_home = ".shared/agent-home"
├── main/                  (worktree, no [sandbox])
└── login/                 (worktree, no [sandbox])
```

Both `main/` and `login/` inherit `.shared/agent-home`, which resolves to
`~/work/acme/.shared/agent-home` — relative to the workspace directory, so every
child shares one home.

## Use cases

### 1. Worktrees of one repo — `per-repo`

Git worktrees share one common `.git` directory, so `per-repo` keys the agent home
on that shared directory:

```toml
[sandbox]
agent_home = "per-repo"
```

Result: the `acme`, `acme-login`, and `acme-billing` worktrees all share one home,
isolated from other repos.

### 2. Named groups — `group:<name>`

For unrelated projects that should share a home, give them the same group name:

```toml
# in each project's danno.toml
[sandbox]
agent_home = "group:my-project-family"
```

Every project with that name resolves to the same group home.

### 3. Explicit shared path

```toml
[sandbox]
agent_home = "~/shared-agent-home"
```

### 4. Workspace file for DRY configuration

Instead of repeating `[sandbox]` in every project, drop one `danno.workspace.toml`
at the common parent:

```text
~/acme-corp/
├── danno.workspace.toml   ← [sandbox] agent_home = ".agent-home"
├── service-a/
│   └── danno.toml         (no [sandbox]; inherits the parent)
└── service-b/
    └── danno.toml         (inherits too)
```

## What it does not do

### Not a config-merging mechanism

Only `[sandbox]` is inherited. Every other section (`[backends]`, `[models]`,
`[agents]`, …) must be declared in each project's own `danno.toml`.

### Not multi-level inheritance

Only the **nearest** workspace file is used; workspace files do not chain. The
nearest ancestor (or the target itself) wins, and any workspace file further up is
ignored:

```text
~/acme/
├── danno.workspace.toml     ← ignored (a nearer one exists)
└── service-a/
    ├── danno.workspace.toml ← this one wins (nearest)
    └── danno.toml           (no [sandbox])
```

### Not a standalone config

A workspace file is read only while resolving a target's `[sandbox]`; it is not
validated or loaded on its own.

## Example `danno.workspace.toml`

```toml
# shared sandbox config for a project family
[sandbox]
agent_home = ".shared/agent-home"
```

Or with a group name:

```toml
[sandbox]
agent_home = "group:acme-corp-services"
```

## When to use it

**Use `danno.workspace.toml` when:**

- Multiple projects or worktrees should share sandbox settings.
- You want to avoid duplicating `[sandbox]` across many `danno.toml` files.
- Your projects sit under a natural common parent directory.

**Prefer per-project config when:**

- Each project needs different `agent_home` behavior.
- Projects sit at the same level with no shared parent.
- You want the configuration explicit and visible in each project.
