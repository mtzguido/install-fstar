"""Integration assertions run on the actual GitHub-hosted OS/architecture."""
import json
import os
from pathlib import Path
import platform
import subprocess


work = Path(os.environ['RUNNER_TEMP']) / 'install-fstar-tests'
work.mkdir(exist_ok=True)
env = os.environ.copy()
exe = os.environ['FSTAR_EXE']


def run(args, reject=False):
    print('+', ' '.join(args), flush=True)
    # Expected proof failures should not emit GitHub error annotations.
    command_env = dict(env, GITHUB_ACTIONS='') if reject else env
    result = subprocess.run(args, cwd=work, env=command_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end='', flush=True)
    with (work / 'commands.log').open('a', encoding='utf-8') as log:
        log.write(' '.join(args) + '\n' + result.stdout)
    if reject:
        assert result.returncode != 0, 'F* accepted an invalid proof'
        assert 'could not prove' in result.stdout.lower(), result.stdout
    else:
        result.check_returncode()
    return result.stdout


version = run([exe, '--version'])
assert f"commit={os.environ['EXPECTED_COMMIT']}" in version.splitlines()
assert os.environ['EXPECTED_METHOD'] == os.environ['ACTUAL_METHOD']
assert Path(exe).is_file()
work.joinpath('Example.fst').write_text('module Example\nlet _ = assert (forall (x:int). x + 1 > x)\n', encoding='utf-8')
run([exe, str(work / 'Example.fst')])
work.joinpath('Invalid.fst').write_text('module Invalid\nlet _ = assert (forall (x:int). x + 1 = x)\n', encoding='utf-8')
run([exe, str(work / 'Invalid.fst')], reject=True)
if os.environ.get('LEGACY') != 'true':
    work.joinpath('PulseExample.fst').write_text('module PulseExample\n#lang-pulse\nopen Pulse\nfn test (x:int) returns int { (x+1); }\n', encoding='utf-8')
    run([exe, str(work / 'PulseExample.fst')])
if os.environ.get('NATIVE') == 'true':
    # Capture the implicit local switch before leaving the caller's workspace.
    env['OPAMSWITCH'] = subprocess.check_output(['opam', 'switch', 'show', '--safe'], text=True).strip()
    prefix = subprocess.check_output(['opam', 'var', 'prefix'], text=True).strip()
    assert prefix == Path(os.environ['RUNNER_TEMP'], 'caller-prefix').read_text().strip()
    assert f"compiler=OCaml {os.environ['EXPECTED_OCAML']}" in version.splitlines()
    for suffix in ('cmxs', 'cmi', 'cmx', 'o', 'ml', 'fst.checked'):
        work.joinpath('Plugin.' + suffix).unlink(missing_ok=True)
    work.joinpath('Plugin.fst').write_text('module Plugin\n[@@plugin] irreducible let identity (x: int) : int = x\n', encoding='utf-8')
    work.joinpath('UsePlugin.fst').write_text('module UsePlugin\nlet _ = assert_norm (Plugin.identity 42 = 42)\n', encoding='utf-8')
    for args in (['Plugin.fst', '--cache_checked_modules'], ['Plugin.fst', '--codegen', 'Plugin'],
                 ['UsePlugin.fst', '--load', 'Plugin']):
        run(['opam', 'exec', '--', exe, *args])
    assert work.joinpath('Plugin.cmxs').is_file()
work.joinpath('result.json').write_text(json.dumps({
    'platform': platform.platform(), 'machine': platform.machine(), 'version': version,
    'native': os.environ.get('NATIVE', 'false'), 'method': os.environ['ACTUAL_METHOD'],
    'commit': os.environ['EXPECTED_COMMIT'], 'passed': True,
}, indent=2), encoding='utf-8')
