import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class AptTransportTests(unittest.TestCase):
    def test_https_overrides_preserve_system_sources_and_cleanup(self):
        lib = (SOURCE / 'scripts/lib.sh').read_text()
        wrapper = lib[lib.index('apt_for_installer() ('):lib.index('\ninstall_apt_dependencies()')]
        bootstrap = (SOURCE / 'bootstrap.sh').read_text()
        self.assertIn(wrapper.strip().replace('die "', 'fail "'), bootstrap)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            etc = root / 'apt'
            parts = etc / 'sources.list.d'
            parts.mkdir(parents=True)
            main = etc / 'sources.list'
            main.write_text('deb http://us.archive.ubuntu.com/ubuntu noble main\n')
            sources = parts / 'ubuntu.sources'
            sources.write_text('Types: deb\nURIs: http://security.ubuntu.com/ubuntu\nSuites: noble-security\nComponents: main\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
            third = parts / 'custom.list'
            third.write_text('deb https://packages.example.test stable main\n')
            before = {p: p.read_bytes() for p in [main, sources, third]}
            ca = root / 'ca.crt'
            ca.write_text('test CA presence marker; no requests are sent')
            wrapper = wrapper.replace('/etc/apt', str(etc)).replace('/etc/ssl/certs/ca-certificates.crt', str(ca))
            wrapper = wrapper.replace('/var/tmp/ppflight-pdf-apt.XXXXXX', str(root / 'private.XXXXXX'))
            apt_config = f'Dir::Etc "{etc}";\nDir::Etc::sourcelist "sources.list";\nDir::Etc::sourceparts "sources.list.d";'
            program = 'set -Eeuo pipefail\nnote(){ :; }\ndie(){ exit 1; }\n'
            program += "apt-config(){ printf '%s\\n' " + shlex.quote(apt_config) + '; }\n'
            program += '''apt-get(){
 for item in "$@"; do
   case "$item" in
     Dir::Etc::sourcelist=*) cat "${item#*=}";;
     Dir::Etc::sourceparts=*) cat "${item#*=}"/*;;
   esac
 done
}
'''
            result = subprocess.run(['bash', '-c', program + wrapper + '\napt_for_installer update'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('https://us.archive.ubuntu.com/ubuntu noble main', result.stdout)
            self.assertIn('https://security.ubuntu.com/ubuntu', result.stdout)
            self.assertIn('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg', result.stdout)
            self.assertIn(third.read_text().strip(), result.stdout)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertEqual(list(root.glob('private.*')), [])
