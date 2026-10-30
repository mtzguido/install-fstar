import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build
import fingerprint
import install


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('bin/ocamlc', 'bin/ocamlopt', 'lib/ocaml/stdlib.cmi',
                     'lib/example/META', 'lib/example/example.cmi', 'lib/example/example.cmx',
                     'lib/example/example.a', 'lib/example/example.o', 'lib/stublibs/dllexample.so'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixed package version, original compiled bytes')

    def tree(self):
        files = fingerprint.Files()
        files.add(self.root)
        return install.identity(files.entries)

    def test_same_version_rebuilds_change_identity_for_every_artifact_kind(self):
        before = self.tree()
        meta = (self.root / 'lib/example/META').read_bytes()
        for relative in ('bin/ocamlc', 'bin/ocamlopt', 'lib/ocaml/stdlib.cmi',
                         'lib/example/example.cmi', 'lib/example/example.cmx',
                         'lib/example/example.o', 'lib/example/example.a', 'lib/stublibs/dllexample.so'):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                timestamp = path.stat().st_mtime_ns
                path.write_bytes(original.replace(b'original', b'modified'))
                os.utime(path, ns=(timestamp, timestamp))  # Same size and mtime too.
                self.assertNotEqual(before, self.tree())
                self.assertEqual(meta, (self.root / 'lib/example/META').read_bytes())
                path.write_bytes(original)
                self.assertEqual(before, self.tree())

    def test_timestamps_do_not_invalidate_identical_artifacts(self):
        before = self.tree()
        for path in self.root.rglob('*'):
            os.utime(path, (100, 100))
        self.assertEqual(before, self.tree())

    def test_new_and_removed_interfaces_change_identity(self):
        before = self.tree()
        extra = self.root / 'lib/example/extra.cmi'
        extra.write_bytes(b'extra')
        self.assertNotEqual(before, self.tree())
        extra.unlink()
        self.assertEqual(before, self.tree())
        (self.root / 'lib/example/example.cmi').unlink()
        self.assertNotEqual(before, self.tree())

    def test_symlinked_external_dependency_bytes_are_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / 'library.cmi'
            external.write_bytes(b'first')
            link = self.root / 'lib/example/linked.cmi'
            try:
                link.symlink_to(external)
            except OSError:
                self.skipTest('Host does not allow creating symlinks')
            before = self.tree()
            external.write_bytes(b'other')
            self.assertNotEqual(before, self.tree())

    def capture(self, compiler='version: 5.4.0\nc_compiler: cc', abi='abi-1'):
        values = {('ocamlc', '-config'): compiler, ('ocamlopt', '-config'): compiler,
                  ('ocamlc', '-where'): str(self.root / 'lib/ocaml'),
                  ('ocamlfind', 'printconf', 'path'): str(self.root / 'lib'),
                  ('ocamlfind', 'printconf', 'conf'): str(self.root / 'etc/findlib.conf')}
        with patch.object(fingerprint, 'output', side_effect=lambda a: values[tuple(a)]), \
             patch.object(fingerprint, 'system_abi', return_value={'system': 'Linux', 'abi': abi}), \
             patch('shutil.which', side_effect=lambda name: str(self.root / 'bin' / name)), \
             patch.dict(os.environ, {'OCAMLPATH': '', 'OCAMLLIB': '', 'CAML_LD_LIBRARY_PATH': ''}):
            return fingerprint.capture(self.root)['fingerprint']

    def test_compiler_configuration_and_system_abi_are_part_of_identity(self):
        before = self.capture()
        self.assertEqual(before, self.capture())
        self.assertNotEqual(before, self.capture(compiler='version: 5.4.0\nc_compiler: cc\nflambda: true'))
        self.assertNotEqual(before, self.capture(abi='abi-2'))

    def test_package_identity_uses_commit_recipe_and_environment_not_selector(self):
        state = {'source': True, 'system': 'Linux', 'machine': 'x86_64', 'commit': 'a' * 40,
                 'repository': install.REPOSITORY, 'version': 'branch'}
        before = install.package_identity(state, 'environment-1')
        self.assertEqual(before, install.package_identity({**state, 'version': state['commit'],
                                                           'cache': False, 'work': '/another/path'}, 'environment-1'))
        self.assertNotEqual(before, install.package_identity(state, 'environment-2'))
        self.assertNotEqual(before, install.package_identity({**state, 'commit': 'b' * 40}, 'environment-1'))
        with patch.object(install, 'recipe', return_value={'build.sh': 'changed'}):
            self.assertNotEqual(before, install.package_identity(state, 'environment-1'))

    def test_dependencies_reconciled_even_with_an_existing_private_switch(self):
        (self.root / 'config').touch()
        state = {'native': False, 'system': 'Linux', 'opamroot': str(self.root),
                 'opamswitch': 'build', 'ocaml': '5.4.0', 'work': str(self.root), 'source_path': str(self.root)}
        with patch.object(build, 'run', return_value='5.4.0') as run:
            build.dependencies(state)
        self.assertEqual(run.call_args_list[0].args[0], ['opam', 'update'])
        self.assertEqual(run.call_args_list[1].args[0], ['opam', 'install', '--deps-only', './fstar.opam'])
        self.assertEqual(len(run.call_args_list), 3)
        with patch.object(build, 'run', return_value='5.4.0') as run:
            build.dependencies({**state, 'native': True})
        self.assertEqual(run.call_args_list[0].args[0], ['opam', 'install', '--deps-only', './fstar.opam'])
        self.assertEqual(len(run.call_args_list), 2)  # Caller owns its repositories too.
        with patch.object(build, 'run', return_value='5.3.0'):
            with self.assertRaisesRegex(ValueError, 'changed the compiler'):
                build.dependencies(state)

    def test_private_compiler_cache_survives_fstar_recipe_and_commit_changes(self):
        state = {'ocaml': '5.4.0', 'commit': 'a' * 40}
        with patch.object(build, 'system_abi', return_value={'system': 'Linux', 'abi': 'one'}):
            before = build.dependency_cache_base(state, '2.6.1')
            with patch.object(install, 'recipe', return_value={'build.sh': 'changed'}):
                self.assertEqual(before, build.dependency_cache_base({**state, 'commit': 'b' * 40}, '2.6.1'))
            self.assertNotEqual(before, build.dependency_cache_base({**state, 'ocaml': '5.3.0'}, '2.6.1'))
            self.assertNotEqual(before, build.dependency_cache_base(state, '2.7.0'))
            with patch.object(build, 'DEPENDENCY_CACHE_SCHEMA', 2):
                self.assertNotEqual(before, build.dependency_cache_base(state, '2.6.1'))
        with patch.object(build, 'system_abi', return_value={'system': 'Linux', 'abi': 'two'}):
            self.assertNotEqual(before, build.dependency_cache_base(state, '2.6.1'))

    def test_native_environment_does_not_replace_caller_flags(self):
        state = {'native': True, 'system': 'Linux', 'opamroot': '/caller/root', 'opamswitch': '/caller/project'}
        with patch.dict(os.environ, {'OCAMLPATH': '/caller/lib', 'CFLAGS': '-O3'}):
            env = build.environment(state)
        self.assertEqual(env['OCAMLPATH'], '/caller/lib')
        self.assertEqual(env['CFLAGS'], '-O3')
        self.assertEqual(env['OPAMSWITCH'], '/caller/project')

    def test_bootstrapped_tool_is_found_in_child_environment_path(self):
        directory = self.root / 'tools'
        directory.mkdir()
        executable = directory / ('fstar-fixture.cmd' if os.name == 'nt' else 'fstar-fixture')
        executable.write_text('@echo off\necho bootstrapped\n' if os.name == 'nt'
                              else '#!/bin/sh\necho bootstrapped\n')
        executable.chmod(0o755)
        env = os.environ.copy()
        build.prepend(env, directory)
        self.assertEqual(build.run(['fstar-fixture'], env, capture=True), 'bootstrapped')


if __name__ == '__main__':
    unittest.main()
