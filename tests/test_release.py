import importlib.util
import json
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('fenjue_build_release', ROOT / 'scripts/build_release.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class ReleaseBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = BUILD.entries(ROOT / 'wheelhouse')

    def test_current_cockpit_entries_and_cjs_are_in_allowlist(self):
        self.assertTrue(BUILD.REQUIRED <= self.files.keys())
        self.assertIn('tests/test_cockpit_ui.cjs', self.files)
        self.assertIn('cockpit-patch/fenjue_outbound.go', self.files)
        self.assertIn('Start-Guard.cmd', self.files)
        self.assertIn('COCKPIT.md', self.files)
        self.assertIn('historical-release-validation.json', self.files)
        self.assertNotIn('RELEASE-VALIDATION.json', self.files)

    def test_release_zip_contains_required_operational_entries(self):
        # Exercise the actual archive writer with every operational input.  The
        # wheel payload is intentionally omitted here to keep this unit test small.
        content = {name: path.read_bytes() for name, path in self.files.items() if name in BUILD.REQUIRED}
        with TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            BUILD.write_archive(content, archive)
            with zipfile.ZipFile(archive) as zf:
                names = {name.removeprefix('Fenjue-Windows/') for name in zf.namelist()}
            self.assertEqual(set(content), names)
            self.assertIn('Restore-Cockpit.cmd', names)
            self.assertIn('tests/test_cockpit_ui.cjs', names)

    def test_machine_paths_rejected_in_both_windows_slash_forms_but_docs_allowed(self):
        home = str(Path.home())
        slash = home.replace('\\', '/') + '/secret'
        backslash = home.replace('/', '\\') + '\\secret'
        for value in (slash, backslash):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    BUILD.validate_content({'scripts/example.py': value.encode()})
        BUILD.validate_content({'README.md': backslash.encode()})

    def test_dirty_tree_is_rejected_without_explicit_override(self):
        with patch.object(BUILD, 'git_status', return_value=' M scripts/example.py'):
            with patch.object(sys, 'argv', ['build_release.py', '--wheelhouse', str(ROOT / 'wheelhouse'), '--out', 'unused.zip']):
                with self.assertRaisesRegex(ValueError, 'Working tree is dirty'):
                    BUILD.main()

    def test_release_metadata_marks_validation_as_not_run(self):
        metadata = json.loads(BUILD.make_release_metadata({'README.md': b'x'}, 'abc123', False))
        self.assertFalse(metadata['source_dirty'])
        self.assertFalse(metadata['quality_advantage_proven'])
        self.assertEqual(metadata['cockpit_patch']['binary_included'], False)
        # The generated validation record is intentionally not a historical pass claim.
        current = json.loads(BUILD.make_validation_metadata('abc123', False))
        self.assertTrue(all(value == 'not_run_by_release_builder' for key, value in current.items()
                            if key.endswith('_tests') or key.endswith('_install') or key == 'live_generation'))
        self.assertEqual(current['historical_validation'], 'historical-release-validation.json')


if __name__ == '__main__':
    unittest.main()
