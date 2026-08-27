import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


class ReleaseArchiveVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = Path(__file__).resolve().parents[1] / "scripts" / "verify-release-archive.py"

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, archive: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), str(archive)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def make_archive(self, name: str, members) -> Path:
        target = self.root / name
        with tarfile.open(target, "w:gz") as archive:
            for member, content in members:
                archive.addfile(member, io.BytesIO(content) if content is not None else None)
        return target

    def test_accepts_one_normal_release_root(self):
        directory = tarfile.TarInfo("ppflight-pdf-agent-1.0")
        directory.type = tarfile.DIRTYPE
        payload = b"release"
        file_info = tarfile.TarInfo("ppflight-pdf-agent-1.0/README.md")
        file_info.size = len(payload)
        result = self.verify(self.make_archive("valid.tar.gz", [(directory, None), (file_info, payload)]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ppflight-pdf-agent-1.0")

    def test_rejects_traversal_and_links(self):
        directory = tarfile.TarInfo("ppflight-pdf-agent-1.0")
        directory.type = tarfile.DIRTYPE
        traversal = tarfile.TarInfo("ppflight-pdf-agent-1.0/../../etc/passwd")
        traversal.size = 1
        result = self.verify(self.make_archive("traversal.tar.gz", [(directory, None), (traversal, b"x")]))
        self.assertEqual(result.returncode, 2)

        link = tarfile.TarInfo("ppflight-pdf-agent-1.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        result = self.verify(self.make_archive("link.tar.gz", [(directory, None), (link, None)]))
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
