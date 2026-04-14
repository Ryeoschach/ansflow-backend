from django.db import models
from utils.encryption import encrypt_string, decrypt_string

class EncryptedCharField(models.CharField):
    """
    自解密字符字段
    """
    def from_db_value(self, value, expression, connection):
        return decrypt_string(value)

    def to_python(self, value):
        return decrypt_string(value)

    def get_prep_value(self, value):
        return encrypt_string(value)

class EncryptedTextField(models.TextField):
    """
    自解密文本字段
    """
    def from_db_value(self, value, expression, connection):
        return decrypt_string(value)

    def to_python(self, value):
        return decrypt_string(value)

    def get_prep_value(self, value):
        return encrypt_string(value)
