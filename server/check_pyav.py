import av
import sys

def test():
    codec = av.CodecContext.create("h264", "r")
    frames = 0
    with open("test.h264", "rb") as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            for packet in codec.parse(chunk):
                for frame in codec.decode(packet):
                    frames += 1
    print(f"Decoded {frames} frames")
test()
