from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class CredentialCipherError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class DpapiCredentialCipher:
    """Encrypt credentials for the current Windows user with DPAPI."""

    prefix = b"MAILCOM-DPAPI-1\0"

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialCipherError("Windows DPAPI is required")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buffer

    def encrypt(self, value: str) -> bytes:
        if not value:
            raise CredentialCipherError("Credential must not be empty")
        raw = value.encode("utf-8")
        source, source_buffer = self._blob(raw)
        _ = source_buffer
        output = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "MailCom Manager credential",
            None,
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
        if not ok:
            raise CredentialCipherError(
                f"DPAPI encryption failed: {ctypes.get_last_error()}"
            )
        try:
            return self.prefix + ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def decrypt(self, value: bytes) -> str:
        if not value.startswith(self.prefix):
            raise CredentialCipherError("Credential format is not recognized")
        source, source_buffer = self._blob(value[len(self.prefix) :])
        _ = source_buffer
        output = _DataBlob()
        description = wintypes.LPWSTR()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
        if not ok:
            raise CredentialCipherError(
                f"DPAPI decryption failed: {ctypes.get_last_error()}"
            )
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            if description:
                self._kernel32.LocalFree(description)
            self._kernel32.LocalFree(output.pbData)
