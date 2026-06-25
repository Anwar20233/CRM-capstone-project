import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: tests that call external services (require real API keys)",
    )


# Default asyncio mode for all async tests (avoids per-test decorators).
@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
