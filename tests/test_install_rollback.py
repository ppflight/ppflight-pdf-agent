"""Exercise the real installer rollback body/trap in a private filesystem."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class InstallRollbackTests(unittest.TestCase):
    def check_failure(self, failure):
        script = (SOURCE / 'install.sh').read_text()
        start = script.index('rollback_install() {')
        end = script.index('\n\nmkdir -p -- "${RELEASE_DIR}"', start)
        code = script[start:end]
        self.assertIn('trap rollback_install EXIT', code)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous = root / 'previous'
            previous.mkdir()
            release = root / 'new-release'
            release.mkdir()
            current = root / 'current'
            current.symlink_to(release, target_is_directory=True)
            binary = root / 'bin'
            binary.mkdir()
            unit = root / 'unit'
            unit.write_text('NEW UNIT')
            backup = root / 'unit.backup'
            backup.write_text('ORIGINAL UNIT')
            pag = root / 'pag.backup'
            wrapper = root / 'wrapper.backup'
            pag.write_text('ORIGINAL PAG')
            wrapper.write_text('ORIGINAL WRAPPER')
            values = {'UNIT_EXISTED': '1', 'PAG_EXISTED': '1', 'AG_PDF_EXISTED': '1',
                      'SERVICE_WAS_ACTIVE': '1', 'SERVICE_NAME': 'test-service',
                      'PREVIOUS_RELEASE': str(previous), 'RELEASE_DIR': str(release),
                      'CURRENT_LINK': str(current), 'UNIT_FILE': str(unit),
                      'UNIT_BACKUP': str(backup), 'PAG_BACKUP': str(pag), 'AG_PDF_BACKUP': str(wrapper)}
            program = 'set -Eeuo pipefail\nnote(){ :; }\ndie(){ exit 1; }\nsystemctl(){ :; }\n'
            program += 'atomic_symlink(){ ln -sfn -- "$1" "$2"; }\n'
            program += '\n'.join(key + '=' + shlex.quote(value) for key, value in values.items()) + '\n'
            program += code.replace('/usr/local/bin', str(binary)) + '\n' + failure
            result = subprocess.run(['bash', '-c', program], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(current.resolve(), previous)
            self.assertEqual(unit.read_text(), 'ORIGINAL UNIT')
            self.assertEqual((binary / 'pag').read_text(), 'ORIGINAL PAG')
            self.assertEqual((binary / 'ag-pdf').read_text(), 'ORIGINAL WRAPPER')
            self.assertFalse(release.exists())
            self.assertFalse(backup.exists())

    def test_explicit_validation_failure_restores_previous_installation(self):
        self.check_failure('die "failed post-install health check"')

    def test_command_failure_restores_previous_installation(self):
        self.check_failure('false')
