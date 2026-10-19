"""State-based form controls using native DOM state and trusted mouse input."""

import asyncio


_TOGGLE_STATE = """el => {
    const native = el instanceof HTMLInputElement && ['checkbox','radio'].includes(el.type);
    const role = el.getAttribute('role');
    const kind = native ? el.type : ['checkbox','radio','switch'].includes(role) ? role : null;
    const aria = el.getAttribute('aria-checked');
    return {kind, native, checked: native ? el.checked : aria === 'true' ? true : aria === 'false' ? false : null,
            mixed: native ? el.indeterminate : aria === 'mixed',
            disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
            readonly: el.getAttribute('aria-readonly') === 'true'};
}"""


def _desired_state(value) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("true", "false") and isinstance(value, str):
        return value == "true"
    raise ValueError(
        "Checkboxes, radios and switches require a boolean or the string 'true'/'false'."
    )


def _matches(state, desired):
    return state["checked"] is desired and not state["mixed"]


async def fill_toggle(element, value, click) -> bool:
    """Return False for text controls; otherwise set and verify a toggle state."""
    initial = await element.apply(_TOGGLE_STATE)
    if not initial["kind"]:
        return False
    desired = _desired_state(value)
    if _matches(initial, desired):
        return True
    if initial["kind"] == "radio" and not desired:
        raise ValueError(
            "Cannot uncheck a selected radio; select another option in its group."
        )
    if initial["disabled"] or initial["readonly"]:
        raise ValueError("Cannot change a disabled or read-only form control.")
    if not initial["native"] and initial["checked"] is None and not initial["mixed"]:
        raise ValueError("ARIA toggles must expose their state through aria-checked.")
    # Native indeterminate checkboxes may first clear 'mixed' while toggling
    # away from the desired checked value; a second verified click can be needed.
    attempts = (
        2
        if initial["native"] and initial["kind"] == "checkbox" and initial["mixed"]
        else 1
    )
    for attempt in range(attempts):
        await click()
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            state = await element.apply(_TOGGLE_STATE)
            if _matches(state, desired):
                return True
            if attempts == 2 and attempt == 0 and not state["mixed"]:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.02)
    actual = "mixed" if state["mixed"] else state["checked"]
    raise ValueError(
        f"Control did not reach checked={desired}; actual state is {actual}."
    )
