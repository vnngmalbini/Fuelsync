from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dashboard.models import UserProfile


User = get_user_model()


class DashboardAttendanceButtonTests(TestCase):
    def test_main_dashboard_has_mark_attendance_button_for_staff(self):
        user = User.objects.create_user(username='attendant1', password='StrongPass123!')
        UserProfile.objects.create(user=user, role='attendant')

        self.client.force_login(user)
        response = self.client.get(reverse('dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mark Attendance')
