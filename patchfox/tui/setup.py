"""First-run Textual setup shown before an agent is constructed."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, Input, Label, Static

from ..config import PROVIDER_DEFAULTS
from ..config.store import config_paths, save_provider


class PatchFoxSetupApp(App[bool]):
    CSS = """
    Screen { align: center middle; background: #0f1117; }
    #setup { width: 72; height: auto; border: round #4d8dff; padding: 1 2; }
    #title { text-style: bold; color: #8fb8ff; margin-bottom: 1; }
    .field-label { color: #c5d1e8; margin-top: 1; }
    Input { width: 1fr; }
    #actions { margin-top: 1; height: 3; align-horizontal: right; }
    #error { color: #ff8585; height: auto; margin-top: 1; }
    #hint { color: #8c96aa; height: auto; }
    """

    def compose(self) -> ComposeResult:
        defaults = PROVIDER_DEFAULTS["deepseek"]
        with Container(id="setup"):
            yield Static("PatchFox first-time setup", id="title")
            yield Static(
                f"Provider settings go to {config_paths().config}.\n"
                f"The API key is stored separately in {config_paths().auth}.",
                id="hint",
            )
            yield Label("Provider", classes="field-label")
            yield Input(value="deepseek", id="provider")
            yield Label("Protocol (openai or anthropic)", classes="field-label")
            yield Input(value=str(defaults["protocol"]), id="protocol")
            yield Label("Base URL", classes="field-label")
            yield Input(value=str(defaults["base_url"]), id="base-url")
            yield Label("Model", classes="field-label")
            yield Input(value=str(defaults["model"]), id="model")
            yield Label("API key", classes="field-label")
            yield Input(password=True, id="api-key")
            yield Static("", id="error")
            with Horizontal(id="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save and continue", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#api-key", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(False)
            return
        if event.button.id != "save":
            return
        provider = self.query_one("#provider", Input).value.strip().lower()
        protocol = self.query_one("#protocol", Input).value.strip().lower()
        base_url = self.query_one("#base-url", Input).value.strip()
        model = self.query_one("#model", Input).value.strip()
        api_key = self.query_one("#api-key", Input).value.strip()
        try:
            save_provider(
                provider,
                protocol=protocol,
                base_url=base_url,
                model=model,
                api_key=api_key,
                make_default=True,
            )
        except ValueError as exc:
            self.query_one("#error", Static).update(str(exc))
            return
        self.exit(True)
