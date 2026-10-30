import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import install


SHA = "479825350e902c868b7b8c2ad918eaac073f3494"
DIGEST = "sha256:" + "a" * 64


def release(name):
    return {"assets": [{"name": name, "id": 123, "digest": DIGEST,
                        "browser_download_url": "https://github.com/example/package.tar.gz"}]}


class ResolutionTests(unittest.TestCase):
    def test_release_and_platforms(self):
        for system, machine in [("Linux", "x86_64"), ("Linux", "aarch64"),
                                ("Darwin", "arm64"), ("Darwin", "x86_64")]:
            with self.subTest(system=system, machine=machine), patch.object(
                install, "api", side_effect=[release(f"fstar-v2026.10.25-{system}-{machine}.tar.gz"),
                                             {"sha": SHA}]
            ) as api:
                result = install.select("v2026.10.25", system, machine)
                self.assertFalse(result["source"])
                self.assertEqual(result["digest"], DIGEST)
                self.assertEqual(result["commit"], SHA)
                self.assertEqual(api.call_args.args, ("FStarLang/FStar", "commits/v2026.10.25"))

    def test_nightly_date_and_tag(self):
        for selector in ("2026-10-18", "nightly-2026-10-18"):
            with self.subTest(selector=selector), patch.object(
                install, "api", side_effect=[release("fstar-Linux-x86_64.tar.gz"), {"sha": SHA}]
            ) as api:
                result = install.select(selector, "Linux", "x86_64")
                self.assertEqual(result["version"], "nightly-2026-10-18")
                self.assertEqual(api.call_args.args, ("FStarLang/FStar-nightly", "commits/nightly-2026-10-18"))

    def test_commit_and_abbreviation(self):
        for ref in (SHA, SHA[:8]):
            with self.subTest(ref=ref), patch.object(install, "api", return_value={"sha": SHA}) as api:
                result = install.select(ref, "Linux", "x86_64")
                self.assertTrue(result["source"])
                self.assertEqual(result["version"], SHA)
                api.assert_called_once_with("FStarLang/FStar", f"commits/{ref}")

    def test_branch_resolves_to_exact_commit(self):
        with patch.object(install, "api", side_effect=[None, {"sha": SHA}]) as api:
            result = install.select("feature/fstar1", "Linux", "x86_64")
        self.assertTrue(result["source"])
        self.assertEqual(result["commit"], SHA)
        self.assertEqual(api.call_args.args[1], "commits/feature%2Ffstar1")

    def test_missing_binary_does_not_silently_build(self):
        with patch.object(install, "api", side_effect=[release("fstar-src.tar.gz"), {"sha": SHA}]):
            with self.assertRaisesRegex(ValueError, "has no fstar-v"):
                install.select("v2026.10.25", "Linux", "x86_64")

    def test_invalid_inputs_do_not_make_network_requests(self):
        for version in ("", "$(touch bad)", "x\ncommit=bad", "2026-02-30"):
            with self.subTest(version=version), patch.object(install, "api") as api:
                with self.assertRaises(ValueError):
                    install.select(version, "Linux", "x86_64")
                api.assert_not_called()

    def test_source_build_accepts_supported_platforms(self):
        with patch.object(install, "api", return_value={"sha": SHA}):
            for system, machine in [('Darwin', 'arm64'), ('Windows', 'AMD64'), ('Linux', 'aarch64')]:
                self.assertTrue(install.select(SHA, system, machine)['source'])

    def test_windows_release_selects_zip(self):
        with patch.object(install, 'api', side_effect=[release('fstar-v2026.10.25-Windows_NT-x86_64.zip'), {'sha': SHA}]):
            self.assertFalse(install.select('v2026.10.25', 'Windows', 'AMD64')['source'])

    def test_platform_aliases_and_unsupported_platforms(self):
        for system in ('Windows', 'CYGWIN_NT-10.0', 'MSYS_NT-10.0'):
            self.assertEqual(install.platform_names(system, 'AMD64'), ('Windows_NT', 'x86_64'))
        self.assertEqual(install.platform_names('Linux', 'arm64'), ('Linux', 'aarch64'))
        self.assertEqual(install.platform_names('Darwin', 'aarch64'), ('Darwin', 'arm64'))
        for system, machine in [('Windows', 'ARM64'), ('Linux', 'riscv64'), ('FreeBSD', 'x86_64')]:
            with self.assertRaises(ValueError):
                install.platform_names(system, machine)

    def test_native_prefers_portable_source_package(self):
        for ref, name in [('2026-10-18', 'fstar-src.tar.gz'), ('v2026.10.25', 'fstar-v2026.10.25-src.tar.gz')]:
            with patch.object(install, 'api', side_effect=[release(name), {'sha': SHA}]):
                selected = install.select(ref, 'Windows', 'AMD64', native=True)
            self.assertTrue(selected['source'])
            self.assertIn('source_url', selected)
            self.assertEqual(selected['source_digest'], DIGEST)
            self.assertNotIn('digest', selected)

    def test_native_rebuilds_releases_and_nightlies_even_without_platform_assets(self):
        for selector in ("v2026.10.25", "2026-10-18", "nightly-2026-10-18"):
            with self.subTest(selector=selector), patch.object(
                install, "api", side_effect=[{"assets": []}, {"sha": SHA}]
            ):
                result = install.select(selector, "Darwin", "arm64", native=True)
                self.assertTrue(result["source"])
                self.assertTrue(result["native"])
                self.assertEqual(result["commit"], SHA)
                self.assertNotIn("url", result)
                self.assertNotIn("digest", result)

    def test_only_404_allows_fallback(self):
        for status in (401, 403, 404):
            with self.subTest(status=status), patch("urllib.request.urlopen", side_effect=
                urllib.error.HTTPError("https://api.github.com", status, "error", {}, None)):
                if status == 404:
                    self.assertIsNone(install.api("FStarLang/FStar", "releases/tags/missing", missing_ok=True))
                else:
                    with self.assertRaisesRegex(RuntimeError, str(status)):
                        install.api("FStarLang/FStar", "releases/tags/missing", missing_ok=True)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "fstar.tar.gz"
        self.env = {key: str(self.root / key) for key in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT")}
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def package(self, prefix="./fstar", sha=SHA, solver=True, extra=None):
        with tarfile.open(self.archive, "w:gz") as archive:
            contents = {f"{prefix}/bin/fstar.exe": f"#!/bin/sh\nprintf 'F*\\ncommit={sha}\\n'\n"}
            if solver:
                contents[f"{prefix}/lib/fstar/z3-5.3.0/bin/z3"] = "#!/bin/sh\necho Z3\n"
            for name, content in contents.items():
                member = tarfile.TarInfo(name)
                data = content.encode()
                member.size, member.mode = len(data), 0o755
                archive.addfile(member, io.BytesIO(data))
            if extra:
                archive.addfile(extra)
        return {"archive": str(self.archive), "work": str(self.root), "commit": SHA,
                "source": False, "digest": "sha256:" + install.file_hash(self.archive)}

    @unittest.skipIf(os.name == 'nt', 'The fixture is a POSIX executable; Windows executes real packages in integration tests')
    def test_real_extraction_execution_and_environment(self):
        for prefix in ("./fstar", "fstar"):
            with self.subTest(prefix=prefix):
                state = self.package(prefix)
                work = self.root / prefix.replace("/", "_")
                work.mkdir()
                state["work"] = str(work)
                install.install(state)
                exe = work / "installed/fstar/bin/fstar.exe"
                self.assertTrue(exe.is_file())
                self.assertIn(f"FSTAR_EXE={exe}\n", Path(self.env["GITHUB_ENV"]).read_text())
                self.assertIn(str(exe.parent) + "\n", Path(self.env["GITHUB_PATH"]).read_text())

    def test_digest_mismatch_stops_before_extraction(self):
        state = self.package()
        state["digest"] = DIGEST
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            install.install(state)
        self.assertFalse((self.root / "installed").exists())

    def test_wrong_commit_and_missing_solver_never_export_environment(self):
        for sha, solver in (("0" * 40, True), (SHA, False)):
            with self.subTest(sha=sha, solver=solver):
                state = self.package(sha=sha, solver=solver)
                work = self.root / f"work-{sha}-{solver}"
                work.mkdir()
                state["work"] = str(work)
                with patch('subprocess.check_output', return_value=f'F*\ncommit={sha}\n'), self.assertRaises(ValueError):
                    install.install(state)
                self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())

    def test_traversal_absolute_paths_and_links_are_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/tmp/escape", tarfile.REGTYPE),
                           ("fstar/link", tarfile.SYMTYPE), ("fstar/link", tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind):
                entry = tarfile.TarInfo(name)
                entry.type, entry.linkname = kind, "../../escape"
                self.package(extra=entry)
                with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                    install.extract(self.archive, self.root / "extract")

    def test_zip_install_exports_portable_paths_and_accepts_solver_exe(self):
        with zipfile.ZipFile(self.archive, 'w') as package:
            package.writestr('fstar/bin/fstar.exe', b'executable fixture')
            package.writestr('fstar/lib/fstar/z3-5.3.0/bin/z3.exe', b'solver fixture')
        state = {'archive': str(self.archive), 'work': str(self.root), 'commit': SHA, 'source': True}
        with patch('subprocess.check_output', return_value=f'F*\ncommit={SHA}\n'):
            install.install(state)
        expected = self.root / 'installed/fstar/bin/fstar.exe'
        self.assertEqual(Path(self.env['GITHUB_ENV']).read_text(), f'FSTAR_EXE={expected.as_posix()}\n')

    def test_zip_paths_and_symlinks_are_rejected(self):
        for name in ('../escape', '/escape', 'C:/escape', 'C:escape', 'fstar\\..\\escape', 'fstar/link'):
            with self.subTest(name=name), zipfile.ZipFile(self.archive, 'w') as package:
                info = zipfile.ZipInfo(name)
                if name == 'fstar/link':
                    info.external_attr = 0o120777 << 16
                package.writestr(info, '../../escape')
            with self.assertRaisesRegex(ValueError, 'Unsafe archive'):
                install.unpack(self.archive, self.root / 'unpack')

    def test_native_rejects_the_wrong_ocaml_compiler(self):
        state = self.package()
        state.update(native=True, ocaml='5.4.0')
        with patch('subprocess.check_output', return_value=f'F*\ncommit={SHA}\ncompiler=OCaml 5.3.0\n'):
            with self.assertRaisesRegex(ValueError, 'active OCaml compiler'):
                install.install(state)
        self.assertFalse(Path(self.env['GITHUB_ENV']).exists())

    def test_cache_identity_and_path_are_stable_but_installations_are_separate(self):
        env = {"RUNNER_TEMP": str(self.root), "INSTALL_FSTAR_VERSION": "fstar1",
               "INSTALL_FSTAR_OCAML": "5.3.0", "INSTALL_FSTAR_CACHE": "true", "INSTALL_FSTAR_NATIVE": "false"}
        selected = {"source": True, "commit": SHA, "version": SHA, "repository": install.REPOSITORY}
        states = []
        with patch.dict(os.environ, env), patch.object(install, "select", side_effect=lambda *a, **kw: selected.copy()), \
             patch("platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}):
            for ocaml in ("5.3.0", "5.3.0", "4.14.2"):
                os.environ["INSTALL_FSTAR_OCAML"] = ocaml
                install.resolve()
                output = Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines()
                state_path = [line.removeprefix("state=") for line in output if line.startswith("state=")][-1]
                states.append(json.loads(Path(state_path).read_text()))
        self.assertEqual(states[0]["archive"], states[1]["archive"])
        self.assertEqual(states[0]["cache-key"], states[1]["cache-key"])
        self.assertNotEqual(states[0]["work"], states[1]["work"])
        self.assertNotEqual(states[0]["cache-key"], states[2]["cache-key"])

    def test_native_captures_active_switch_and_ignores_cache_and_compiler_inputs(self):
        env = {"RUNNER_TEMP": str(self.root), "INSTALL_FSTAR_VERSION": "v2026.10.25",
               "INSTALL_FSTAR_OCAML": "ignored", "INSTALL_FSTAR_CACHE": "true", "INSTALL_FSTAR_NATIVE": "true"}
        selected = {"source": True, "native": True, "commit": SHA, "version": "v2026.10.25", "repository": install.REPOSITORY}
        with patch.dict(os.environ, env), patch.object(install, "select", return_value=selected), \
             patch("subprocess.check_output", side_effect=["/caller/opam\n", "/caller/project\n", "5.4.0\n"]):
            install.resolve()
        outputs = dict(line.split("=", 1) for line in Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines())
        state = json.loads(Path(outputs["state"]).read_text())
        self.assertEqual(outputs["cache"], "false")
        self.assertEqual(outputs["source"], "true")
        self.assertEqual(state["opamroot"], "/caller/opam")
        self.assertEqual(state["opamswitch"], "/caller/project")
        self.assertEqual(state["ocaml"], "5.4.0")

    def test_native_without_opam_reports_setup_requirement(self):
        env = {"INSTALL_FSTAR_VERSION": SHA, "INSTALL_FSTAR_CACHE": "true", "INSTALL_FSTAR_NATIVE": "true"}
        with patch.dict(os.environ, env), patch.object(install, "select", return_value={"source": True}), \
             patch("subprocess.check_output", side_effect=FileNotFoundError("opam")):
            with self.assertRaisesRegex(ValueError, "requires an active OPAM switch"):
                install.resolve()
        self.assertFalse(Path(self.env["GITHUB_OUTPUT"]).exists())


if __name__ == "__main__":
    unittest.main()
