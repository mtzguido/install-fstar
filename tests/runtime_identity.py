import json, os, pathlib, shutil, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import build
root = pathlib.Path(os.environ['RUNNER_TEMP'])
prefix = pathlib.Path(subprocess.check_output(['opam', 'var', 'prefix'], text=True).strip())
state = dict(native=True, system='Windows_NT',
             opamroot=subprocess.check_output(['opam', 'var', 'root'], text=True).strip(),
             opamswitch=subprocess.check_output(['opam', 'switch', 'show', '--safe'], text=True).strip())
env = build.environment(state)
action = pathlib.Path(__file__).resolve().parents[1]
first, second = root / 'before.json', root / 'after.json'
build.run(['opam', 'exec', '--', sys.executable, action / 'fingerprint.py', prefix, first], env)
bundle = root / 'bundled-compiler'
(bundle / 'bin').mkdir(parents=True)
shutil.copy2(prefix / 'bin/ocamlopt.opt.exe', bundle / 'bin/fstar.exe')
build.run(['opam', 'exec', '--', sys.executable, action / 'build.py', 'windows-runtime', bundle], env)
build.prepend(env, bundle / 'bin')
build.run(['opam', 'exec', '--', sys.executable, action / 'fingerprint.py', prefix, second], env)
before = json.loads(first.read_text())
after = json.loads(second.read_text())
print('before:', before['fingerprint'], 'after:', after['fingerprint'], flush=True)
for key in before['data']:
    a, b = before['data'][key], after['data'][key]
    if a == b:
        continue
    print('CHANGED', key, flush=True)
    if isinstance(a, dict):
        for name in sorted(a.keys() | b.keys()):
            if a.get(name) != b.get(name):
                print(name, a.get(name), '->', b.get(name), flush=True)
    else:
        print(a, '->', b, flush=True)

assert before['fingerprint'] == after['fingerprint'], 'Bundling runtime libraries changed the compiler environment'
