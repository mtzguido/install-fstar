"""Content fingerprints of an installed OCaml environment, never package versions.

Run inside `opam exec` after dependency reconciliation. Timestamps, inode numbers,
OPAM logs and build directories deliberately do not contribute to the identity.
"""

import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys

from install import file_hash, identity, platform_names


def output(args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def system_abi():
    system, machine = platform_names(platform.system(), platform.machine())
    result = {'system': system, 'machine': machine, 'release': platform.release(),
              'version': platform.version()}
    if system == 'Linux':
        result['distribution'] = platform.freedesktop_os_release()
        result['libc'] = platform.libc_ver()
    elif system == 'Darwin':
        result['macos'] = output(['sw_vers'])
        result['sdk'] = output(['xcrun', '--show-sdk-version'])
    return result


class Files:
    """Hash files and symlink targets, including targets outside the switch."""

    def __init__(self):
        self.entries = {}
        self.digests = {}
        self.binaries = set()
        self.bytes = 0

    def add(self, path, parents=()):
        path = Path(path).absolute()
        name = str(path)
        if name in self.entries:
            return
        if path.is_symlink():
            self.entries[name] = ['symlink', os.readlink(path)]
            target = path.resolve(strict=True)
            if target in parents:
                raise ValueError(f'Cyclic library symlink: {path}')
            self.add(target, parents)
        elif path.is_dir():
            self.entries[name] = ['directory']
            for child in sorted(path.iterdir()):
                self.add(child, (*parents, path))
        elif path.is_file():
            info = path.stat()
            # Hard links are common in compiler installations. Read each actual
            # file once, without using mtime/size as a substitute for its bytes.
            key = (info.st_dev, info.st_ino) if info.st_ino else name
            if key not in self.digests:
                self.digests[key] = file_hash(path)
                self.bytes += info.st_size
            self.entries[name] = ['file', stat.S_IMODE(info.st_mode), self.digests[key]]
            with path.open('rb') as source:
                header = source.read(18)
            if header.startswith(b'\x7fELF'):
                endian = 'little' if header[5] == 1 else 'big'
                dynamic = int.from_bytes(header[16:18], endian) in (2, 3)
            else:
                dynamic = header.startswith((b'MZ', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                             b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'))
            if dynamic:
                self.binaries.add(path)
        elif not path.exists():
            self.entries[name] = ['missing']
        else:
            raise ValueError(f'Cannot fingerprint non-regular installed file: {path}')


def linked_libraries(binary, system):
    """Resolve C runtime dependencies of installed executables and C stubs."""
    if system == 'Linux':
        result = subprocess.run(['ldd', str(binary)], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode and not any(s in result.stdout + result.stderr
                                        for s in ('not a dynamic executable', 'statically linked')):
            raise ValueError(f'Cannot inspect shared libraries of {binary}: {result.stderr}')
        if '=> not found' in result.stdout:
            raise ValueError(f'Missing shared library for {binary}: {result.stdout}')
        return [Path(p) for p in re.findall(r'(?:=>\s+)?(/[^\n]+?)\s+\(0x[0-9a-f]+\)', result.stdout)]
    if system == 'Windows_NT':
        # cygcheck reports transitive dependencies using native Windows paths.
        result = output(['cygcheck', binary])
        return [Path(line.strip()) for line in result.splitlines() if Path(line.strip()).is_file()]
    listing = output(['otool', '-L', binary])
    rpaths = re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset',
                       output(['otool', '-l', binary]))
    libraries = []
    for line in listing.splitlines()[1:]:
        name = line.strip().split(' (compatibility version', 1)[0]
        name = name.replace('@loader_path', str(binary.parent)).replace('@executable_path', str(binary.parent))
        if name.startswith('@rpath/'):
            candidates = [Path(p.replace('@loader_path', str(binary.parent))) / name[7:] for p in rpaths]
            candidate = next((p for p in candidates if p.is_file()), None)
            if candidate is None:
                raise ValueError(f'Cannot resolve {name} linked by {binary}')
            libraries.append(candidate)
        elif name.startswith('/'):
            path = Path(name)
            # Modern macOS supplies these from its dyld cache. sw_vers pins that
            # system ABI; Homebrew and other on-disk libraries are content-hashed.
            if path.exists():
                libraries.append(path)
            elif not name.startswith(('/usr/lib/', '/System/Library/')):
                raise ValueError(f'Missing shared library {name} linked by {binary}')
    return libraries


def capture(prefix):
    files = Files()
    prefix = Path(prefix)
    config = output(['ocamlc', '-config'])
    native_config = output(['ocamlopt', '-config'])
    findlib = output(['ocamlfind', 'printconf', 'path']).splitlines()
    # Hash all installed code, not only files with known extensions. This covers
    # interfaces, native objects, archives, generated code, C stubs and headers.
    for name in ('bin', 'sbin', 'lib', 'libexec', 'include', 'etc', 'share'):
        files.add(prefix / name)
    for directory in [output(['ocamlc', '-where']), *findlib]:
        files.add(directory)
    findlib_conf = output(['ocamlfind', 'printconf', 'conf'])
    files.add(findlib_conf)
    for variable in ('OCAMLPATH', 'CAML_LD_LIBRARY_PATH', 'OCAMLLIB'):
        for directory in os.environ.get(variable, '').split(os.pathsep):
            if directory:
                files.add(directory)
    compiler = dict(line.split(': ', 1) for line in config.splitlines() if ': ' in line)
    tools = {}
    for name in ('ocamlc', 'ocamlopt', 'ocamlrun', 'ocamlfind', 'dune',
                 shlex.split(compiler['c_compiler'])[0], 'ld', 'ar'):
        executable = shutil.which(name)
        if not executable:
            raise ValueError(f'Cannot locate build tool {name}')
        tools[name] = executable
        files.add(executable)
    abi = system_abi()
    inspected = set()
    while files.binaries - inspected:
        binary = sorted(files.binaries - inspected)[0]
        inspected.add(binary)
        for library in linked_libraries(binary, abi['system']):
            files.add(library)
    flags = {key: os.environ.get(key, '') for key in
             ('OCAMLPARAM', 'OCAMLPATH', 'OCAMLLIB', 'OCAMLFIND_CONF', 'CAML_LD_LIBRARY_PATH',
              'CC', 'CFLAGS', 'CPPFLAGS', 'CXXFLAGS', 'LDFLAGS', 'LIBRARY_PATH',
              'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET')}
    data = {'system': abi, 'compiler': config, 'native-compiler': native_config,
            'findlib': findlib, 'tools': tools, 'flags': flags, 'files': files.entries}
    return {'fingerprint': identity(data), 'data': data, 'bytes': files.bytes}


if __name__ == '__main__':
    result = capture(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(result, sort_keys=True), encoding='utf-8')
    print(f"OCaml environment {result['fingerprint']}: "
          f"{len(result['data']['files'])} paths, {result['bytes'] // (1024 * 1024)} MiB hashed")
