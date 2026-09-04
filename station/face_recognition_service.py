"""Lightweight, fully-offline staff face recognition using OpenCV Haar cascades + LBPH.

Trains against each active Staff member's `profile_picture`. The trained model is
cached in-process and lazily rebuilt whenever the set of enrolled staff photos changes
(detected via a cheap staleness signature), so callers just call recognize_frame().
"""
import cv2
import numpy as np
from django.db.models import Max, Count

from .models import Staff

CONFIDENCE_THRESHOLD = 70.0  # LBPH distance; lower = more confident match. Tune once real photos exist.
FACE_SIZE = (200, 200)

_HAAR_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

_recognizer_cache = {"recognizer": None, "label_to_staff_id": {}, "signature": None}


def _enrolled_staff_qs():
    return Staff.objects.filter(profile_picture__isnull=False, is_active=True).exclude(profile_picture='')


def _staleness_signature():
    agg = _enrolled_staff_qs().aggregate(latest=Max('updated_at'), n=Count('id'))
    return (agg['n'], agg['latest'].isoformat() if agg['latest'] else None)


def detect_largest_face(gray_frame):
    """Returns (x, y, w, h) of the largest detected face in a grayscale frame, or None."""
    faces = _HAAR_CASCADE.detectMultiScale(gray_frame, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def _load_training_faces():
    faces, labels, label_map = [], [], {}
    for idx, staff in enumerate(_enrolled_staff_qs()):
        img = cv2.imread(staff.profile_picture.path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        box = detect_largest_face(img)
        if box is None:
            continue
        x, y, w, h = box
        faces.append(cv2.resize(img[y:y + h, x:x + w], FACE_SIZE))
        labels.append(idx)
        label_map[idx] = staff.id
    return faces, labels, label_map


def get_recognizer():
    """Returns (recognizer, label_to_staff_id), rebuilding lazily if enrollment changed.
    recognizer is None if no staff have a usable enrolled photo."""
    sig = _staleness_signature()
    if _recognizer_cache["signature"] != sig:
        faces, labels, label_map = _load_training_faces()
        if not faces:
            _recognizer_cache.update(recognizer=None, label_to_staff_id={}, signature=sig)
        else:
            recognizer = cv2.face.LBPHFaceRecognizer_create()
            recognizer.train(faces, np.array(labels))
            _recognizer_cache.update(recognizer=recognizer, label_to_staff_id=label_map, signature=sig)
    return _recognizer_cache["recognizer"], _recognizer_cache["label_to_staff_id"]


def recognize_frame(bgr_frame):
    """Takes an OpenCV BGR frame and returns a dict describing the recognition result:
    {'matched': bool, 'staff_id': int|None, 'confidence': float|None, 'reason': str|None}
    reason is one of: 'no_face_detected', 'no_enrolled_staff', 'low_confidence' (only set when not matched)."""
    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    box = detect_largest_face(gray)
    if box is None:
        return {'matched': False, 'staff_id': None, 'confidence': None, 'reason': 'no_face_detected'}

    recognizer, label_map = get_recognizer()
    if recognizer is None:
        return {'matched': False, 'staff_id': None, 'confidence': None, 'reason': 'no_enrolled_staff'}

    x, y, w, h = box
    face = cv2.resize(gray[y:y + h, x:x + w], FACE_SIZE)
    label, confidence = recognizer.predict(face)
    if confidence > CONFIDENCE_THRESHOLD:
        return {'matched': False, 'staff_id': None, 'confidence': confidence, 'reason': 'low_confidence'}

    return {'matched': True, 'staff_id': label_map[label], 'confidence': confidence, 'reason': None}
