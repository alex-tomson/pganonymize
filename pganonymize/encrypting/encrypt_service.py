import hashlib
import os
from binascii import hexlify, unhexlify
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptingService:

    def __init__(self, secret: str):
        self._secret = secret

    def _deriveKey(self, salt: bytes = None) -> [str, bytes]:
        if salt is None:
            salt = os.urandom(8)
        return hashlib.pbkdf2_hmac("sha256", self._secret.encode("utf8"), salt, 1000), salt

    def encrypt_function(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            return plaintext
        key, salt = self._deriveKey()
        aes = AESGCM(key)
        iv = os.urandom(12)
        plaintext = plaintext.encode("utf8")
        ciphertext = aes.encrypt(iv, plaintext, None)
        return "%s-%s-%s" % (
            hexlify(salt).decode("utf8"),
            hexlify(iv).decode("utf8"),
            hexlify(ciphertext).decode("utf8")
        )

    def decrypt_function(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str):
            return ciphertext
        salt, iv, ciphertext = map(unhexlify, ciphertext.split("-"))
        key, _ = self._deriveKey(salt)
        aes = AESGCM(key)
        plaintext = aes.decrypt(iv, ciphertext, None)
        return plaintext.decode("utf8")
