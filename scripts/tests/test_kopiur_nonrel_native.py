"""Host-only parsing/admission regressions, never native engine proof."""
import io
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_nonrel_native as native


class AdmissionTests(unittest.TestCase):
    def test_unknown_service(self):
        with self.assertRaises(ValueError):
            native.Fixture('production')

    def test_host_runtime_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': ''}):
            with self.assertRaises(RuntimeError):
                native.Fixture('dragonfly')

    def test_other_scale_set_denied(self):
        with patch.dict(native.os.environ, {'RUNNER_NAME': 'ghar-set-maudecode-fixture'}):
            with self.assertRaises(RuntimeError):
                native.Fixture('rabbitmq-server')

    def test_images_are_pinned(self):
        self.assertEqual(set(native.IMAGES), {'dragonfly', 'elasticsearch', 'rabbitmq-server'})
        for image in native.IMAGES.values():
            self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')


class RespTests(unittest.TestCase):
    def test_binary_bulk(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'$3\r\n\x00\xffx\r\n')), b'\x00\xffx')

    def test_nested_array(self):
        self.assertEqual(native.resp_read(io.BytesIO(b'*3\r\n+OK\r\n:2\r\n$-1\r\n')), [b'OK', 2, None])

    def test_server_error_suppresses_content(self):
        with self.assertRaises(RuntimeError) as caught:
            native.resp_read(io.BytesIO(b'-fixture-password\r\n'))
        self.assertNotIn('fixture-password', str(caught.exception))

    def test_invalid_frames(self):
        frames = (b'$-2\r\n', b'$1048577\r\n', b'*4097\r\n', b'$3\r\nx\r\n',
                  b'$1\r\nxzz', b'hello\r\n', b'+truncated', b'*' + b'1\r\n*' * 17 + b'0\r\n')
        for frame in frames:
            with self.subTest(frame=frame[:20]), self.assertRaises(ValueError):
                native.resp_read(io.BytesIO(frame))


class ArchiveTests(unittest.TestCase):
    def archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                if kind == tarfile.REGTYPE:
                    member.size = 3
                    archive.addfile(member, io.BytesIO(b'abc'))
                else:
                    archive.addfile(member)
        return stream.getvalue()

    def test_regular_archive(self):
        self.assertRegex(native.validate_archive(self.archive([('fixture', tarfile.REGTYPE)])), r'^[0-9a-f]{64}$')

    def test_unsafe_members(self):
        entries = ([('/outside', tarfile.REGTYPE)], [('../outside', tarfile.REGTYPE)],
                   [('link', tarfile.SYMTYPE)], [('link', tarfile.LNKTYPE)],
                   [('pipe', tarfile.FIFOTYPE)], [('duplicate', tarfile.REGTYPE)] * 2)
        for members in entries:
            with self.subTest(members=members), self.assertRaises(ValueError):
                native.validate_archive(self.archive(members))

    def test_empty_archive(self):
        with self.assertRaises(ValueError):
            native.validate_archive(self.archive([]))

    def test_archive_size_bound(self):
        with patch.object(native, 'MAX_ARCHIVE', 1):
            with self.assertRaises(ValueError):
                native.validate_archive(b'xx')


if __name__ == '__main__':
    unittest.main()
