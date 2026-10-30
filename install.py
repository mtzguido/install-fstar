#!/usr/bin/env python3
"""Implementation of the install-fstar composite action (stdlib only)."""

import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


REPOSITORY = "FStarLang/FStar"
ACTION = Path(__file__).resolve().parent


def api(repository, endpoint, missing_ok=False):
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("INSTALL_FSTAR_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{endpoint}", headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            if error.code < 500 or attempt == 3:
                raise RuntimeError(
                    f"GitHub returned HTTP {error.code} for {repository}/{endpoint}") from error
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)


def platform_names(system, machine):
    if system.startswith(('CYGWIN', 'MSYS', 'MINGW')) or system == 'Windows':
        system = 'Windows_NT'
    machine = {'AMD64': 'x86_64', 'x64': 'x86_64', 'ARM64': 'arm64'}.get(machine, machine)
    if machine in {'arm64', 'aarch64'}:
        machine = 'aarch64' if system == 'Linux' else 'arm64'
    if (system, machine) not in {
        ('Linux', 'x86_64'), ('Linux', 'aarch64'), ('Darwin', 'x86_64'),
        ('Darwin', 'arm64'), ('Windows_NT', 'x86_64'),
    }:
        raise ValueError(f'Unsupported platform: {system}/{machine}')
    return system, machine


