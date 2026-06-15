"""SAI desktop ad overlay.

A standalone, app-agnostic companion that floats the sponsor card over a target
app's window (Claude Desktop, and later the Codex GUI) without touching that app.
It owns its own window and measures its own on-screen visible time, so a screen
overlay impression is held to the exact same honesty bar as the in-terminal card:
the sponsor only bills while the card is genuinely visible and the user present.

The package is import-safe on every platform: nothing here touches the Win32 API
at import time. The OS queries live behind the ``SystemProbe`` interface in
``win32`` so the visibility logic can be exercised headless on POSIX CI.
"""
