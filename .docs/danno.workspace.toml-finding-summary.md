# `danno.workspace.toml` — Investigation Summary

## Executive Summary

- **Status:** Feature is implemented and functional
- **Documentation:** Partial (exists in code but missing examples/guides)
- **Consistency:** No contradictions found between docs/code
- **Risk Assessment:** Low - feature is well-contained and tested in implementation

## Findings

### 1. Implementation Status: COMPLETE

The `danno.workspace.toml` feature is **fully implemented** in `src/book_em_danno/commands/sandbox.py`:

- `_find_workspace()` (sandbox.py:173): walks up directory tree to find workspace file
- `_read_sandbox()` (sandbox.py:152): parses and validates `[sandbox]` section only
- `resolve_home()` (sandbox.py:189): resolves agent_home with inheritance logic

**Key behaviors verified:**
- Searches parent directories for nearest danno.workspace.toml
- Inherits only [sandbox] section (ignores other config)
- Relative paths resolve against workspace file's directory
- Falls back to defaults if no workspace/config found
- Fails loud on invalid TOML or schema validation errors

### 2. Documentation Findings

#### README.md
**Status:** Mentions feature conceptually with example layout

Gaps: No actual danno.workspace.toml example file provided

#### docs/ux-requirements.md
**Status:** Technical spec mentions workspace inheritance

No contradictions found with implementation.

#### .docs/sandbox-agents.md
**Status:** References agent_home but no mention of workspace.toml

This is a documentation gap (not a bug) - the sandbox agents doc should reference workspace inheritance for completeness.

### 3. Missing Artifacts

| Artifact | Status | Impact |
|----------|--------|--------|
| Example danno.workspace.toml file | Not present | Users may not discover feature |

**Workspace inheritance is tested** in `tests/test_sandbox.py`
(`test_resolve_home_inherits_workspace_with_relative_path`,
`test_resolve_agent_home_relative_against_workspace_dir`,
`test_resolve_home_reads_own_sandbox_section`,
`test_resolve_home_defaults_to_per_project`, plus the misplaced-cwd-hint,
in-repo-footgun, and malformed-config-fails-loud cases).

**Impact assessment:** Low - feature is self-contained, implementation is solid and
covered by tests. Missing examples only affect discoverability.

### 4. Code Documentation

The code at sandbox.py has excellent inline documentation that matches implementation exactly.

### 5. No Conflicts Found

Checked for inconsistencies across:
- README.md
- docs/ux-requirements.md  
- .docs/*.md (all user-facing docs)
- src/book_em_danno/commands/sandbox.py (implementation)
- .specify/memory/parts/*.md (development guidance)

**Result:** No contradictions between any documents.

## Conclusion

The `danno.workspace.toml` feature is:
- Well-implemented and production-ready
- Consistently documented across codebase (no contradictions)
- Under-documented for user discoverability (missing examples)

## Documents Created

1. **docs/danno.workspace.toml-explanation.md**
   - Publication-ready explanation of how workspace inheritance works
   - Resolution order, path resolution, and limitations (described behaviorally,
     no code/line citations so it does not rot on refactors)
   - Use cases and examples