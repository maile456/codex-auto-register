from __future__ import annotations

import os


_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def roxy_system_proxy_chain_available() -> bool:
    """Return whether Roxy can chain its custom proxy through WinINet."""
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (FileNotFoundError, OSError):
        return False
    return bool(enabled) and bool(str(proxy_server or "").strip())
