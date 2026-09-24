import pytest

from migtool.config import ConfigError, Secret, credential, get_instance


def test_unknown_instance_lists_valid_names():
    with pytest.raises(ConfigError, match="Valid instances: .*klaviyo_us"):
        get_instance("klaviyo_uk")


def test_wrong_service_is_rejected():
    with pytest.raises(ConfigError, match="not a stoq instance"):
        get_instance("klaviyo_us", "stoq")


def test_missing_variable_names_the_variable():
    with pytest.raises(ConfigError, match="KLAVIYO_CA_API_KEY is not set"):
        credential(get_instance("klaviyo_ca"), {})


def test_credential_value_never_shows():
    secret = credential(get_instance("klaviyo_us"), {"KLAVIYO_US_API_KEY": "pk_live_abc123"})
    assert secret.reveal() == "pk_live_abc123"
    assert "abc123" not in repr(secret)
    assert "abc123" not in str(secret)
    assert "abc123" not in f"{secret}"


def test_only_ca_instances_are_sources():
    assert get_instance("klaviyo_ca").is_source
    assert get_instance("attentive_ca").is_source
    assert not get_instance("klaviyo_us").is_source
    assert not get_instance("stoq_dev").is_source
