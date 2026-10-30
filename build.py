"""Build a package without exporting a private build switch to the caller."""

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

from install import ACTION, append, file_hash, identity, package_cache, recipe, unpack
from fingerprint import system_abi


def run(args, env, cwd=None, capture=False):
    args = [str(a) for a in args]
    # CreateProcess on Windows does not use env['PATH'] to find the program.
    # Resolve explicitly so newly bootstrapped OPAM/Cygwin tools are visible.
    args[0] = shutil.which(args[0], path=env['PATH']) or args[0]
    print('+', ' '.join(args), flush=True)
    if capture:
        return subprocess.check_output(args, env=env, cwd=cwd, text=True).strip()
    subprocess.run(args, env=env, cwd=cwd, check=True)


def prepend(env, *paths):
    env['PATH'] = os.pathsep.join([*(str(p) for p in paths), env['PATH']])


def fetch(url, digest, destination, env):
    run(['curl', '--fail', '--location', '--retry', '3', '--output', destination, url], env)
    if digest and digest != 'sha256:' + file_hash(destination):
        raise ValueError(f'SHA-256 mismatch for {url}')


def prepare_system(state, env, work):
    system = state['system']
    if system == 'Linux' and not state['native']:
        run(['sudo', 'apt-get', 'update', '-qq'], env)
        run(['sudo', 'apt-get', 'install', '-y', '-qq', '--no-install-recommends',
             'opam', 'build-essential', 'libgmp-dev', 'pkg-config', 'curl', 'unzip', 'rsync', 'git'], env)
    elif system == 'Darwin':
        packages = ['bash', 'coreutils', 'gnu-sed', 'gnu-getopt', 'make', 'gmp', 'pkg-config', 'rsync']
        if not state['native']:
            packages.append('opam')
        run(['brew', 'install', *packages], env)
        prefix = Path(run(['brew', '--prefix'], env, capture=True))
        prepend(env, prefix / 'opt/bash/bin', prefix / 'opt/coreutils/libexec/gnubin',
                prefix / 'opt/gnu-sed/libexec/gnubin', prefix / 'opt/gnu-getopt/bin')
    elif system == 'Windows_NT' and not shutil.which('opam', path=env['PATH']):
        directory = work / 'tools'
        directory.mkdir()
        # A pinned official binary; opam itself provisions its Cygwin tools.
        fetch('https://github.com/ocaml/opam/releases/download/2.6.1/opam-2.6.1-x86_64-windows.exe',
              'sha256:2548f0592abfa728432c85ef6d08bf97c295c1859ddca61876e33884336c130c',
              directory / 'opam.exe', env)
        prepend(env, directory)


def environment(state):
    env = os.environ.copy()
    env.update(OPAMYES='1', OPAMCONFIRMLEVEL='unsafe-yes')
    if state['system'] == 'Windows_NT':
        env.setdefault('CYGWIN', 'winsymlinks:native')
        env.setdefault('MSYS', 'winsymlinks:native')
    if not state['native']:
        for name in ('OPAMSWITCH', 'OPAM_SWITCH_PREFIX', 'CAML_LD_LIBRARY_PATH',
                     'OCAML_TOPLEVEL_PATH', 'OCAMLTOP_INCLUDE_PATH', 'OCAMLPATH', 'OCAMLLIB'):
            env.pop(name, None)
    if 'opamroot' in state:
        env.update(OPAMROOT=state['opamroot'], OPAMSWITCH=state['opamswitch'])
    prepend(env, *state.get('tool_paths', []))
    if state['system'] == 'Windows_NT' and 'opamroot' in state:
        cygwin_bin = Path(state['opamroot']) / '.cygwin/root/bin'
        if cygwin_bin.is_dir():
            prepend(env, cygwin_bin)
    return env


