"""Check that ag-pdf hands only verified GitHub release arguments to update.sh."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class AgPdfUpdateTests(unittest.TestCase):
    def run_update(self, *args, installed="1.0.0", checksum=True):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_root = root / "app"
            release = app_root / "releases" / installed
            release.mkdir(parents=True)
            (app_root / "current").symlink_to(release)
            trace = root / "update-arguments"
            update = release / "update.sh"
            update.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"$TEST_TRACE\"\n")
            update.chmod(0o755)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env bash\nset -eu\n"
                "url=\"${!#}\"\n"
                "case \"$url\" in\n"
                "  *'/releases/latest') printf '%s\\n' '{\"tag_name\":\"v1.2.3\"}' ;;\n"
                "  *.sha256)\n"
                "    if [[ \"${TEST_BAD_CHECKSUM:-}\" == 1 ]]; then printf '%s\\n' 'not-a-checksum'; "
                "else printf '%s  %s\\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' "
                "'ppflight-pdf-agent-1.2.3.tar.gz'; fi ;;\n"
                "  *) exit 12 ;;\n"
                "esac\n"
            )
            fake_curl.chmod(0o755)
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
            fake_sudo.chmod(0o755)
            wrapper = (SOURCE / "ag-pdf").read_text().replace(
                "APP_ROOT=/opt/${APP_NAME}", f"APP_ROOT={app_root}"
            )
            entry = root / "ag-pdf"
            entry.write_text(wrapper)
            entry.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_TRACE=str(trace))
            if not checksum:
                env["TEST_BAD_CHECKSUM"] = "1"
            result = subprocess.run(["bash", str(entry), "update", *args], capture_output=True, text=True, env=env)
            return result, trace.read_text() if trace.exists() else ""

    def test_default_uses_latest_release_and_verified_arguments(self):
        result, trace = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            trace.strip(),
            "--version 1.2.3 --url https://github.com/ppflight/ppflight-pdf-agent/releases/download/v1.2.3/ppflight-pdf-agent-1.2.3.tar.gz "
            "--sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_explicit_version_normalizes_v_prefix(self):
        result, trace = self.run_update("v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--version 1.2.3", trace)

    def test_invalid_checksum_never_starts_updater(self):
        result, trace = self.run_update(checksum=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace, "")

    def test_installed_latest_never_starts_updater(self):
        result, trace = self.run_update(installed="1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(trace, "")
        self.assertIn("无需升级", result.stdout)


if __name__ == "__main__":
    unittest.main()
