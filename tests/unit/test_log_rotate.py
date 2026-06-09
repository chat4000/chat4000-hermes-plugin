"""10 MB log rotation — port of TS log-rotate semantics."""

from __future__ import annotations

from chat4000_hermes_plugin.log_rotate import LOG_MAX_BYTES, rotate_log_if_oversized


class TestRotate:
    def test_no_op_below_cap(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_bytes(b"hello")
        rotate_log_if_oversized(path, 10)
        assert path.exists()
        assert path.read_bytes() == b"hello"

    def test_no_op_when_file_missing(self, tmp_path):
        path = tmp_path / "missing.txt"
        # Should NOT raise.
        rotate_log_if_oversized(path, 100)

    def test_rotates_when_over_cap(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_bytes(b"x" * (LOG_MAX_BYTES + 100))
        rotate_log_if_oversized(path, 1)
        assert not path.exists()
        assert (tmp_path / "log.txt.1").exists()

    def test_rotation_replaces_existing_archive(self, tmp_path):
        path = tmp_path / "log.txt"
        archive = tmp_path / "log.txt.1"
        archive.write_bytes(b"old archive")
        path.write_bytes(b"y" * (LOG_MAX_BYTES + 100))
        rotate_log_if_oversized(path, 1)
        # The pre-existing archive is overwritten by the rotated file.
        assert archive.exists()
        # New archive contains the new content (the y's), not the old "old archive".
        assert b"y" in archive.read_bytes()
        assert b"old archive" not in archive.read_bytes()

    def test_custom_cap(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_bytes(b"a" * 100)
        rotate_log_if_oversized(path, 50, max_bytes=120)
        # 100 + 50 = 150 > 120 → rotates.
        assert not path.exists()
        assert (tmp_path / "log.txt.1").exists()

    def test_pending_zero_uses_just_file_size(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_bytes(b"x" * (LOG_MAX_BYTES + 1))
        rotate_log_if_oversized(path, 0)
        # 10MB + 1 > 10MB → rotates.
        assert (tmp_path / "log.txt.1").exists()
