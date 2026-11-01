import base64
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib

import vision


def png(width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    data = b'\x89PNG\r\n\x1a\n'
    data += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    data += chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * width) * height))
    data += chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


class VisionTests(unittest.TestCase):
    @unittest.skipUnless(Path('/usr/bin/sips').exists(), 'macOS image preparation')
    def test_small_large_and_narrow_images_fit_jev_limits(self):
        for width, height in ((20, 40), (2000, 1800), (30, 2000)):
            with self.subTest(width=width, height=height):
                prepared = vision.prepare(png(width, height))
                self.assertTrue(prepared.startswith('data:image/jpeg;base64,'))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'image.jpg'
                    path.write_bytes(base64.b64decode(prepared.split(',')[1]))
                    output = subprocess.check_output(['/usr/bin/sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(path)], text=True)
                    dimensions = [int(line.split(':')[1]) for line in output.splitlines() if 'pixel' in line]
                self.assertTrue(all(256 <= n <= 1568 for n in dimensions))
                self.assertTrue(78400 <= dimensions[0] * dimensions[1] <= 2458624)

    def test_invalid_image_returns_safe_error(self):
        with self.assertRaisesRegex(RuntimeError, 'Не удалось подготовить'):
            vision.prepare('data:image/png;base64,bad')


if __name__ == '__main__':
    unittest.main()
