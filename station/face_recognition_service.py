"""Lightweight, fully-offline staff face recognition using OpenCV Haar cascades + LBPH.

Trains against each active Staff member's `profile_picture`. The trained model is
cached in-process and lazily rebuilt whenever the set of enrolled staff photos changes
(detected via a cheap staleness signature), so callers just call recognize_frame().
"""
import cv2
import numpy as np
from pathlib import Path
import skimage.data

from .models import Staff

CONFIDENCE_THRESHOLD = 60.0  # LBPH distance; lower = more confident match.
MIN_MATCH_MARGIN = 10.0  # The best identity must beat the runner-up by this much.
FACE_SIZE = (200, 200)
MIN_FACE_SIZE = (70, 70)

_HAAR_PATH = Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'
if not _HAAR_PATH.is_file():
    _HAAR_PATH = Path(skimage.data.__file__).parent / 'lbpcascade_frontalface_opencv.xml'
_HAAR_CASCADE = cv2.CascadeClassifier(str(_HAAR_PATH)) if _HAAR_PATH.is_file() else None

_recognizer_cache = {"recognizer": None, "label_to_staff_id": {}, "signature": None}


def _enrolled_staff_qs():
    return Staff.objects.filter(profile_picture__isnull=False, is_active=True).exclude(profile_picture='')


def _staleness_signature():
    enrolled = _enrolled_staff_qs().values_list('id', 'updated_at')
    return tuple((staff_id, updated_at.isoformat() if updated_at else None) for staff_id, updated_at in enrolled)


def detect_largest_face(gray_frame):
    """Returns (x, y, w, h) of the largest detected face in a grayscale frame, or None."""
    if _HAAR_CASCADE is None or _HAAR_CASCADE.empty():
        return None
    faces = _HAAR_CASCADE.detectMultiScale(
        gray_frame,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=MIN_FACE_SIZE,
    )
    if len(faces) == 0:
        # Attendance frames can be wider or darker than enrollment photos. A
        # second pass accepts smaller faces without making the normal pass slower.
        faces = _HAAR_CASCADE.detectMultiScale(
            cv2.equalizeHist(gray_frame),
            scaleFactor=1.12,
            minNeighbors=4,
            minSize=(50, 50),
        )
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def _detect_enrollment_face(gray_frame):
    """Use a more tolerant pass for stored photos with uneven scale or lighting."""
    if _HAAR_CASCADE is None or _HAAR_CASCADE.empty():
        return None
    box = detect_largest_face(gray_frame)
    if box is not None:
        return box
    equalized = cv2.equalizeHist(gray_frame)
    faces = _HAAR_CASCADE.detectMultiScale(
        equalized,
        scaleFactor=1.05,
        minNeighbors=3,
        minSize=(40, 40),
    )
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def _normalize_face(face):
    """Normalizes an inner face crop so hair, clothes, and lighting have less influence."""
    face = cv2.resize(face, FACE_SIZE, interpolation=cv2.INTER_AREA)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(face)


def _inner_face_crop(gray_frame, box):
    """Crop away the outer detector region, which can include hair and background."""
    x, y, width, height = box
    left = x + int(width * 0.12)
    top = y + int(height * 0.10)
    right = x + int(width * 0.88)
    bottom = y + int(height * 0.94)
    return gray_frame[top:bottom, left:right]


def _appearance_variants(face):
    """Return views that reduce sensitivity to glasses, lighting, and expression."""
    normalized = _normalize_face(face)
    return [
        normalized,
        cv2.flip(normalized, 1),
        cv2.GaussianBlur(normalized, (3, 3), 0),
    ]


def _training_variants(face):
    """Build mild enrollment variations without changing the person's identity."""
    normalized = _normalize_face(face)
    darker = cv2.convertScaleAbs(normalized, alpha=0.82, beta=-8)
    brighter = cv2.convertScaleAbs(normalized, alpha=1.15, beta=12)
    shifted = cv2.warpAffine(
        normalized,
        np.float32([[1, 0, 3], [0, 1, 2]]),
        FACE_SIZE,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return [
        normalized,
        cv2.flip(normalized, 1),
        cv2.GaussianBlur(normalized, (3, 3), 0),
        darker,
        brighter,
        shifted,
    ]


def _load_training_faces():
    faces, labels, label_map = [], [], {}
    for idx, staff in enumerate(_enrolled_staff_qs()):
        img = cv2.imread(staff.profile_picture.path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        box = _detect_enrollment_face(img)
        if box is None:
            continue
        variants = _training_variants(_inner_face_crop(img, box))
        faces.extend(variants)
        labels.extend([idx] * len(variants))
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

    face = _inner_face_crop(gray, box)
    predictions = [recognizer.predict(variant) for variant in _appearance_variants(face)]
    valid_predictions = [item for item in predictions if item[0] in label_map]
    if not valid_predictions:
        return {'matched': False, 'staff_id': None, 'confidence': None, 'reason': 'low_confidence'}

    scores = {}
    for label, distance in valid_predictions:
        scores.setdefault(label, []).append(distance)
    ranked_scores = sorted(
        [
            (
                label,
                sum(sorted(distances)[:2]) / min(2, len(distances)),
            )
            for label, distances in scores.items()
        ],
        key=lambda item: item[1],
    )
    label, confidence = ranked_scores[0]
    runner_up_confidence = ranked_scores[1][1] if len(ranked_scores) > 1 else None
    if confidence > CONFIDENCE_THRESHOLD or (
        runner_up_confidence is not None
        and runner_up_confidence - confidence < MIN_MATCH_MARGIN
    ):
        return {'matched': False, 'staff_id': None, 'confidence': confidence, 'reason': 'low_confidence'}

    return {'matched': True, 'staff_id': label_map[label], 'confidence': confidence, 'reason': None}
