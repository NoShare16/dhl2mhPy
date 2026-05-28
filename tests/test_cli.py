"""CLI smoke checks only.

DO NOT invoke ``runner.invoke(app, ...)`` here unless the pipeline is fully
mocked AND we have verified the mock actually intercepts the call. A previous
version of this file triggered the real pipeline against Plenty/DHL UAT/SMTP
because ``patch("dhl2mh.cli.run_pipeline", ...)`` didn't take effect under
typer's command dispatch. CLI behaviour belongs in manual / integration tests.
"""

import typer

from dhl2mh.cli import app


def test_app_is_a_typer_instance():
    assert isinstance(app, typer.Typer)


def test_run_command_is_registered():
    names = {cmd.name or cmd.callback.__name__ for cmd in app.registered_commands}
    assert "run" in names
