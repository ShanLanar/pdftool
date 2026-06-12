"""
pytest-Konfiguration.

Der Kerncode importiert beim Modulstart tkinter (für die GUI), die reinen
Funktionen brauchen es aber nicht. Damit die Tests auch ohne Display / ohne
tkinter laufen (z. B. headless oder in CI), wird tkinter hier durch einen
Platzhalter ersetzt, BEVOR pdf_optimizer importiert wird.
"""
import sys
import types

if "tkinter" not in sys.modules:
    class _AnyMeta(type):
        def __getattr__(cls, name):
            return _Any

    class _Any(metaclass=_AnyMeta):
        """Subclass-/instanziierbarer Platzhalter (tk.Tk, ttk.Style, ...)."""

    fake = types.ModuleType("tkinter")
    fake.__getattr__ = lambda name: _Any   # type: ignore[attr-defined]
    sys.modules["tkinter"] = fake
