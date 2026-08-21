"""Provider/model picker used by the Textual frontend."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..config import PROVIDER_DEFAULTS
from ..config.store import load_global_config


class ProviderModelPicker(ModalScreen[tuple[str, str] | None]):
    CSS = """
    ProviderModelPicker { align: center middle; background: rgba(0, 0, 0, 0.55); }
    #picker { width: 68; height: 28; border: round #4d8dff; background: #0f1117; padding: 1 2; }
    #picker-title { text-style: bold; color: #8fb8ff; margin-bottom: 1; }
    #provider-list { height: 12; border: solid #30394d; }
    #picker-actions { height: 3; margin-top: 1; align-horizontal: right; }
    #picker-error { color: #ff8585; height: 1; }
    """

    def __init__(self, current_provider: str, current_model: str):
        super().__init__()
        self.current_provider = str(current_provider or "").strip().lower()
        self.current_model = str(current_model or "").strip()
        config = load_global_config()
        profiles = dict(config.get("providers", {}))
        if self.current_provider and self.current_provider not in profiles:
            profiles[self.current_provider] = {"model": self.current_model}
        self.profiles = profiles
        self.selected_provider = self.current_provider or next(iter(profiles), "")

    def compose(self) -> ComposeResult:
        options = []
        for name, profile in sorted(self.profiles.items()):
            model = str(profile.get("model", "") or PROVIDER_DEFAULTS.get(name, {}).get("model", ""))
            options.append(Option(f"{name}  ({model or '-'})", id=name))
        with Container(id="picker"):
            yield Static("Select provider and model", id="picker-title")
            yield OptionList(*options, id="provider-list")
            yield Label("Model")
            yield Input(value=self._selected_model(), id="picker-model")
            yield Static("", id="picker-error")
            with Horizontal(id="picker-actions"):
                yield Button("Cancel", id="picker-cancel")
                yield Button("Use for this project", id="picker-apply", variant="primary")

    def on_mount(self) -> None:
        option_list = self.query_one("#provider-list", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == self.selected_provider:
                option_list.highlighted = index
                break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.selected_provider = str(event.option.id or "")
        self.query_one("#picker-model", Input).value = self._selected_model()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-cancel":
            self.dismiss(None)
            return
        if event.button.id != "picker-apply":
            return
        model = self.query_one("#picker-model", Input).value.strip()
        if not self.selected_provider or not model:
            self.query_one("#picker-error", Static).update(
                "Select a provider and enter a model."
            )
            return
        self.dismiss((self.selected_provider, model))

    def key_escape(self) -> None:
        self.dismiss(None)

    def _selected_model(self) -> str:
        profile = self.profiles.get(self.selected_provider, {})
        return str(
            profile.get("model", "")
            or PROVIDER_DEFAULTS.get(self.selected_provider, {}).get("model", "")
            or self.current_model
        )
