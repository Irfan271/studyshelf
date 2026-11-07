import base64, hashlib
from cryptography.fernet import Fernet
from django.conf import settings

def _fernet():
    key=base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)

def encrypt_secret(value):
    return _fernet().encrypt(value.encode()).decode()

def decrypt_secret(value):
    return _fernet().decrypt(value.encode()).decode()
