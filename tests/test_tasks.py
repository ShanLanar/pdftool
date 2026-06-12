"""Tests für die Werkzeug-Registry (ohne echte PDF-Verarbeitung)."""
import tasks


def test_tools_registry():
    keys = {t.key for t in tasks.TOOLS}
    assert {"merge", "split", "rotate", "remove_password",
            "strip_metadata", "repair"} <= keys
    for t in tasks.TOOLS:
        assert t.label and callable(t.func)
