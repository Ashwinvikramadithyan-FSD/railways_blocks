import hashlib
import hmac
import re

from django import forms
from django.conf import settings
from django.contrib.auth.models import User

from .models import UserProfile

MAX_TEXT = 200

# The four "characters" (roles) shown on the register page.
CHARACTER_CHOICES = [
    ('STATION', 'Station Master'),
    ('DIVISION', 'Division Master'),
    ('CTRL', 'Control Office'),
    ('WORKER', 'Worker Dashboard'),
]

# Gmail rules: 6-30 chars, letters/digits/dots, no leading/trailing/double dots.
GMAIL_RE = re.compile(r'^(?!\.)(?!.*\.\.)[a-z0-9.]{6,30}(?<!\.)@gmail\.com$')


def password_fingerprint(raw_password):
    """Deterministic keyed hash, used only to detect password re-use."""
    return hmac.new(
        settings.SECRET_KEY.encode(), raw_password.encode(), hashlib.sha256
    ).hexdigest()


def password_in_use(raw_password):
    """True if any existing account already uses this exact password."""
    fp = password_fingerprint(raw_password)
    if UserProfile.objects.filter(password_fingerprint=fp).exists():
        return True
    # Accounts created before fingerprints existed: compare against their hash.
    legacy = User.objects.filter(profile__password_fingerprint='')
    return any(u.check_password(raw_password) for u in legacy)


def _limited_text(label, value):
    value = (value or '').strip()
    if not value:
        raise forms.ValidationError(f"{label} is required.")
    if len(value) > MAX_TEXT:
        raise forms.ValidationError(
            f"{label} cannot be more than {MAX_TEXT} characters (you entered {len(value)})."
        )
    return value


class RegisterForm(forms.Form):
    username = forms.CharField(required=False)
    department = forms.CharField(required=False)  # the "Character" dropdown
    password = forms.CharField(widget=forms.PasswordInput, required=False)
    phone_number = forms.CharField(required=False)
    email = forms.CharField(required=False)
    division = forms.CharField(required=False)
    section = forms.CharField(required=False)
    station = forms.CharField(required=False)
    work_department = forms.CharField(required=False)  # SMMS / TMS / TDMS (workers only)

    def clean_username(self):
        username = _limited_text('Username', self.cleaned_data.get('username'))
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_department(self):
        value = (self.cleaned_data.get('department') or '').strip()
        if value not in dict(CHARACTER_CHOICES):
            raise forms.ValidationError("Please select a character.")
        return value

    def clean_password(self):
        password = self.cleaned_data.get('password') or ''
        if not password:
            raise forms.ValidationError("Password is required.")
        if password_in_use(password):
            raise forms.ValidationError(
                "This password is already used by another account. Please choose a different password."
            )
        return password

    def clean_phone_number(self):
        phone = (self.cleaned_data.get('phone_number') or '').strip()
        if not phone:
            raise forms.ValidationError("Phone number is required.")
        if not phone.isascii() or not phone.isdigit():
            raise forms.ValidationError("Phone number must contain digits only (0-9), no letters or symbols.")
        if len(phone) != 10:
            raise forms.ValidationError(
                f"Phone number must be exactly 10 digits (you entered {len(phone)})."
            )
        return phone

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            raise forms.ValidationError("Email ID is required.")
        if not email.endswith('@gmail.com'):
            raise forms.ValidationError("Only a Gmail address ending with @gmail.com is allowed.")
        if not GMAIL_RE.match(email):
            raise forms.ValidationError(
                "Enter a valid Gmail address (6-30 letters, numbers or dots before @gmail.com)."
            )
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email ID is already registered.")
        return email

    def clean_division(self):
        return _limited_text('Division', self.cleaned_data.get('division'))

    def clean_section(self):
        return _limited_text('Section', self.cleaned_data.get('section'))

    def clean_station(self):
        return _limited_text('Station', self.cleaned_data.get('station'))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('department') == 'WORKER':
            wd = (self.cleaned_data.get('work_department') or '').strip()
            if wd not in dict(UserProfile.WORK_DEPARTMENT_CHOICES):
                self.add_error('work_department', "Please select a department (SMMS, TMS or TDMS).")
            else:
                cleaned['work_department'] = wd
        else:
            cleaned['work_department'] = ''
        return cleaned