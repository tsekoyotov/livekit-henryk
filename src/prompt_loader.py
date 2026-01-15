"""
Prompt loader for Agent_prompt.md with variable substitution.

Loads the system prompt from markdown and handles {{variable}} placeholder
substitution for time and timezone.
"""

import re
import logging
from pathlib import Path
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def load_prompt_file() -> str:
    """
    Load Agent_prompt.md content (cached).

    Uses LRU cache to avoid repeated file I/O.
    Call load_prompt_file.cache_clear() to reload after file changes.

    Returns:
        File content as string, or empty string if file not found.
    """
    path = Path(__file__).parent / "Agent_prompt.md"

    if not path.exists():
        logger.warning(f"Prompt file not found: {path}")
        return ""

    try:
        content = path.read_text(encoding="utf-8")
        logger.info(f"Loaded prompt file: {path} ({len(content)} chars)")
        return content
    except Exception as e:
        logger.error(f"Failed to read prompt file {path}: {e}")
        return ""


def get_system_prompt(current_time: str, timezone: str) -> str:
    """
    Get the system prompt with variables substituted.

    Args:
        current_time: Formatted time string (e.g., "Monday, January 15, 2026 at 10:30 AM")
        timezone: IANA timezone name (e.g., "UTC", "America/New_York")

    Returns:
        System prompt ready to be used as LLM instructions.
    """
    content = load_prompt_file()

    if not content:
        # Fallback prompt if file is missing
        logger.warning("Using fallback prompt (file not loaded)")
        return f"""You are a helpful voice assistant on a phone call.
Keep responses conversational and concise.
The current time is {current_time} ({timezone})."""

    # Extract content between ``` blocks in ## System Prompt section
    match = re.search(
        r'## System Prompt\s*\n+```\n?(.*?)\n?```',
        content,
        re.DOTALL
    )

    if match:
        prompt = match.group(1).strip()
        logger.debug(f"Parsed System Prompt: {len(prompt)} chars")
    else:
        logger.warning("Could not find '## System Prompt' section, using full content")
        prompt = content

    # Substitute variables
    prompt = prompt.replace("{{current_time}}", current_time)
    prompt = prompt.replace("{{timezone}}", timezone)

    return prompt
