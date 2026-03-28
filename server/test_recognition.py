import av
import sys
import face_recognition
import numpy as np

# Load the single Jason image or Kaleb or Maryam
known_names = []
known_encodings = []
import os
import glob
for img_path in glob.glob("user_recognition_agent/known_faces/*.jpg"):
    print("loading", img_path)
    img = face_recognition.load_image_file(img_path)
    encodings = face_recognition.face_encodings(img)
    if encodings:
        known_encodings.append(encodings[0])
        name = os.path.basename(img_path).split('.')[0]
        known_names.append(name)
        print("Loaded", name)

codec = av.CodecContext.create("h264", "r")
frames = 0
found_faces = []

with open("test.h264", "rb") as f:
    while True:
        chunk = f.read(4096)
        if not chunk:
            break
        for packet in codec.parse(chunk):
            for frame in codec.decode(packet):
                frames += 1
                if frames % 10 == 0:
                    bgr_img = frame.to_ndarray(format='bgr24')
                    rgb_frame = bgr_img[:, :, ::-1]
                    import cv2
                    small_frame = cv2.resize(rgb_frame, (0, 0), fx=0.5, fy=0.5)
                    locations = face_recognition.face_locations(small_frame)
                    if locations:
                        encodings = face_recognition.face_encodings(small_frame, locations)
                        for enc in encodings:
                            matches = face_recognition.compare_faces(known_encodings, enc, tolerance=0.6)
                            if True in matches:
                                face_distances = face_recognition.face_distance(known_encodings, enc)
                                best_match_index = np.argmin(face_distances)
                                if matches[best_match_index]:
                                    name = known_names[best_match_index]
                                    print("Found", name, "at frame", frames)
                    else:
                        print("No faces found at frame", frames)
print("Total frames", frames)
