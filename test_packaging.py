import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.scripts').mkdir()
        self.script = self.root / '.scripts/bin-install.sh'
        self.script.write_text('''#!/bin/bash
if windows; then
    if [ -z "$LIBGMP" ]; then
      LIBGMP=$(which libgmp-10.dll)
    fi
    cp "$LIBGMP" "$PREFIX/bin"
fi
STRIP=strip
if windows; then
    strip_prefix="$(cygpath -m "$PREFIX")"
fi
$STRIP "$strip_prefix"/bin/*
''')
        (self.root / 'Makefile').write_text('FSTAR_LIB=$(abspath ulib)\nFSTAR_LIB=$(CURDIR)/ulib\n')
        (self.root / 'mk').mkdir()
        (self.root / 'mk/stage.mk').write_text('DUNE=flock $(LOCKFILE) dune\ncd dune && $(DUNE) install --prefix=$(PREFIX)\n')
        (self.root / 'stage3').mkdir()
        (self.root / 'stage3/Makefile').write_text((self.root / 'mk/stage.mk').read_text())
        (self.root / 'pulse/mk').mkdir(parents=True)
        (self.root / 'pulse/mk/common.mk').write_text('$(eval override $(strip $1):=$(abspath $(value $(strip $1))))\n')
        (self.root / 'pulse/mk/boot.mk').write_text('FSTAR_LIBDIR ?= $(shell $(FSTAR_EXE) --locate_lib)\n')

    def test_macos_keeps_symbols_for_dynamic_plugins(self):
        build.prepare_packaging(self.root, 'Darwin')
        self.assertIn('STRIP="strip -S"', self.script.read_text())
        self.assertIn('cp "$LIBGMP"', self.script.read_text())

    def test_windows_uses_actual_runtime_dependencies_and_native_paths(self):
        build.prepare_packaging(self.root, 'Windows_NT')
        contents = self.script.read_text()
        self.assertNotIn('LIBGMP', contents)
        self.assertIn('windows-runtime "$(cygpath -m "$PREFIX")"', contents)
        self.assertIn('strip_prefix="$(cygpath -m "$PREFIX")"', contents)
        self.assertIn('$STRIP "$strip_prefix"/bin/fstar.exe', contents)
        self.assertNotIn('"$strip_prefix"/bin/*', contents)
        self.assertEqual((self.root / 'Makefile').read_text(), 'FSTAR_LIB=$(call cygpath,ulib)\n' * 2)
        self.assertIn('--prefix=$(call cygpath,$(PREFIX))', (self.root / 'mk/stage.mk').read_text())
        self.assertIn('DUNE=flock $(LOCKFILE) "$${INSTALL_FSTAR_PYTHON}"',
                      (self.root / 'mk/stage.mk').read_text())
        self.assertIn('windows-dune "$${INSTALL_FSTAR_SOURCE}"',
                      (self.root / 'mk/stage.mk').read_text())
        self.assertEqual((self.root / 'mk/stage.mk').read_text(),
                         (self.root / 'stage3/Makefile').read_text())
        self.assertIn('$(call maybe_cygwin_path,$(abspath $(value $(strip $1))))',
                      (self.root / 'pulse/mk/common.mk').read_text())
        self.assertIn('FSTAR_ROOT := $(call maybe_cygwin_path,$(FSTAR_ROOT))',
                      (self.root / 'pulse/mk/common.mk').read_text())
        self.assertIn('cygpath -m "$(shell $(FSTAR_EXE) --locate_lib)"',
                      (self.root / 'pulse/mk/boot.mk').read_text())

    def test_windows_link_target_becomes_directory_after_extraction(self):
        link = self.root / 'fstarc.ml'
        link.symlink_to('generated', target_is_directory=False)
        outer = self.root / 'outer.ml'
        outer.symlink_to('fstarc.ml', target_is_directory=False)
        generated = self.root / 'generated'
        generated.mkdir()
        (generated / 'Example.ml').write_text('let value = 42\n')
        listing = ''.join('120000 ' + 'a' * 40 + ' 0\t' + name + '\0'
                          for name in ('outer.ml', 'fstarc.ml'))
        with patch.object(build, 'run', return_value=listing):
            build.windows_links(self.root)
        self.assertTrue(link.is_dir())
        self.assertEqual((link / 'Example.ml').read_text(), 'let value = 42\n')
        self.assertEqual((outer / 'Example.ml').read_text(), 'let value = 42\n')

    def test_runtime_bundling_excludes_system_dlls_and_supports_static_gmp(self):
        prefix = self.root / 'fstar'
        destination = prefix / 'bin'
        destination.mkdir(parents=True)
        system = self.root / 'Windows'
        system.mkdir()
        dependencies = self.root / 'compiler-runtime'
        dependencies.mkdir()
        api_set = dependencies / 'api-ms-win-crt-runtime-l1-1-0.dll'
        for path in (destination / 'fstar.exe', system / 'kernel32.dll', dependencies / 'libwinpthread-1.dll', api_set):
            path.write_bytes(b'fixture binary')
        files = [destination / 'fstar.exe', system / 'kernel32.dll', dependencies / 'libwinpthread-1.dll', api_set]
        with patch.dict(os.environ, {'SystemRoot': str(system)}), \
             patch.object(build, 'linked_libraries', return_value=files):
            build.windows_runtime(prefix)
        self.assertTrue((destination / 'libwinpthread-1.dll').is_file())
        self.assertFalse((destination / 'kernel32.dll').exists())
        self.assertFalse((destination / 'libgmp-10.dll').exists())
        self.assertFalse((destination / api_set.name).exists())


if __name__ == '__main__':
    unittest.main()
