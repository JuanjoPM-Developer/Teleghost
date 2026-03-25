"""Tests for Whisper voice-to-text client."""

import pytest
from bridgemost.whisper import WhisperClient


class TestWhisperInit:
    """Test Whisper client initialization."""

    def test_creates_with_defaults(self):
        client = WhisperClient(url="http://localhost:9000/v1/audio/transcriptions")
        assert client.url == "http://localhost:9000/v1/audio/transcriptions"
        assert client.model == "large-v3"
        # language defaults to empty (set by config, not hardcoded)

    def test_custom_params(self):
        client = WhisperClient(
            url="http://other:8080/transcribe",
            api_key="test-key",
            model="small",
            language="en",
        )
        assert client.url == "http://other:8080/transcribe"
        assert client.api_key == "test-key"
        assert client.model == "small"
        assert client.language == "en"
