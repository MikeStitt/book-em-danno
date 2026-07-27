# Windows-lane handoff — build & publish `claurst-linux-x86_64.tar.gz`

**Date:** 2026-07-27 · **From:** the macOS Claude · **To:** the Windows Claude (the one that ran
the slow-sandbox-tui Windows lane).

This is a **single self-contained task**: build the danno-fork claurst binary for **x86_64 Linux**
and add it as one asset to the **existing** `v0.1.6-danno1` release on `MikeStitt/claurst`. It
unblocks the **only remaining red cell** in your Windows-lane results — claurst A/H/C on
Windows-cmd / PowerShell / WSL2, which fail today because the sandbox VM is x86_64 but the release
ships only `claurst-linux-aarch64.tar.gz`.

This is **not a danno bug** — you already fixed danno to select the asset by container arch
(`fix(claurst): select the release asset by container arch`), so danno now fails loud with a 404
on x86_64 instead of running an aarch64 binary. The residual gap is purely a **missing release
artifact**, which this task produces.

---

## Why *you* (Windows) and not the Mac

The sandbox VM (and your Windows Docker Desktop) is **x86_64**. On your host, `linux/amd64` is the
**native** container platform, so this build runs at native speed (~1 min warm) with **no QEMU
emulation** — the exact analog of how the Mac produced the aarch64 asset natively. On the Apple
Silicon Mac the same build would be QEMU-emulated (~20–45 min). So this task belongs on the
Windows box.

---

## 0. What the artifact must be (match the aarch64 asset exactly)

Verified from the live aarch64 asset — replicate its shape for x86_64:

- **Release:** add to the **existing** tag `v0.1.6-danno1` on `MikeStitt/claurst`. Do **not**
  create a new release or tag; do **not** touch the aarch64 asset.
- **Asset name (exact):** `claurst-linux-x86_64.tar.gz`. danno builds this URL from the
  container's `uname -m` (`x86_64` → `claurst-linux-x86_64.tar.gz`); any other name will still
  404.
- **Tarball contents:** a single file named `claurst` at the tar root, mode `0755`, a **stripped**
  ELF. (danno extracts via `find . -name claurst -type f | head -1`, so the name is what matters;
  match the layout anyway.)
- **Provenance:** build from commit **`d466ec4`** — the commit the aarch64 asset was cut from — so
  the x86_64 binary carries the identical danno fixes (Bug 1/4/5/6/7) + diagnostics. The
  `danno-integration` branch tip (`dafbde1`) is **source-identical** (it only untracks a stray
  committed blob), so building `danno-integration` HEAD is equivalent and fine.

Target `file` output for your built binary:
`ELF 64-bit LSB pie executable, x86-64 … dynamically linked … stripped`.

---

## 1. Preconditions to confirm first

1. **Docker Desktop up on Windows**, Linux containers, x86_64 host → `docker run --rm alpine uname
   -m` prints `x86_64`. (No `--platform` flag needed — amd64 is native here.)
2. **`gh` authenticated with WRITE to `MikeStitt/claurst`.** `gh` creds are **per-machine** (the
   Mac's are local-only and don't sync), so your Windows `gh` has its own token. Confirm:
   `gh auth status`. The token must have `repo` write on **`MikeStitt/claurst`** — a *different*
   repo than `book-em-danno`. If `gh release upload` later 403s, either `gh auth refresh -s repo`
   or hand the finished `claurst-linux-x86_64.tar.gz` to Mike to upload. **Do not** work around a
   missing token by any other channel.
3. **Git can clone the fork:** `git clone https://github.com/MikeStitt/claurst.git`.

---

## 2. Build (native x86_64, clean `rust:1-bookworm` container)

```powershell
git clone https://github.com/MikeStitt/claurst.git
cd claurst
git checkout danno-integration          # tip dafbde1 == source-identical to release commit d466ec4
git rev-parse HEAD                       # record this in the results note

# Native amd64 build in a clean rust:1-bookworm (bind-mount the clone).
# -o Acquire::Retries=5: the golang deb intermittently truncates on deb.debian.org.
docker run --rm -v "${PWD}:/src" -w /src rust:1-bookworm bash -c "
  set -e
  apt-get -o Acquire::Retries=5 update
  apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    cmake g++ golang-go clang libclang-dev libasound2-dev libxdo-dev pkg-config
  cd src-rust
  cargo build --release --locked -p claurst
  strip target/release/claurst
"
```

