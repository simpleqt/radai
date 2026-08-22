from backend.api.routers import guest_sms_auth


TENCENT_ENV_KEYS = (
    "TENCENT_SMS_SECRET_ID",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENT_SMS_SECRET_KEY",
    "TENCENTCLOUD_SECRET_KEY",
    "TENCENT_SMS_SDK_APP_ID",
    "TENCENT_SMS_SIGN_NAME",
    "TENCENT_SMS_TEMPLATE_ID",
)


def _clear_tencent_env(monkeypatch):
    for key in (
        *TENCENT_ENV_KEYS,
        "TENCENT_SMS_REGION",
        "TENCENT_SMS_TEMPLATE_PARAM_MODE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_guest_sms_does_not_use_school_sms_config(monkeypatch):
    _clear_tencent_env(monkeypatch)

    monkeypatch.setattr(
        guest_sms_auth,
        "_backend_env",
        lambda: {
            "SMS_SEND_MESSAGE_API_URL": "https://school.example.edu/send",
            "SMS_APP_ID": "school-app",
            "SMS_APP_SECRET": "school-secret",
        },
    )

    _, error = guest_sms_auth._required_sms_config()

    assert error
    assert "腾讯云短信" in error


def test_guest_sms_uses_tencent_config_even_when_school_sms_exists(monkeypatch):
    _clear_tencent_env(monkeypatch)

    monkeypatch.setattr(
        guest_sms_auth,
        "_backend_env",
        lambda: {
            "SMS_SEND_MESSAGE_API_URL": "https://school.example.edu/send",
            "SMS_APP_ID": "school-app",
            "SMS_APP_SECRET": "school-secret",
            "TENCENT_SMS_SECRET_ID": "tencent-secret-id",
            "TENCENT_SMS_SECRET_KEY": "tencent-secret-key",
            "TENCENT_SMS_SDK_APP_ID": "tencent-sdk-app-id",
            "TENCENT_SMS_SIGN_NAME": "tencent-sign",
            "TENCENT_SMS_TEMPLATE_ID": "tencent-template",
        },
    )

    config, error = guest_sms_auth._required_sms_config()

    assert error is None
    assert config["secret_id"] == "tencent-secret-id"
    assert config["sdk_app_id"] == "tencent-sdk-app-id"