def select(version, system, machine, native=False):
    """Select a binary release, or resolve a source ref exactly once."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", version):
        raise ValueError("version must be a release tag, commit, branch, or YYYY-MM-DD")
    system, machine = platform_names(system, machine)

    nightly = bool(re.fullmatch(r"(?:nightly-)?\d{4}-\d{2}-\d{2}", version))
    if nightly:
        date = version.removeprefix("nightly-")
        datetime.date.fromisoformat(date)
        version = f"nightly-{date}"
    repository = REPOSITORY + ("-nightly" if nightly else "")
    ref = urllib.parse.quote(version, safe="")
    # Hex commit IDs cannot denote one of F*'s published release tags.
    release = None if re.fullmatch(r"[0-9a-fA-F]{7,40}", version) else api(
        repository, f"releases/tags/{ref}", missing_ok=not nightly)
    revision = api(repository, f"commits/{ref}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("GitHub did not return a full commit SHA")

    result = {"commit": revision, "repository": repository, "source": native or release is None,
              "native": native,
              "version": version if release else revision}
    if release is not None and not native:
        prefix = "" if nightly else version + "-"
        suffix = 'zip' if system == 'Windows_NT' else 'tar.gz'
        name = f"fstar-{prefix}{system}-{machine}.{suffix}"
        asset = next((a for a in release["assets"] if a["name"] == name), None)
        if asset is None:
            raise ValueError(f"Release {version} has no {name}; available assets: "
                             + ", ".join(a["name"] for a in release["assets"]))
        digest = asset.get("digest")
        if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Unsupported release digest: {digest}")
        result.update(url=asset["browser_download_url"], digest=digest, asset=asset["id"])
    elif release is not None:
        prefix = '' if nightly else version + '-'
        # Recompile the portable OCaml source distribution, also used by F*'s
        # Windows release workflow, in the selected switch.
        source = next((a for a in release['assets'] if a['name'] == f'fstar-{prefix}src.tar.gz'), None)
        if source:
            result.update(source_url=source['browser_download_url'], source_digest=source.get('digest'))
    return result


def append(variable, values):
    with open(os.environ[variable], "a", encoding="utf-8") as output:
        for value in values:
            if "\n" in value or "\r" in value:
                raise ValueError("Newlines are not allowed in action outputs")
            output.write(value + "\n")


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve():
    cache = os.environ["INSTALL_FSTAR_CACHE"]
    if cache not in {"true", "false"}:
        raise ValueError("cache must be 'true' or 'false'")
    native = os.environ["INSTALL_FSTAR_NATIVE"]
    if native not in {"true", "false"}:
        raise ValueError("native must be 'true' or 'false'")
    native = native == "true"
    system, machine = platform_names(platform.system(), platform.machine())
    state = select(os.environ["INSTALL_FSTAR_VERSION"], system, machine, native=native)
    state.update(system=system, machine=machine)
    if native:
        # Capture the active switch before changing to the source directory.
        # In particular, an implicitly selected workspace/_opam switch would
        # otherwise stop being active when we cd into RUNNER_TEMP.
        try:
            state["opamroot"] = subprocess.check_output(["opam", "var", "root"], text=True).strip()
            state["opamswitch"] = subprocess.check_output(["opam", "switch", "show", "--safe"], text=True).strip()
            state["ocaml"] = subprocess.check_output(["opam", "exec", "--", "ocamlc", "-version"], text=True).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError("native: true requires an active OPAM switch; run ocaml/setup-ocaml first") from error
        print(f"Native build using OPAM switch {state['opamswitch']} (OCaml {state['ocaml']}); package cache disabled")
    elif state["source"]:
        ocaml = os.environ["INSTALL_FSTAR_OCAML"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", ocaml):
            raise ValueError("ocaml-version must be an exact compiler version, e.g. 5.3.0")
        if system == 'Linux':
            distro = platform.freedesktop_os_release()
            if distro['ID'] != 'ubuntu':
                raise ValueError('Automatic Linux source builds require Ubuntu; use native with a provisioned OPAM switch elsewhere')
            os_version = distro['VERSION_ID']
        else:
            os_version = platform.mac_ver()[0] if system == 'Darwin' else platform.version()
        state.update(ocaml=ocaml, os_version=os_version)
    implementation = [file_hash(ACTION / name) for name in ("action.yml", "install.py", "build.py", "build.sh")]
    identity = json.dumps([state, system, machine, implementation], sort_keys=True).encode()
    fingerprint = hashlib.sha256(identity).hexdigest()
    state["cache-key"] = f"install-fstar-v1-{system}-{machine}-{fingerprint}"
    # This path must be stable across jobs: actions/cache hashes the path as
    # well as the key when determining whether a cache entry is compatible.
    suffix = 'zip' if system == 'Windows_NT' and not state['source'] else 'tar.gz'
    archive = Path(os.environ["RUNNER_TEMP"]) / "install-fstar-cache" / fingerprint / f"fstar.{suffix}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)
    work = Path(tempfile.mkdtemp(prefix="install-fstar-", dir=os.environ["RUNNER_TEMP"]))
    state.update(archive=str(archive), work=str(work))
    state_path = work / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    append("GITHUB_OUTPUT", [f"{key}={state[key]}" for key in ("version", "commit", "archive", "cache-key")])
    append("GITHUB_OUTPUT", [f"state={state_path}", f"source={str(state['source']).lower()}"])
    append("GITHUB_OUTPUT", [f"cache={str(cache == 'true' and not native).lower()}"])
    append('GITHUB_OUTPUT', [f"method={'source' if state['source'] else 'binary'}"])
    print(f"Selected {state['version']} ({state['commit']})")


def check_digest(state):
    digest = state.get("digest")
    if digest and file_hash(state["archive"]) != digest.removeprefix("sha256:"):
        raise ValueError("F* archive SHA-256 does not match GitHub's release metadata")


def download(state):
    subprocess.run(["curl", "--fail", "--location", "--retry", "3",
                    "--output", state["archive"], state["url"]], check=True)
    check_digest(state)


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or PureWindowsPath(name).drive:
        raise ValueError(f'Unsafe archive entry: {name}')


def unpack(archive, destination):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                safe_name(member.filename)
                kind = (member.external_attr >> 16) & 0o170000
                if kind not in (0, 0o100000, 0o040000):
                    raise ValueError(f'Unsafe archive entry: {member.filename}')
            package.extractall(destination)
        return
    with tarfile.open(archive, 'r:gz') as package:
        members = package.getmembers()
        for member in members:
            safe_name(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f'Unsafe archive entry: {member.name}')
        package.extractall(destination, members=members)


def extract(archive, destination):
    # F* packages contain ./fstar/... (some older ones omit the leading ./).
    # Discover the root instead of depending on tar's strip-components count.
    # Packaging dereferences links, so only regular files/directories are needed.
    unpack(archive, destination)
    candidates = list(destination.glob("*/bin/fstar.exe"))
    if len(candidates) != 1:
        raise ValueError("Expected exactly one F* installation in the archive")
    return candidates[0].parent.parent


def install(state):
    check_digest(state)  # Also validate restored release archives.
    if not state["source"] and not state.get("digest"):
        print("::warning::This older release has no SHA-256 digest in GitHub's metadata")
    destination = Path(state["work"]) / "installed"
    destination.mkdir()
    root = extract(state["archive"], destination)
    exe = root / "bin/fstar.exe"
    version = subprocess.check_output([str(exe), "--version"], text=True)
    print(version, end="")
    if f"commit={state['commit']}" not in version.splitlines():
        raise ValueError(f"Installed F* does not report the requested commit {state['commit']}")
    if state.get('native') and f"compiler=OCaml {state['ocaml']}" not in version.splitlines():
        raise ValueError('The native build did not use the active OCaml compiler')
    if not any(p.name in {'z3', 'z3.exe'} for p in (root / 'lib/fstar').glob('z3-*/bin/z3*')):
        raise ValueError("The F* package has no bundled Z3 solvers")
    append("GITHUB_PATH", [(root / 'bin').as_posix()])
    append("GITHUB_ENV", [f"FSTAR_EXE={exe.as_posix()}"])
    append("GITHUB_OUTPUT", [f"path={root.as_posix()}", f"fstar-exe={exe.as_posix()}"])


def main():
    command = sys.argv[1]
    if command == "resolve":
        resolve()
        return
    state = json.loads(Path(os.environ["INSTALL_FSTAR_STATE"]).read_text(encoding="utf-8"))
    if command == "download":
        download(state)
    elif command == "build":
        from build import build
        build(state)
    elif command == "install":
        install(state)
    else:
        raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
