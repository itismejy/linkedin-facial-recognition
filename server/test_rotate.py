import av
import sys
import face_recognition
import cv2

with open("test.h264", "rb") as f:
    config = f.read(39)
    codec = av.CodecContext.create("h264", "r")
    
    for packet in codec.parse(config):
        for frame in codec.decode(packet):
            pass
            
    frames = 0
    while True:
        chunk = f.read(10000)
        if not chunk:
            break
        for packet in codec.parse(chunk):
            for frame in codec.decode(packet):
                frames += 1
                if frames == 80:
                    bgr = frame.to_ndarray(format='bgr24')
                    rgb = bgr[:, :, ::-1]
                    small = cv2.resize(rgb, (0, 0), fx=0.5, fy=0.5)
                    locs = face_recognition.face_locations(small)
                    print("Unrotated locs:", locs)
                    
                    # Try rotating
                    rotated = cv2.rotate(small, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    locs2 = face_recognition.face_locations(rotated)
                    print("Rotated counterclockwise locs:", locs2)
                    
                    rotated2 = cv2.rotate(small, cv2.ROTATE_90_CLOCKWISE)
                    locs3 = face_recognition.face_locations(rotated2)
                    print("Rotated clockwise locs:", locs3)
                    sys.exit(0)
