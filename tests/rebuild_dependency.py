"""Rebuild installed native artifacts, keeping the package version unchanged."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

env = os.environ.copy()
env['OPAMSWITCH'] = subprocess.check_output(['opam', 'switch', 'show', '--safe'], text=True).strip()
prefix = Path(subprocess.check_output(['opam', 'var', 'prefix'], text=True).strip())
installed = prefix / 'lib/install_fstar_cache_probe'
installed.mkdir(exist_ok=True)
(installed / 'META').write_text('version = "1.0"\n')
value = int(sys.argv[1])
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    (work / 'probe.ml').write_text(f'let answer = {value}\n')
    (work / 'stub.c').write_text(f'int probe(void) {{ return {value}; }}\n')
    for source in ('probe.ml', 'stub.c'):
        subprocess.run(['opam', 'exec', '--', 'ocamlopt', '-c', source], cwd=work, env=env, check=True)
    for name in ('probe.cmi', 'probe.cmx', 'probe.o', 'stub.o'):
        shutil.copyfile(work / name, installed / name)
