"""State-based form controls using native DOM state and trusted mouse input."""

import asyncio
import json

from nodriver import cdp


_TOGGLE_STATE = """el => {
    const native = el instanceof HTMLInputElement && ['checkbox','radio'].includes(el.type);
    const role = el.getAttribute('role');
    const kind = native ? el.type : ['checkbox','radio','switch'].includes(role) ? role : null;
    const aria = el.getAttribute('aria-checked');
    return {kind, native, checked: native ? el.checked : aria === 'true' ? true : aria === 'false' ? false : null,
            mixed: native ? el.indeterminate : aria === 'mixed',
            disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
            readonly: (!native && !!el.readOnly) || el.getAttribute('aria-readonly') === 'true',
            tag: el.tagName.toLowerCase(), input_type: el.type || '', editable: !!el.isContentEditable,
            connected: el.isConnected, max_length: typeof el.maxLength === 'number' ? el.maxLength : -1,
            value: el.isContentEditable ? el.innerText : typeof el.value === 'string' ? el.value : null};
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


async def control_state(element):
    return await element.apply(_TOGGLE_STATE)


async def fill_toggle(element, value, click, *, initial=None) -> bool:
    """Return False for text controls; otherwise set and verify a toggle state."""
    initial = initial if initial is not None else await control_state(element)
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


async def fill_text(element, value: str, state):
    if state["disabled"] or state["readonly"]:
        raise ValueError("Cannot fill a disabled or read-only form control.")
    tag, kind, editable = state["tag"], state["input_type"], state["editable"]
    typeable = (
        tag == "textarea"
        or tag == "input"
        and kind in {"text", "url", "tel", "search", "password", "number", "email"}
    )
    special = tag == "input" and kind in {
        "date",
        "datetime-local",
        "month",
        "range",
        "time",
        "week",
        "color",
    }
    if not (typeable or editable or special or tag == "select"):
        raise ValueError(
            "Element is not an editable text, select or supported form control."
        )
    maximum = state["max_length"]
    if maximum >= 0 and len(value.encode("utf-16-le", "surrogatepass")) // 2 > maximum:
        raise ValueError(f"Value exceeds the control's maxlength={maximum}.")
    encoded = json.dumps(value)
    if tag == "select" or special or not value:
        await element.apply(
            """el => {
            const value = %s;
            if (el.tagName === 'SELECT' && value && !Array.from(el.options).some(o => o.value === value))
                throw new Error('No select option matches the requested value.');
            el.focus();
            if (el.isContentEditable) el.innerText=value;
            else {
                let prototype=Object.getPrototypeOf(el), setter;
                while (prototype && !setter) {
                    setter=Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                    prototype=Object.getPrototypeOf(prototype);
                }
                if (setter) setter.call(el, value); else el.value=value;
            }
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }"""
            % encoded
        )
    else:
        same = bool(value) and state["value"] == value
        focused = await element.apply(
            """el => {
            el.focus();
            if (!el.matches(':focus')) return false;
            if (el.isContentEditable) {
                const selection=el.ownerDocument.defaultView.getSelection();
                const range=el.ownerDocument.createRange(); range.selectNodeContents(el);
                if (%s) range.collapse(false);
                selection.removeAllRanges(); selection.addRange(range);
            } else if (el.type === 'number') {
                if (!%s) Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set.call(el,'');
            } else if (%s && (el.tagName === 'TEXTAREA' ||
                ['text','search','url','tel','password'].includes(el.type))) {
                el.setSelectionRange(el.value.length,el.value.length);
            } else if (!%s) el.select();
            return true;
        }"""
            % ((json.dumps(same),) * 4)
        )
        if not focused:
            raise ValueError(
                "The form control did not receive focus; no text was inserted."
            )
        if not same:
            if len(value.encode("utf-16-le", "surrogatepass")) // 2 >= 100:
                await element.tab.send(cdp.input_.insert_text(value))
            else:
                await element.send_keys(value)
    actual = await element.apply("el => el.isContentEditable ? el.innerText : el.value")
    expected = (
        value.replace("\r\n", "\n").replace("\r", "\n")
        if tag == "textarea" or editable
        else value
    )
    if actual != expected:
        raise ValueError("The form control did not retain the requested value.")
