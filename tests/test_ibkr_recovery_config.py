from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_env_example_keeps_no_auth_admin_actions_opt_in():
    env_example = (ROOT / ".env.example").read_text()

    assert "\nADMIN_ALLOW_NO_AUTH=0\n" in env_example
    assert "\nADMIN_TOKEN=\n" in env_example


def test_compose_keeps_no_auth_admin_actions_opt_in():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "ADMIN_ALLOW_NO_AUTH: ${ADMIN_ALLOW_NO_AUTH:-0}" in compose
