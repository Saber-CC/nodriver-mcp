import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nodriver_mcp.forms import fill_toggle


class FormStateTests(unittest.IsolatedAsyncioTestCase):
    def element(self, **options):
        state = {
            "kind": "checkbox",
            "native": True,
            "checked": False,
            "mixed": False,
            "disabled": False,
            "readonly": False,
            **options,
        }
        element = SimpleNamespace(apply=AsyncMock(side_effect=lambda _js: dict(state)))
        return element, state

    async def test_target_state_is_idempotent_and_non_toggles_are_left_for_text_fill(
        self,
    ):
        element, _ = self.element(checked=True, disabled=True)
        click = AsyncMock()
        self.assertTrue(await fill_toggle(element, "true", click))
        click.assert_not_awaited()
        element, _ = self.element(kind=None)
        self.assertFalse(await fill_toggle(element, "text", click))

    async def test_invalid_values_selected_radios_and_disabled_controls_do_not_click(
        self,
    ):
        click = AsyncMock()
        for value in ("yes", "TRUE", 1, None):
            element, _ = self.element()
            with self.assertRaisesRegex(ValueError, "boolean"):
                await fill_toggle(element, value, click)
        for options, value, message in (
            ({"kind": "radio", "checked": True}, False, "Cannot uncheck"),
            ({"disabled": True}, True, "disabled"),
            ({"readonly": True}, True, "read-only"),
            ({"native": False, "checked": None}, True, "aria-checked"),
        ):
            element, _ = self.element(**options)
            with self.assertRaisesRegex(ValueError, message):
                await fill_toggle(element, value, click)
        click.assert_not_awaited()

    async def test_delayed_aria_state_and_native_mixed_state_are_verified(self):
        element, state = self.element(native=False)

        async def delayed_click():
            asyncio.get_running_loop().call_later(
                0.02, lambda: state.update(checked=True)
            )

        self.assertTrue(await fill_toggle(element, True, delayed_click))
        element, state = self.element(checked=True, mixed=True)

        async def click():
            state.update(checked=not state["checked"], mixed=False)

        click = AsyncMock(side_effect=click)
        self.assertTrue(await fill_toggle(element, True, click))
        self.assertEqual(click.await_count, 2)

    async def test_failed_click_retains_its_original_failure(self):
        element, _ = self.element()
        error = RuntimeError("Disconnected while clicking")
        with self.assertRaises(RuntimeError) as caught:
            await fill_toggle(element, True, AsyncMock(side_effect=error))
        self.assertIs(caught.exception, error)
