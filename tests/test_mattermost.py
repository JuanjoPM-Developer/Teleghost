"""Tests for Mattermost client — non-network tests only."""

from bridgemost.mattermost import MattermostClient


class TestClientInit:
    """Test MattermostClient initialization."""

    def test_base_url_normalization(self):
        client = MattermostClient("http://localhost:8065/")
        assert client.base_url == "http://localhost:8065"

    def test_base_url_no_trailing_slash(self):
        client = MattermostClient("http://localhost:8065")
        assert client.base_url == "http://localhost:8065"

    def test_headers_built(self):
        client = MattermostClient("http://localhost:8065")
        # Client should be usable without immediate errors
        assert client.base_url is not None
