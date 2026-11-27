"""ICC APP2 goes behind the JFIF APP0 segment."""
from __future__ import annotations

import struct

from image_optimizer.optimizer import Optimizer


def test_icc_segment_goes_after_the_jfif_segment(tmp_path):
    p = tmp_path / "t.jpg"
    jfif = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    p.write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(jfif) + 2) + jfif + b"\xff\xd9")
    Optimizer._embed_jpeg_icc(p, b"profile-bytes")
    data = p.read_bytes()
    assert data[2:4] == b"\xff\xe0", "JFIF APP0 must stay first after SOI"
    off = 4 + struct.unpack(">H", data[4:6])[0]
    assert data[off:off + 2] == b"\xff\xe2" and b"ICC_PROFILE" in data[off:off + 20]
