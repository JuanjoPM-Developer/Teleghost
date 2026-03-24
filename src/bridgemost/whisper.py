"""Voice-to-text transcription via OpenAI-compatible Whisper API."""

import logging
from pathlib import Path

import aiohttp

logger = logging.getLogger("bridgemost.whisper")


class WhisperClient:
    """Transcribes audio files using any OpenAI-compatible speech-to-text API.

    Supports: local faster-whisper-server, OpenAI API, Groq, etc.
    """

    def __init__(
        self,
        url: str,
        api_key: str = "",
        model: str = "large-v3",
        language: str = "",
    ):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.language = language

    async def transcribe(self, audio_path: str) -> str | None:
        """Transcribe an audio file to text.

        Args:
            audio_path: Path to the audio file (ogg, mp3, wav, etc.)

        Returns:
            Transcribed text, or None on failure.
        """
        endpoint = f"{self.url}/v1/audio/transcriptions"
        path = Path(audio_path)

        if not path.exists():
            logger.error("Audio file not found: %s", audio_path)
            return None

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = aiohttp.FormData()
        data.add_field(
            "file",
            open(audio_path, "rb"),  # noqa: SIM115
            filename=path.name,
            content_type=_guess_mime(path.suffix),
        )
        data.add_field("model", self.model)
        if self.language:
            data.add_field("language", self.language)
        data.add_field("response_format", "json")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            "Whisper API error %d: %s", resp.status, body[:200]
                        )
                        return None

                    result = await resp.json()
                    text = result.get("text", "").strip()

                    if not text:
                        logger.warning("Whisper returned empty transcription")
                        return None

                    logger.info(
                        "Transcribed %s (%d bytes) → %d chars",
                        path.name, path.stat().st_size, len(text),
                    )
                    return text

        except aiohttp.ClientError as exc:
            logger.error("Whisper connection error: %s", exc)
            return None
        except TimeoutError:
            logger.error("Whisper transcription timed out (120s)")
            return None
        except Exception:
            logger.exception("Unexpected Whisper error")
            return None


def _guess_mime(suffix: str) -> str:
    """Guess MIME type from file extension."""
    return {
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/opus",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(suffix.lower(), "audio/ogg")
