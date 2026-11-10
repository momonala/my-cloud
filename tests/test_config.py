"""Tests for src/core/config.py.

These tests verify that:
1. A config key returns its value, in both the flag and bare forms
2. --all returns every configuration value
3. A missing or unknown key produces an error

The real `src.core.config.app` is exercised rather than a locally-assembled Typer
app, because the flag form (`--project-name`) depends on that app's
`ignore_unknown_options` context settings -- which is exactly what
install/install.sh relies on.
"""

import pytest
from typer.testing import CliRunner

from src.core.config import app

runner = CliRunner()


def test_config_returns_single_value():
    result = runner.invoke(app, ["--flask-port"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "5016"


def test_bare_key_form_also_works():
    """install.sh uses the flag form; the underscore form is the documented alias."""
    assert runner.invoke(app, ["flask_port"]).stdout.strip() == "5016"


def test_config_all_returns_all_values():
    result = runner.invoke(app, ["--all"])

    assert result.exit_code == 0
    assert "project_name=my-cloud" in result.stdout
    assert "project_version=0.1.0" in result.stdout
    assert "flask_port=5016" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"), [([], "no config key specified"), (["--nonsense"], "unknown config key")]
)
def test_config_with_a_missing_or_unknown_key_fails(args, message):
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert message in result.output.lower()
