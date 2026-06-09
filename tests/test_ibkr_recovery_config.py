from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_env_example_uses_trusted_local_admin_actions_for_dashboard_restart():
    env_example = (ROOT / ".env.example").read_text()

    assert "\nADMIN_TOKEN=\n" in env_example
    assert "\nADMIN_ALLOW_NO_AUTH=1\n" in env_example


def test_compose_defaults_to_trusted_local_admin_actions():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "ADMIN_ALLOW_NO_AUTH: ${ADMIN_ALLOW_NO_AUTH:-1}" in compose
