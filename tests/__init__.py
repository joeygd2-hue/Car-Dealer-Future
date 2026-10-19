"""Test package. Being a package makes pytest import conftest as ``tests.conftest``, so
``from tests.conftest import ...`` reuses the same module instead of re-running its setup."""
