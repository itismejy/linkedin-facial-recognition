import av
import sys

# Let's read the first 39 bytes as config, and the rest as chunks
with open("test.h264", "rb") as f:
    config = f.read(39)
    codec = av.CodecContext.create("h264", "r")
    
    print("Parsing config")
    for packet in codec.parse(config):
        for frame in codec.decode(packet):
            print("Decoded config frame!")
            
    print("Parsing rest")
    frames = 0
    while True:
        chunk = f.read(10000)
        if not chunk:
            break
        for packet in codec.parse(chunk):
            for frame in codec.decode(packet):
                frames += 1
    print("Frames decoded:", frames)
