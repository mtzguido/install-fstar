"""Build a package without exporting a private build switch to the caller."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from install import ACTION, file_hash, unpack


def run(args, env, cwd=None, capture=False):
    args = [str(a) for a in args]
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


def build(state):
    env = os.environ.copy()
    env.update(OPAMYES='1', OPAMCONFIRMLEVEL='unsafe-yes')
    with tempfile.TemporaryDirectory(prefix='build-', dir=state['work']) as directory:
        work = Path(directory)
        prepare_system(state, env, work)
        if state['native']:
            env.update(OPAMROOT=state['opamroot'], OPAMSWITCH=state['opamswitch'])
        else:
            for name in ('OPAMSWITCH', 'OPAM_SWITCH_PREFIX', 'CAML_LD_LIBRARY_PATH',
                         'OCAML_TOPLEVEL_PATH', 'OCAMLTOP_INCLUDE_PATH', 'OCAMLPATH', 'OCAMLLIB'):
                env.pop(name, None)
            env['OPAMROOT'] = (work / 'opam').as_posix()
            args = ['opam', 'init', '--bare', '--no-setup', '--disable-sandboxing']
            if state['system'] == 'Windows_NT':
                args.append('--cygwin-internal-install')
            run(args, env, cwd=work)
            run(['opam', 'switch', 'create', 'build', 'ocaml-base-compiler.' + state['ocaml']], env, cwd=work)
            env['OPAMSWITCH'] = 'build'

        source = work / 'source'
        if state.get('source_url'):
            archive = work / 'source.tar.gz'
            fetch(state['source_url'], state.get('source_digest'), archive, env)
            source.mkdir()
            unpack(archive, source)
            source = source / 'fstar'
            if not (source / 'Makefile').is_file():
                raise ValueError('The source package has no fstar/Makefile')
        else:
            run(['git', 'init', '-q', source], env)
            if state['system'] == 'Windows_NT':
                run(['git', '-C', source, 'config', 'core.symlinks', 'true'], env)
            run(['git', '-C', source, 'remote', 'add', 'origin',
                 f"https://github.com/{state['repository']}.git"], env)
            run(['git', '-C', source, 'fetch', '--depth=1', 'origin', state['commit']], env)
            run(['git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD'], env)
            if run(['git', '-C', source, 'rev-parse', 'HEAD'], env, capture=True) != state['commit']:
                raise ValueError('The checkout does not match the requested commit')
            run(['git', '-C', source, 'submodule', 'update', '--init', '--recursive', '--depth=1'], env)

        script, output = str(ACTION / 'build.sh'), state['archive']
        bash = shutil.which('bash', path=env['PATH'])
        if state['system'] == 'Windows_NT':
            cygwin_bin = Path(env['OPAMROOT']) / '.cygwin/root/bin'
            if (cygwin_bin / 'bash.exe').exists():
                bash = str(cygwin_bin / 'bash.exe')
                prepend(env, cygwin_bin)
            if not bash or not run([bash, '-c', 'uname -s'], env, capture=True).startswith('CYGWIN'):
                raise ValueError('Windows source builds require Cygwin (the default ocaml/setup-ocaml environment)')
            cygpath = str(Path(bash).with_name('cygpath.exe'))
            script, source, output = [run([cygpath, '-u', str(p)], env, capture=True) for p in (script, source, output)]
        if not bash:
            raise ValueError('Building F* requires Bash')
        run([bash, script, source, output, str(os.cpu_count() or 2)], env)
