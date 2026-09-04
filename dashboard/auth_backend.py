from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from .forms import normalize_ghana_phone

User = get_user_model()


class PhoneAuthenticationBackend(ModelBackend):
    """
    Authenticate using phone number stored in UserProfile
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            # Try to find user by phone number in UserProfile
            from .models import UserProfile
            profile = UserProfile.objects.get(phone=normalize_ghana_phone(username))
            user = profile.user
            
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        except UserProfile.DoesNotExist:
            pass
        
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
