import pytest
from pydantic import ValidationError

from reelbot.config import Settings


def test_single_higgsfield_key_is_split():
    s = Settings(_env_file=None, hf_api_key="  abc123:sec:ret  ")
    assert (s.hf_api_key_id, s.hf_api_key_secret) == ("abc123", "sec:ret")


def test_separate_parts_still_work_and_win():
    s = Settings(_env_file=None, hf_api_key="x:y", hf_api_key_id="id", hf_api_key_secret="secret")
    assert (s.hf_api_key_id, s.hf_api_key_secret) == ("id", "secret")


def test_key_without_colon_is_rejected_with_a_clear_message():
    with pytest.raises(ValidationError, match="HF_API_KEY must look like"):
        Settings(_env_file=None, hf_api_key="no-colon-here")
