# install-fstar

[![CI](https://github.com/mtzguido/install-fstar/actions/workflows/ci.yml/badge.svg)](https://github.com/mtzguido/install-fstar/actions/workflows/ci.yml)

Install [F*](https://github.com/FStarLang/FStar) and its bundled Z3 solvers in
GitHub Actions. Select an exact release tag, commit, branch, or nightly date.
F* versions are opaque identifiers; no semantic-version matching is performed.

```yaml
- uses: mtzguido/install-fstar@main
  with:
    version: v2026.10.25
- run: fstar.exe Example.fst
```

This is under development; see the CI results for the currently verified matrix.
Pin the action to a reviewed **full commit SHA** for reproducibility. The action
reference and the compiler's `version` are independent. Checking out your project
is optional for the installer itself.

Other selectors:

```yaml
# Both date spellings select the same nightly from FStarLang/FStar-nightly.
- uses: mtzguido/install-fstar@main
  with:
    version: '2026-10-18' # or nightly-2026-10-18

# Source build pinned to a full F* commit (abbreviations are also resolved).
- uses: mtzguido/install-fstar@main
  with:
    version: 3515a8a94ea4b83cc97eb89dcbac48cab88e8f6e

# Resolve a moving branch before looking up the package cache.
- uses: mtzguido/install-fstar@main
  with:
    version: fstar1
```

## Native builds and plugins

F* plugins must be compiled in the same OCaml environment as the compiler that
loads them. Use `native: true` after setting up the caller's OPAM switch:

```yaml
- uses: ocaml/setup-ocaml@v3
  with:
    ocaml-compiler: '5.4.0'
    opam-pin: false
- uses: mtzguido/install-fstar@main
  with:
    version: '2026-10-18'
    native: true
- run: opam exec -- make
```

Native mode rebuilds releases and nightlies too. It recompiles the selected
release's portable OCaml source package when available, or bootstraps from the
exact Git commit otherwise. It installs the revision's dependencies in the
active OPAM switch, then builds with that compiler and those libraries.
Install your project's dependencies first and use the same switch for plugins.
Changing its compiler or dependency interfaces afterwards requires rebuilding F*.

Native mode ignores `ocaml-version` and always bypasses package cache restore
and save. Matching compiler/package version strings alone does not establish
OCaml interface compatibility. The caller can still cache its OPAM switch.

## Inputs and outputs

| Input | Meaning | Default |
| --- | --- | --- |
| `version` | Exact tag, commit, branch, or nightly date | Required |
| `native` | Recompile in the active OPAM switch; always bypass the package cache | `false` |
| `cache` | Restore/save the finished binary package; ignored in native mode | `true` |
| `ocaml-version` | Exact OCaml compiler for an isolated source build | `5.3.0` |
| `token` | GitHub token for public release/commit metadata | `${{ github.token }}` |

The action adds the installation's `bin` to `PATH` and exports `FSTAR_EXE`.
F* discovers its bundled solvers automatically. Outputs:

| Output | Meaning |
| --- | --- |
| `version` | Selected release/nightly tag or resolved source SHA |
| `commit` | Full source SHA, checked against the installed compiler |
| `path` | Installation root |
| `fstar-exe` | Absolute executable path |
| `method` | `binary` or `source` |
| `cache-hit` | `true` only when the exact package was restored |

Calling the action again selects a separate installation for subsequent steps.
For branch builds shared across jobs, pass the first job's `commit` output to
later jobs to avoid resolving the branch at different times.

## Platforms and prerequisites

The target platform matrix matches F*'s published binaries:

| OS | Architectures | Source-build environment |
| --- | --- | --- |
| Linux | x86-64, ARM64 | Ubuntu for automatic setup; a provisioned OPAM switch for native builds elsewhere |
| macOS | Intel, Apple Silicon | Homebrew and OPAM; GNU build tools installed as needed |
| Windows | x86-64 | MinGW OCaml with Cygwin, as provided by default by `ocaml/setup-ocaml` |

The action needs Python 3.10+ (`python` on PATH) and curl. Use
`actions/setup-python` first on runners lacking a suitable Python. Source builds
also need Git and platform build tools. Automatic builds provision an isolated
OPAM root/switch; their environment does not replace the caller's OPAM settings.
On Ubuntu this installs system dependencies with apt; macOS uses Homebrew;
Windows bootstraps a checksum-pinned official OPAM binary and its Cygwin tools.
Native Windows builds require an already configured Cygwin/MinGW switch.
Windows ARM64 and MSVC/MSYS2 source builds are outside the current matrix.

Binary compatibility and available assets depend on the selected F* release.
Missing nightly releases or platform assets produce an error. Historical
revisions may need an older compiler, and releases without the current package
layout or bundled Z3 solvers are not supported.

## Integrity and caching

Release downloads are verified against GitHub's SHA-256 digest when present;
older releases lacking a digest produce a warning. Every installed compiler
must report the resolved source commit. Native builds must also report the
active OCaml compiler version. Authentication/rate-limit failures are reported
rather than interpreted as missing releases.

Only validated, finished archives are saved. Cache identities include the exact
commit, OS/architecture, installer implementation, release asset identity/digest,
and OS/compiler version for isolated builds. No partial-key restores are used.
`cache: false` skips both restore and save. Concurrent misses may build the same
revision independently; a prerequisite job can warm the cache.

Source builds use `ADMIT=1`, matching F*'s own package builds. They compile the
selected compiler and libraries without rerunning all F* verification proofs.

## Tests

[CI](.github/workflows/ci.yml) runs on pushes, pull requests, manual dispatches,
and weekly to catch runner/toolchain changes. It covers:

- Unit tests on Linux, macOS, Windows, and multiple Python versions.
- Release and nightly binaries on Linux x86-64/ARM64, Intel/Apple Silicon macOS,
  and Windows 2022/2025, with solver-backed F* and Pulse proofs.
- Commit builds and native release/nightly builds on all five OS/architecture
  combinations, with plugin compilation and loading in the caller's OPAM switch.
- Legacy F*1 and an older OCaml compiler, exact cache reuse, cache bypass,
  unchanged caller OPAM settings, platform aliases, bad selectors, checksums,
  wrong compiler/commit detection, and unsafe tar/ZIP entries.

The final `ci` job requires the complete matrix to pass. Each integration job
uploads its proof/plugin test output and platform/version metadata for seven
days. Full matrix execution can take considerably longer than binary installs.

Local checks:

```sh
python -m unittest -v
shellcheck build.sh
# Optional: actionlint .github/workflows/ci.yml
```

## License

Apache-2.0. Initially developed alongside F*'s packaging scripts.
