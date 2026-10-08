import shutil
import subprocess

import pytest
from mutagen.oggopus import OggOpus

from beets_bridge import Bridge


def test_discovery_is_written_to_comment_and_description(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    path = tmp_path / "fixture.opus"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.2", "-c:a", "libopus", str(path),
    ], check=True)
    expected = "Discovery Date: 2021-04-17--21-36-42 UTC"
    Bridge.write_discovery_tags(path, "2021-04-17T21:36:42Z")
    tags = OggOpus(path)
    assert tags["comment"] == [expected]
    assert tags["description"] == [expected]
