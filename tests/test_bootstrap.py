"""Exercise the single-command workflow without installing packages or services."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
VERSION = subprocess.check_output([sys.executable, str(SOURCE / 'agent.py'), 'version'], text=True).strip()


class BootstrapTests(unittest.TestCase):
    def run_install(self, *, corrupt=False, traversal=False, installed='', repeat=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / 'release'
            release.mkdir()
            app = root / 'current'
            config = root / 'config.json'
            trace = root / 'trace'
            install = '''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >>"$TEST_TRACE"
mkdir -p "$TEST_APP/.venv/bin" "$TEST_APP/scripts"
printf '#!/usr/bin/env bash\\nprintf "''' + VERSION + '''\\\\n"\\n' >"$TEST_APP/.venv/bin/python"
chmod +x "$TEST_APP/.venv/bin/python"
printf '{}' >"$TEST_APP/agent.py"
cat >"$TEST_APP/scripts/status-report.py" <<'PYREPORT'
import json
print(json.dumps({"binding": "unbound"}))
PYREPORT
printf '{"tunnel_port":9761}' >"$TEST_CONFIG"
'''
            archive = release / ('ppflight-pdf-agent-' + VERSION + '.tar.gz')
            base = 'ppflight-pdf-agent-' + VERSION
            with tarfile.open(archive, 'w:gz') as tar:
                directory = tarfile.TarInfo(base)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                tar.addfile(directory)
                for name, data in [('install.sh', install.encode()), ('renderer/vendor/autoload.php', b'<?php')]:
                    member = tarfile.TarInfo(base + '/' + name)
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
                if traversal:
                    member = tarfile.TarInfo(base + '/../escaped')
                    tar.addfile(member, io.BytesIO(b''))
            checksum = '0' * 64 if corrupt else hashlib.sha256(archive.read_bytes()).hexdigest()
            (release / (archive.name + '.sha256')).write_text(checksum + '  ' + archive.name + '\n')
            binary = root / 'bin'
            binary.mkdir()
            # HTTPS fetching is intercepted; the tested script still validates
            # the real compressed archive, checksum and extraction boundaries.
            (binary / 'curl').write_text('''#!/usr/bin/env bash
set -eu
url=''; dest=''
while [[ $# -gt 0 ]]; do
 case "$1" in https://*) url="$1";; -o) shift; dest="$1";; esac
 shift
done
cp "$TEST_RELEASE/${url##*/}" "$dest"
''')
            (binary / 'systemctl').write_text('#!/usr/bin/env bash\nexit 0\n')
            for script in binary.iterdir():
                script.chmod(0o755)
            source = (SOURCE / 'bootstrap.sh').read_text()
            source = source.replace('readonly APP_CURRENT=/opt/ppflight-pdf-agent/current', 'readonly APP_CURRENT=' + str(app))
            source = source.replace('readonly CONFIG_PATH=/etc/ppflight-pdf-agent/config.json', 'readonly CONFIG_PATH=' + str(config))
            source = source.replace('[[ ${EUID} -eq 0 ]]', 'true').replace('[[ -d /run/systemd/system ]]', 'true')
            source = source.replace('mktemp -d /var/tmp/ppflight-pdf-setup.XXXXXX', 'mktemp -d ' + str(root / 'work.XXXXXX'))
            entry = root / 'bootstrap.sh'
            entry.write_text(source)
            if installed:
                (app / '.venv/bin').mkdir(parents=True)
                (app / '.venv/bin/python').write_text('#!/usr/bin/env bash\nprintf "%s\\n" "' + installed + '"\n')
                (app / '.venv/bin/python').chmod(0o755)
                (app / 'agent.py').touch()
            env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'], TEST_APP=str(app), TEST_TRACE=str(trace), TEST_CONFIG=str(config), TEST_RELEASE=str(release))
            result = subprocess.run(['bash', str(entry), '--skip-bind'], env=env, capture_output=True, text=True)
            if repeat and result.returncode == 0:
                result = subprocess.run(['bash', str(entry), '--skip-bind'], env=env, capture_output=True, text=True)
            calls = trace.read_text() if trace.exists() else ''
            self.assertFalse((root / 'escaped').exists())
            self.assertEqual(list(root.glob('work.*')), [], result.stderr)
            return result, calls

    def test_fresh_install_and_repeat_preserve_installation(self):
        result, calls = self.run_install(repeat=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls.splitlines()), 1)
        self.assertIn('--artifact-dir /var/lib/ppflight-pdf-agent/artifacts', calls)
        self.assertIn('http://127.0.0.1:9761', result.stderr)

    def test_upgrade_preserves_installed_artifact_directory(self):
        result, calls = self.run_install(installed='1.0.6')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--artifact-dir', calls)

    def test_downgrade_is_rejected(self):
        result, calls = self.run_install(installed='1.0.13')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')

    def test_checksum_failure_never_runs_installer(self):
        result, calls = self.run_install(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')

    def test_traversal_never_runs_installer(self):
        result, calls = self.run_install(traversal=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')