def prepare(state):
    """Fetch the dependency manifest before restoring a private OPAM cache."""
    work = Path(state['work'])
    env = environment(state)
    original_path = env['PATH']
    prepare_system(state, env, work)
    state['tool_paths'] = env['PATH'].removesuffix(original_path).rstrip(os.pathsep).split(os.pathsep)
    state['tool_paths'] = [p for p in state['tool_paths'] if p]
    source = work / 'source'
    if state.get('source_url'):
        archive = work / 'source.tar.gz'
        fetch(state['source_url'], state.get('source_digest'), archive, env)
        source.mkdir()
        unpack(archive, source)
        source = source / 'fstar'
    else:
        run(['git', 'init', '-q', source], env)
        if state['system'] == 'Windows_NT':
            run(['git', '-C', source, 'config', 'core.symlinks', 'true'], env)
            run(['git', '-C', source, 'config', 'core.autocrlf', 'false'], env)
        run(['git', '-C', source, 'remote', 'add', 'origin',
             f"https://github.com/{state['repository']}.git"], env)
        run(['git', '-C', source, 'fetch', '--depth=1', 'origin', state['commit']], env)
        run(['git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD'], env)
        if run(['git', '-C', source, 'rev-parse', 'HEAD'], env, capture=True) != state['commit']:
            raise ValueError('The checkout does not match the requested commit')
    if not (source / 'fstar.opam').is_file():
        raise ValueError('The source has no fstar.opam dependency manifest')
    state['source_path'] = str(source)
    if not state['native']:
        base = identity([system_abi(), state['ocaml'], run(['opam', '--version'], env, capture=True), recipe()])
        prefix = f'install-fstar-opam-v2-{base}-'
        key = prefix + file_hash(source / 'fstar.opam')
        root = Path(os.environ['RUNNER_TEMP']) / 'ifstar-deps' / base[:16] / 'opam'
        # The path must be stable: compiler configurations contain absolute
        # prefixes. Never reuse an unrelated or partially prepared local switch.
        if root.exists():
            shutil.rmtree(root)
        state.update(opamroot=root.as_posix(), opamswitch='build')
        append('GITHUB_OUTPUT', [f'dependencies-path={root}', f'dependencies-key={key}',
                                 f'dependencies-prefix={prefix}'])


def dependencies(state):
    """Reconcile restored dependencies even on an exact dependency-cache hit."""
    env = environment(state)
    work = Path(state['work'])
    if not state['native']:
        root = Path(state['opamroot'])
        if not (root / 'config').exists():
            args = ['opam', 'init', '--bare', '--no-setup', '--disable-sandboxing']
            if state['system'] == 'Windows_NT':
                args.append('--cygwin-internal-install')
            # OPAMSWITCH cannot name a switch until it has been created.
            initial = env.copy()
            initial.pop('OPAMSWITCH', None)
            run(args, initial, cwd=work)
            run(['opam', 'switch', 'create', 'build', 'ocaml-base-compiler.' + state['ocaml']], initial, cwd=work)
        env = environment(state)  # opam init may have installed Cygwin.
    run(['opam', 'install', '--deps-only', './fstar.opam'], env, cwd=state['source_path'])
    actual = run(['opam', 'exec', '--', 'ocamlc', '-version'], env, capture=True)
    if actual != state['ocaml']:
        raise ValueError(f"Dependency reconciliation changed the compiler: {state['ocaml']} -> {actual}")


def fingerprint(state):
    if state['source']:
        env = environment(state)
        prefix = run(['opam', 'var', 'prefix'], env, capture=True)
        report = Path(state['work']) / 'environment.json'
        run(['opam', 'exec', '--', sys.executable, ACTION / 'fingerprint.py', prefix, report], env)
        result = json.loads(report.read_text(encoding='utf-8'))
        state['environment'] = result['fingerprint']
        append('GITHUB_OUTPUT', [f"environment={state['environment']}"])
    package_cache(state, state.get('environment'))


def build(state):
    env = environment(state)
    source = Path(state['source_path'])
    if not state.get('source_url'):
        run(['git', '-C', source, 'submodule', 'update', '--init', '--recursive', '--depth=1'], env)
    script, output = str(ACTION / 'build.sh'), state['archive']
    bash = shutil.which('bash', path=env['PATH'])
    if state['system'] == 'Windows_NT':
        if not bash or not run([bash, '-c', 'uname -s'], env, capture=True).startswith('CYGWIN'):
            raise ValueError('Windows source builds require Cygwin (the default ocaml/setup-ocaml environment)')
        cygpath = str(Path(bash).with_name('cygpath.exe'))
        script, source, output = [run([cygpath, '-u', str(p)], env, capture=True) for p in (script, source, output)]
    if not bash:
        raise ValueError('Building F* requires Bash')
    run([bash, script, source, output, str(os.cpu_count() or 2)], env)
