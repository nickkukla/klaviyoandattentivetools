import pytest

from migtool.config import get_instance
from migtool.safety import WriteRefused, confirm_write


def run(target, typed=None, **kwargs):
    shown, prompts = [], []

    def prompt(text):
        prompts.append(text)
        return typed

    confirm_write(
        get_instance(target), account="LOF US", record_count=1234,
        echo=shown.append, prompt=prompt, **kwargs,
    )
    return shown, prompts


def test_shows_target_account_and_count_then_accepts_typed_name():
    shown, prompts = run("klaviyo_us", typed="klaviyo_us")
    text = "\n".join(shown)
    assert "klaviyo_us" in text and "LOF US" in text and "1,234" in text
    assert len(prompts) == 1


def test_wrong_name_is_refused():
    with pytest.raises(WriteRefused, match="did not match"):
        run("klaviyo_us", typed="klaviyo_ca")


def test_yes_skips_the_prompt():
    _, prompts = run("klaviyo_us", yes=True)
    assert prompts == []


def test_source_write_refused_without_flag_even_with_yes():
    with pytest.raises(WriteRefused, match="--allow-write-to-source"):
        run("klaviyo_ca", yes=True)


def test_source_write_refused_before_anything_is_shown():
    shown = []
    with pytest.raises(WriteRefused):
        confirm_write(get_instance("attentive_ca"), account="LOF CA", record_count=1,
                      echo=shown.append, prompt=lambda _: "attentive_ca")
    assert shown == []


def test_source_write_allowed_with_flag_and_confirmation():
    run("klaviyo_ca", typed="klaviyo_ca", allow_write_to_source=True)
