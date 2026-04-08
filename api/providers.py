"""Providers API — returns the list of available chat providers for the config UI dropdown.

Endpoint: POST /plugins/a0_playwright_cli/providers
Response: {"providers": [{"value": "openai", "label": "OpenAI"}, ...]}

Dynamically reads from A0's model_providers.yaml (including any plugin-contributed providers),
so the dropdown always stays in sync with whatever providers are registered in the system.
"""
from helpers.api import ApiHandler, Request, Response
from helpers.providers import get_providers


class Providers(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        # Pass the literal string "chat" — helpers/providers.py uses Literal["chat","embedding"]
        raw = get_providers("chat")
        providers = [
            {"value": p.get("value", ""), "label": p.get("label", p.get("value", ""))}
            for p in raw
            if p.get("value")
        ]
        return {"providers": providers}
