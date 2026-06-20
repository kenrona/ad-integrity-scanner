import pytest

from app.ssrf import SSRFError, assert_public_host, literal_host_blocked

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_literal_host_blocked():
    for bad in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254",
                "::1", "localhost", "metadata.google.internal", None, ""]:
        assert literal_host_blocked(bad) is True
    for ok in ["8.8.8.8", "1.1.1.1", "example.com"]:
        assert literal_host_blocked(ok) is False


async def test_assert_public_host_rejects_private_and_metadata():
    for bad in ["127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "localhost"]:
        with pytest.raises(SSRFError):
            await assert_public_host(bad)


async def test_assert_public_host_allows_public_ip_literal():
    # Public IP literal needs no DNS and must pass.
    await assert_public_host("8.8.8.8")
