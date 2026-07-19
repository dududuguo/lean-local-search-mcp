import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools import validate_release


class ReleaseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_archive(self, name="HighDimProb-alpha-0.1/README.md", content=b"fixture\n"):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, digest

    @staticmethod
    def copying_downloader(source):
        def download(_url, destination):
            shutil.copyfile(source, destination)
        return download

    def test_downloads_verified_archive_then_reuses_cache(self):
        archive, digest = self.make_archive()
        cache = self.root / "cache"

        fixture, action = validate_release.prepare_fixture(
            cache, expected_sha256=digest, downloader=self.copying_downloader(archive)
        )

        self.assertEqual(action, "downloaded")
        self.assertEqual((fixture / "README.md").read_text(), "fixture\n")

        def fail_downloader(_url, _destination):
            raise AssertionError("valid cache must not use the network")

        reused, action = validate_release.prepare_fixture(
            cache, expected_sha256=digest, downloader=fail_downloader
        )
        self.assertEqual(action, "reused")
        self.assertEqual(reused, fixture)

    def test_rejects_corrupt_download(self):
        archive, digest = self.make_archive()
        archive.write_bytes(b"corrupt")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            validate_release.prepare_fixture(
                self.root / "cache",
                expected_sha256=digest,
                downloader=self.copying_downloader(archive),
            )

    def test_rejects_tar_path_traversal(self):
        archive, digest = self.make_archive("../escape.txt")

        with self.assertRaisesRegex(ValueError, "unsafe archive member"):
            validate_release.prepare_fixture(
                self.root / "cache",
                expected_sha256=digest,
                downloader=self.copying_downloader(archive),
            )

    def test_rejects_windows_style_tar_path_traversal(self):
        archive, digest = self.make_archive("HighDimProb-alpha-0.1/..\\escape.txt")

        with self.assertRaisesRegex(ValueError, "unsafe archive member"):
            validate_release.prepare_fixture(
                self.root / "cache",
                expected_sha256=digest,
                downloader=self.copying_downloader(archive),
            )

    def test_rejects_tar_special_files(self):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("HighDimProb-alpha-0.1/device")
            info.type = tarfile.CHRTYPE
            tf.addfile(info)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ValueError, "unsafe archive member"):
            validate_release.prepare_fixture(
                self.root / "cache",
                expected_sha256=digest,
                downloader=self.copying_downloader(archive),
            )

    def test_repairs_modified_extracted_cache_from_verified_archive(self):
        archive, digest = self.make_archive()
        cache = self.root / "cache"
        fixture, _ = validate_release.prepare_fixture(
            cache, expected_sha256=digest, downloader=self.copying_downloader(archive)
        )
        (fixture / "README.md").write_text("modified\n")

        repaired, action = validate_release.prepare_fixture(
            cache, expected_sha256=digest, downloader=self.copying_downloader(archive)
        )

        self.assertEqual(action, "repaired")
        self.assertEqual((repaired / "README.md").read_text(), "fixture\n")

    def test_cache_lock_serializes_callers(self):
        cache = self.root / "cache"
        active = 0
        maximum = 0
        guard = threading.Lock()

        def worker():
            nonlocal active, maximum
            with validate_release.cache_lock(cache):
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 1)

    def test_windows_lock_retries_contention(self):
        attempts = []

        def fake_locking(_fd, _mode, _size):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("busy")

        with tempfile.TemporaryFile() as lock_file:
            validate_release.lock_windows_file(
                lock_file,
                fake_locking,
                nonblocking_mode=1,
                timeout=1,
                retry_interval=0,
            )
        self.assertEqual(len(attempts), 3)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_source_manifest_rejects_special_files(self):
        source = self.root / "source"
        source.mkdir()
        os.mkfifo(source / "unexpected.fifo")

        with self.assertRaisesRegex(ValueError, "unsafe cached fixture entry"):
            validate_release.source_manifest(source)

    def test_project_name_canonicalizes_macos_temp_alias(self):
        server = validate_release.load_server()
        raw = Path(tempfile.mkdtemp(prefix="project-name-regression-")) / "ProjectB"
        self.addCleanup(shutil.rmtree, raw.parent, True)
        self.assertEqual(server.project_name(raw), server.project_name(raw.resolve()))

    def test_named_assertion_reports_invariant(self):
        with self.assertRaisesRegex(AssertionError, "indexed file baseline"):
            validate_release.require(False, "indexed file baseline", {"indexed_files": 1})


if __name__ == "__main__":
    unittest.main()
