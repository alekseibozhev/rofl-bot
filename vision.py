"""Prepare Telegram images for Jev's PNG/JPEG and dimension limits on macOS."""
from base64 import b64decode, b64encode
from pathlib import Path
import re
import subprocess
import tempfile


def prepare(image):
    """Fit within 1280px, pad small sides to 280px, and encode a static JPEG."""
    try:
        data = b64decode(image.split(',', 1)[1], validate=True)
        with tempfile.TemporaryDirectory(prefix='rofl-vision-') as directory:
            source = Path(directory) / 'source'
            target = Path(directory) / 'image.jpg'
            source.write_bytes(data)

            def sips(*args):
                return subprocess.run(['/usr/bin/sips', *map(str, args)], check=True,
                                      capture_output=True, text=True, timeout=15).stdout

            sips('-s', 'format', 'jpeg', '--resampleHeightWidthMax', 1280, source, '--out', target)
            dimensions = sips('-g', 'pixelWidth', '-g', 'pixelHeight', target)
            width = int(re.search(r'pixelWidth: (\d+)', dimensions)[1])
            height = int(re.search(r'pixelHeight: (\d+)', dimensions)[1])
            if min(width, height) < 280:
                sips('--padToHeightWidth', max(280, height), max(280, width), target, '--out', target)
            return 'data:image/jpeg;base64,' + b64encode(target.read_bytes()).decode()
    except (OSError, ValueError, IndexError, TypeError, subprocess.SubprocessError):
        raise RuntimeError('Не удалось подготовить изображение для Jev') from None
