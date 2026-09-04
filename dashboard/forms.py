from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, UsernameField
from django.utils.text import slugify
from django.utils import timezone
from django.core.exceptions import ValidationError
from PIL import Image
from .models import UserProfile
from station.models import Staff, StaffRole


User = get_user_model()


def normalize_ghana_phone(phone):
    phone = (phone or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())

    if digits.startswith("233") and len(digits) == 12:
        return f"+{digits}"

    # Strip leading zero from local format (0XX-XXX-XXXX = 10 digits, or mistyped 9-digit with 0)
    if digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 9:
        return f"+233{digits}"

    return phone


class PhoneAuthenticationForm(AuthenticationForm):
    ROLE_CHOICES = [
        ('admin', 'Administrator'),
        ('manager', 'Manager'),
        ('attendant', 'Fuel Attendant'),
    ]

    username = forms.CharField(
        label="Phone Number (+233)",
        widget=forms.TextInput(attrs={"autofocus": True, "placeholder": "50 XXX XXXX", "maxlength": "20"}),
    )
    
    role = forms.ChoiceField(
        label="Account Role",
        choices=ROLE_CHOICES,
        required=False,
        widget=forms.RadioSelect(attrs={
            "class": "role-selector",
        }),
    )

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        
        if username and password:
            full_phone = normalize_ghana_phone(username)
            # Authenticate using phone number
            self.user_cache = self._authenticate(full_phone, password)
            if self.user_cache is None:
                raise forms.ValidationError("Invalid phone number or password.")
            else:
                # Set the backend explicitly to avoid ValueError with multiple backends
                self.user_cache.backend = 'dashboard.auth_backend.PhoneAuthenticationBackend'
        
        return self.cleaned_data

    def _authenticate(self, phone, password):
        try:
            from .models import UserProfile
            profile = UserProfile.objects.get(phone=phone)
            user = profile.user
            if user.check_password(password):
                return user
        except UserProfile.DoesNotExist:
            pass
        return None


class PhoneSignupForm(UserCreationForm):
    ROLE_CHOICES = [
        ('admin', 'Administrator'),
        ('manager', 'Manager'),
        ('attendant', 'Fuel Attendant'),
    ]

    phone = forms.CharField(
        max_length=20,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '50 XXX XXXX',
            'required': True
        }),
        label="Phone Number (+233)"
    )
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'First name',
            'required': True,
        }),
        label="First Name"
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Last name'
        }),
        label="Last Name"
    )
    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        widget=forms.RadioSelect(attrs={
            'class': 'role-selector',
        }),
        label="Account Role"
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("first_name", "last_name", "email", "password1", "password2")

    email = forms.EmailField(
        max_length=254,
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'you@example.com',
            'required': True,
        }),
        label="Email Address"
    )

    @staticmethod
    def _build_unique_username(phone):
        base = f"user_{phone[-7:]}"
        candidate = base[:150]
        counter = 1

        while User.objects.filter(username=candidate).exists():
            suffix = str(counter)
            candidate = f"{base[:150 - len(suffix)]}{suffix}"
            counter += 1

        return candidate

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if not phone:
            raise forms.ValidationError("Phone number is required.")
        full_phone = normalize_ghana_phone(phone)
        if not full_phone.startswith("+233") or len(full_phone) < 13:
            raise forms.ValidationError("Enter a valid Ghana phone number.")
        if UserProfile.objects.filter(phone=full_phone).exists():
            raise forms.ValidationError("An account with this phone number already exists.")
        return full_phone

    def clean_first_name(self):
        first_name = self.cleaned_data.get("first_name", "").strip()
        if not first_name:
            raise forms.ValidationError("First name is required.")
        return first_name

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if not email:
            raise forms.ValidationError('Email is required.')
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self._build_unique_username(self.cleaned_data["phone"])
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get('email', '').strip()
        selected_role = self.cleaned_data.get("role", "attendant")

        if selected_role == 'admin':
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False

        if commit:
            user.save()
            # Create UserProfile with phone number (already has +233 prefix from clean_phone)
            UserProfile.objects.update_or_create(
                user=user,
                defaults={
                    'phone': self.cleaned_data["phone"],
                    'role': selected_role,
                }
            )

            staff_role, _ = StaffRole.objects.get_or_create(
                name=selected_role,
                defaults={'permissions': 'dashboard,inventory,pumps,sales,payments'},
            )
            Staff.objects.update_or_create(
                user=user,
                defaults={
                    'role': staff_role,
                    'phone': self.cleaned_data["phone"],
                    'date_hired': timezone.localdate(),
                    'salary': 0,
                    'is_active': True,
                }
            )
        return user


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=150, required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    email = forms.EmailField(widget=forms.EmailInput(attrs={'class': 'form-control'}))

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].initial = self.instance.first_name
        self.fields['last_name'].initial = self.instance.last_name
        self.fields['email'].initial = self.instance.email


class ProfilePictureForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('profile_picture', 'phone', 'bio', 'country', 'city', 'address', 'dark_mode')
        widgets = {
            'profile_picture': forms.FileInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Phone number'}),
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Tell us about yourself'}),
            'country': forms.TextInput(attrs={'class': 'form-control'}),
            'city': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'dark_mode': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        if not phone:
            return phone
        full_phone = normalize_ghana_phone(phone)
        if not full_phone.startswith('+233') or len(full_phone) < 13:
            raise forms.ValidationError("Enter a valid Ghana phone number.")
        if UserProfile.objects.filter(phone=full_phone).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another account already uses this phone number.")
        return full_phone


class ProfilePreferencesForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('dark_mode', 'email_notifications', 'sms_notifications')
        widgets = {
            'dark_mode': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'email_notifications': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sms_notifications': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class AvatarUploadForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('profile_picture',)
        widgets = {
            'profile_picture': forms.FileInput(
                attrs={
                    'class': 'form-control',
                    'accept': 'image/*',
                }
            ),
        }

    def clean_profile_picture(self):
        profile_picture = self.cleaned_data.get('profile_picture')
        
        if not profile_picture:
            return profile_picture

        # Check file size (max 2MB)
        max_size_mb = 2
        if profile_picture.size > max_size_mb * 1024 * 1024:
            raise ValidationError(
                f"Image size exceeds {max_size_mb}MB. Please upload a smaller image."
            )

        # Check file type
        allowed_formats = {'JPEG', 'PNG', 'WEBP'}
        try:
            img = Image.open(profile_picture)
            img.load()  # Verify it's a valid image
            
            if img.format not in allowed_formats:
                raise ValidationError(
                    "Invalid image format. Allowed formats: JPG, PNG, WebP"
                )
            
        except (IOError, OSError):
            raise ValidationError("Invalid image file. Please upload a valid image.")

        return profile_picture


class SMSTestForm(forms.Form):
    recipient_phone = forms.CharField(
        max_length=20,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Recipient phone number',
            'type': 'tel',
        }),
    )