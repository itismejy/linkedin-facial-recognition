with open("test.h264", "rb") as f:
    config = f.read(50)
    print(config.hex())