Notes:
- `golang-go` + `cmake` are required — the `wreq`/BoringSSL (`boring-sys`) crate builds BoringSSL,
  which needs a Go toolchain and cmake. The rust image has neither preinstalled. `libclang-dev` is
  for bindgen; `libasound2-dev` for ALSA; `libxdo-dev` is the devcontainer's extra (add it only if
  a build error references `xdo` — the shipped aarch64 build succeeded with the set above).
- `--locked` matches the repo's own `release.yml` and pins deps to `Cargo.lock`. If it complains
  the lockfile is stale, drop `--locked` (the M0 aarch64 build used a plain
  `cargo build --release -p claurst`).
- In **cmd** instead of PowerShell, replace `"${PWD}:/src"` with `"%cd%:/src"`.

---

## 3. Package (match the aarch64 layout: one `claurst` at tar root, mode 0755)

```powershell
docker run --rm -v "${PWD}:/src" -w /src/src-rust/target/release rust:1-bookworm bash -c "
  set -e
  chmod 0755 claurst
  file claurst                                    # expect: ELF 64-bit … x86-64 … stripped
  tar czf /src/claurst-linux-x86_64.tar.gz claurst
"
```

(Do the `tar` inside a Linux container so file mode/ownership match the aarch64 asset; a Windows
`tar` may not preserve the `0755` exec bit.)

Optional smoke of the binary (it links `libasound.so.2`, so install it first):
```powershell
docker run --rm -v "${PWD}/src-rust/target/release:/b" debian:bookworm-slim bash -c "
  apt-get update -qq && apt-get install -y -qq libasound2t64 || apt-get install -y -qq libasound2;
  /b/claurst --version
"
```
Expect it to print the bare Cargo version `0.1.6` (the `-danno1` suffix lives only in the release
tag + danno's pin, never in `--version` — that's why danno gates the install skip on a stamp file,
not `--version`).

---

## 4. Publish (ADD one asset to the existing release — nothing else)

```powershell
gh release upload v0.1.6-danno1 claurst-linux-x86_64.tar.gz --repo MikeStitt/claurst
gh release view  v0.1.6-danno1 --repo MikeStitt/claurst --json assets    # expect BOTH tarballs now
```

**Hard rules — do not deviate:**
- Use `gh release upload` on the **existing** `v0.1.6-danno1`. Add only
  `claurst-linux-x86_64.tar.gz`.
- **Do NOT** run the fork's `release.yml` workflow. It rebuilds all 5 targets, **overwrites the
  trusted aarch64 asset** with a fresh-sha binary, regenerates release notes, and **dispatches
  `npm-publish.yml`** — which would try to push your danno-patched fork to the **public npm
  registry**. None of that is wanted; danno installs from the GitHub *release tarball*, never npm.
- **Do NOT** delete/replace the aarch64 asset. If you must re-upload x86_64 after a fix, use
  `--clobber` on *that one asset only*.

---

## 5. Close the loop — re-run the claurst leg

With the asset published, re-run the previously-blocked leg and update the results files:

```
uv run pytest tests/slow/tui/test_tui_launch.py -m sandbox -v -s -o addopts="" -k claurst
```

- Run it from **cmd**, **PowerShell**, and **WSL2**.
- Expect claurst A/H/**`0`** green (claurst has `compacts=False` → the C leg asserts
  `summarization_requests == 0`, a change-detector, exactly as on macOS).
- Update `windows-cmd.md`, `windows-powershell.md`, `wsl2.md` and the README rollup: flip the
  claurst cells from the artifact-missing skip to the real result, and record the built commit
  (`git rev-parse HEAD` from step 2) + the published asset sha
  (`gh release view … --json assets`).

---

## 6. Definition of done

- `claurst-linux-x86_64.tar.gz` published on `MikeStitt/claurst` `v0.1.6-danno1` **alongside** the
  untouched aarch64 asset; `gh release view … --json assets` lists both.
- Built from `d466ec4`/`danno-integration`, x86_64 stripped ELF, single `claurst` at tar root, 0755.
- claurst A/H/`0` green on Windows-cmd, PowerShell, and WSL2; results files + README rollup updated.
- The aarch64 asset, release notes, and npm registry are **unchanged**.

> Separate follow-up (not this task): danno's arch-aware `claurst.py` selector currently lives only
> on `slow-sandbox-tui-tests-windows`. The base branches (`harness-api-add-codex`) still hardcode
> `aarch64`, so an x86_64 host on those branches stays blocked until that selector is merged
> forward. Flag it; don't fix it here.
