import av
import sys

codec = av.CodecContext.create("h264", "r")
frames = 0
with open("test.h264", "rb") as f:
    # First chunk is SPS/PPS maybe?
    # Let's read small chunks of random sizes
    while True:
        chunk = f.read(500) # simulating websocket payloads
        if not chunk:
            break
        for packet in codec.parse(chunk):
            for frame in codec.decode(packet):
                frames += 1

print("Total frames:", frames)
