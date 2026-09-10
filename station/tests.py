from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase

from .face_recognition_service import recognize_frame


class FaceRecognitionTests(SimpleTestCase):
	@staticmethod
	def _recognizer(predictions):
		class FakeRecognizer:
			def predict(self, _face):
				return predictions.pop(0)

		return FakeRecognizer()

	@patch('station.face_recognition_service._appearance_variants', return_value=[object()] * 4)
	@patch('station.face_recognition_service.detect_largest_face', return_value=(0, 0, 100, 100))
	def test_ambiguous_identity_is_rejected(self, _detect_face, _variants):
		recognizer = self._recognizer([(0, 32.0), (1, 35.0), (0, 34.0), (1, 33.0)])

		with patch('station.face_recognition_service.get_recognizer', return_value=(recognizer, {0: 10, 1: 20})):
			result = recognize_frame(np.zeros((120, 120, 3), dtype=np.uint8))

		self.assertFalse(result['matched'])
		self.assertEqual(result['reason'], 'low_confidence')

	@patch('station.face_recognition_service._appearance_variants', return_value=[object()] * 4)
	@patch('station.face_recognition_service.detect_largest_face', return_value=(0, 0, 100, 100))
	def test_match_must_be_below_absolute_threshold(self, _detect_face, _variants):
		recognizer = self._recognizer([(0, 61.0), (0, 62.0), (0, 63.0), (0, 64.0)])

		with patch('station.face_recognition_service.get_recognizer', return_value=(recognizer, {0: 10})):
			result = recognize_frame(np.zeros((120, 120, 3), dtype=np.uint8))

		self.assertFalse(result['matched'])
		self.assertEqual(result['reason'], 'low_confidence')

# Create your tests here.
