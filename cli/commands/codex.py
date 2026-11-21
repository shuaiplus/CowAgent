"""Manage the ChatGPT subscription used by CowAgent's Codex provider."""

import click

from cli.utils import ensure_sys_path


@click.group(context_settings={"help_option_names": ["--help"]})
def codex():
    """Manage ChatGPT sign-in for the Codex provider."""
    ensure_sys_path()


@codex.command()
@click.option("--browser", is_flag=True, help="Use a local browser instead of a device code.")
def login(browser):
    """Sign in with ChatGPT on this CowAgent host."""
    from common.i18n import t
    from models.codex.app_server import CodexError, refresh_models, run_auth_command

    try:
        if run_auth_command("login", *([] if browser else ["--device-auth"])):
            raise click.ClickException("Codex login failed.")
        models = refresh_models()
    except (CodexError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(t(
        f"已同步 {len(models)} 个 Codex 模型。请在网页的「模型」中选择 Codex (ChatGPT)。",
        f"Synced {len(models)} Codex models. Select Codex (ChatGPT) on the web Models page.",
    ))


@codex.command()
def status():
    """Check whether the provider has a ChatGPT login."""
    from models.codex.app_server import AppServer, CodexError

    try:
        with AppServer() as server:
            account = server.require_chatgpt()
    except (CodexError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"ChatGPT ({account.get('planType') or 'unknown plan'})")


@codex.command()
def models():
    """Refresh and print the models available from Codex."""
    from models.codex.app_server import CodexError, refresh_models

    try:
        for model in refresh_models():
            click.echo(model)
    except (CodexError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@codex.command()
def logout():
    """Sign this CowAgent instance out of Codex."""
    from models.codex.app_server import CodexError, run_auth_command

    try:
        if run_auth_command("logout"):
            raise click.ClickException("Codex logout failed.")
    except (CodexError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
