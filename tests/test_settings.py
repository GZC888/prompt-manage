"""The settings page: preferences and the password form."""

from .helpers import CSRF, get_setting, login, seed_password, set_csrf


def _post(client, **fields):
    data = {"_csrf_token": CSRF}
    data.update(fields)
    return client.post("/settings", data=data, follow_redirects=True)


def test_general_settings_round_trip(client, appmod):
    set_csrf(client)
    _post(client, settings_action="general", language="en", version_cleanup_threshold="42")
    assert get_setting(appmod, "language") == "en"
    assert get_setting(appmod, "version_cleanup_threshold") == "42"


def test_language_switch_changes_the_rendered_page(client, appmod):
    set_csrf(client)
    _post(client, settings_action="general", language="en", version_cleanup_threshold="200")
    assert b"New Prompt" in client.get("/").data


def test_threshold_must_be_a_positive_integer(client, appmod):
    set_csrf(client)
    for bad in ("0", "-5", "abc", "999999999"):
        response = _post(client, settings_action="general", version_cleanup_threshold=bad)
        assert "版本保留数量" in response.get_data(as_text=True)
    assert get_setting(appmod, "version_cleanup_threshold") == "200"


def test_invalid_language_is_rejected(client, appmod):
    set_csrf(client)
    _post(client, settings_action="general", language="fr", version_cleanup_threshold="200")
    assert get_setting(appmod, "language") == "zh"


def test_unknown_action_changes_nothing(client, appmod):
    set_csrf(client)
    response = _post(client, settings_action="destroy", version_cleanup_threshold="7")
    assert "未知的设置操作" in response.get_data(as_text=True)
    assert get_setting(appmod, "version_cleanup_threshold") == "200"


def test_setting_the_first_password(client, appmod):
    set_csrf(client)
    _post(client, settings_action="auth", auth_mode="global",
          new_password="password123", confirm_password="password123")
    assert get_setting(appmod, "auth_mode") == "global"
    assert appmod.verify_password("password123", get_setting(appmod, "auth_password_hash"))
    # The owner who just set it stays signed in.
    assert client.get("/settings").status_code == 200


def test_password_change_requires_the_current_password(client, appmod):
    login(client, appmod, "password123")
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="global",
                     new_password="newpassword1", confirm_password="newpassword1")
    assert "请先输入当前密码" in response.get_data(as_text=True)
    assert appmod.verify_password("password123", get_setting(appmod, "auth_password_hash"))


def test_wrong_current_password_is_rejected(client, appmod):
    login(client, appmod, "password123")
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="global",
                     current_password="nope", new_password="newpassword1",
                     confirm_password="newpassword1")
    assert "当前密码不正确" in response.get_data(as_text=True)


def test_mismatched_confirmation_is_rejected(client, appmod):
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="global",
                     new_password="password123", confirm_password="password124")
    assert "两次输入的密码不一致" in response.get_data(as_text=True)
    assert get_setting(appmod, "auth_password_hash") == ""


def test_short_password_is_rejected(client, appmod):
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="global",
                     new_password="short", confirm_password="short")
    assert "至少为 8 位" in response.get_data(as_text=True)


def test_enabling_protection_without_a_password_is_rejected(client, appmod):
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="global")
    assert "请先设置访问密码" in response.get_data(as_text=True)
    assert get_setting(appmod, "auth_mode") == "off"


def test_switching_mode_off_needs_the_current_password(client, appmod):
    login(client, appmod, "password123")
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="off")
    assert "请先输入当前密码" in response.get_data(as_text=True)
    assert get_setting(appmod, "auth_mode") == "global"

    _post(client, settings_action="auth", auth_mode="off", current_password="password123")
    assert get_setting(appmod, "auth_mode") == "off"


def test_removed_per_prompt_mode_is_rejected(client, appmod):
    set_csrf(client)
    response = _post(client, settings_action="auth", auth_mode="per",
                     new_password="password123", confirm_password="password123")
    assert "认证方式无效" in response.get_data(as_text=True)
    assert get_setting(appmod, "auth_mode") == "off"


def test_settings_page_reports_counts(client, appmod):
    from .helpers import create_prompt

    create_prompt(appmod, versions=3)
    body = client.get("/settings").get_data(as_text=True)
    assert "1" in body and "3" in body


def test_settings_requires_login_when_protected(client, appmod):
    seed_password(appmod, "password123", "global")
    assert client.get("/settings").status_code == 302
