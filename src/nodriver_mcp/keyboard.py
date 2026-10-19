"""Keyboard names and trusted input using official nodriver CDP commands."""

from __future__ import annotations

import string
import sys
from dataclasses import dataclass

from nodriver import Tab, cdp


@dataclass(frozen=True)
class Key:
    key: str
    code: str
    key_code: int
    text: str = ""
    location: int = 0


_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_MAC_COMMANDS = {
    (4, "a"): "selectAll",
    (4, "c"): "copy",
    (4, "x"): "cut",
    (4, "v"): "paste",
    (4, "z"): "undo",
    (12, "z"): "redo",
    (12, "v"): "pasteAndMatchStyle",
    (4, "ArrowLeft"): "moveToBeginningOfLine",
    (4, "ArrowRight"): "moveToEndOfLine",
    (4, "ArrowUp"): "moveToBeginningOfDocument",
    (4, "ArrowDown"): "moveToEndOfDocument",
    (1, "ArrowLeft"): "moveWordBackward",
    (1, "ArrowRight"): "moveWordForward",
    (1, "Backspace"): "deleteWordBackward",
    (1, "Delete"): "deleteWordForward",
    (4, "Backspace"): "deleteToBeginningOfLine",
    (4, "Delete"): "deleteToEndOfLine",
}
_KEYS = {
    name: Key(name, name, key_code)
    for name, key_code in (
        ("Backspace", 8),
        ("Tab", 9),
        ("Escape", 27),
        ("Pause", 19),
        ("CapsLock", 20),
        ("PageUp", 33),
        ("PageDown", 34),
        ("End", 35),
        ("Home", 36),
        ("ArrowLeft", 37),
        ("ArrowUp", 38),
        ("ArrowRight", 39),
        ("ArrowDown", 40),
        ("PrintScreen", 44),
        ("Insert", 45),
        ("Delete", 46),
        ("ContextMenu", 93),
        ("NumLock", 144),
        ("ScrollLock", 145),
    )
}
_KEYS.update(
    Enter=Key("Enter", "Enter", 13, "\r"),
    Space=Key(" ", "Space", 32, " "),
    NumpadEnter=Key("Enter", "NumpadEnter", 13, "\r", 3),
)
_KEYS[" "] = _KEYS["Space"]
_KEYS["\r"] = _KEYS["\n"] = _KEYS["Enter"]
for _name, _code in (("Shift", 16), ("Control", 17), ("Alt", 18), ("Meta", 91)):
    for _side, _location in (("Left", 1), ("Right", 2)):
        _KEYS[_name + _side] = Key(
            _name,
            _name + _side,
            92 if _name == "Meta" and _side == "Right" else _code,
            location=_location,
        )
    _KEYS[_name] = _KEYS[_name + "Left"]
for _number in range(1, 25):
    _KEYS[f"F{_number}"] = Key(f"F{_number}", f"F{_number}", 111 + _number)

_SHIFTED = {}
for _letter in string.ascii_lowercase:
    _upper = _letter.upper()
    _KEYS[_letter] = _KEYS["Key" + _upper] = Key(
        _letter, "Key" + _upper, ord(_upper), _letter
    )
    _KEYS[_upper] = Key(_upper, "Key" + _upper, ord(_upper), _upper)
    _SHIFTED[_letter] = _upper
for _normal, _shift, _code, _key_code in (
    *(
        (digit, shifted, "Digit" + digit, ord(digit))
        for digit, shifted in zip("1234567890", "!@#$%^&*()")
    ),
    ("`", "~", "Backquote", 192),
    ("-", "_", "Minus", 189),
    ("=", "+", "Equal", 187),
    ("[", "{", "BracketLeft", 219),
    ("]", "}", "BracketRight", 221),
    ("\\", "|", "Backslash", 220),
    (";", ":", "Semicolon", 186),
    ("'", '"', "Quote", 222),
    (",", "<", "Comma", 188),
    (".", ">", "Period", 190),
    ("/", "?", "Slash", 191),
):
    _KEYS[_normal] = _KEYS[_code] = Key(_normal, _code, _key_code, _normal)
    _KEYS[_shift] = Key(_shift, _code, _key_code, _shift)
    _SHIFTED[_normal] = _shift
for _number in range(10):
    _KEYS[f"Numpad{_number}"] = Key(
        str(_number), f"Numpad{_number}", 96 + _number, str(_number), 3
    )
for _name, _text, _code in (
    ("NumpadMultiply", "*", 106),
    ("NumpadAdd", "+", 107),
    ("NumpadSubtract", "-", 109),
    ("NumpadDecimal", ".", 110),
    ("NumpadDivide", "/", 111),
    ("NumpadEqual", "=", 187),
):
    _KEYS[_name] = Key(_text, _name, _code, _text, 3)


def _resolve_key(name: str, modifiers: int = 0) -> Key:
    key = _KEYS.get(name)
    if key is None:
        if len(name) == 1 and name.isprintable():
            return Key(name, "", 0, name)
        raise ValueError(f"Unknown key {name!r}. Use a key name or type_text for text.")
    if (
        modifiers & _MODIFIER_BITS["Shift"]
        and key.location != 3
        and key.key in _SHIFTED
    ):
        return _KEYS[_SHIFTED[key.key]]
    return key


def _editing_commands(key: Key, modifiers: int) -> list[str] | None:
    if sys.platform != "darwin":
        return None
    name = key.key.lower() if len(key.key) == 1 else key.key
    command = _MAC_COMMANDS.get((modifiers, name))
    if command is None and modifiers & _MODIFIER_BITS["Shift"]:
        movement = _MAC_COMMANDS.get((modifiers & ~_MODIFIER_BITS["Shift"], name))
        if movement and movement.startswith("move"):
            command = movement + "AndModifySelection"
    return [command] if command else None


def parse_shortcut(shortcut: str) -> tuple[str, ...]:
    """Validate a chord before input, preserving '+' as a possible final key."""
    if not shortcut:
        raise ValueError("Supply a key or shortcut, such as Enter or Control+A.")
    remaining = shortcut
    tokens = []
    seen = set()
    while "+" in remaining and remaining != "+":
        prefix, _, remaining = remaining.partition("+")
        modifier = _resolve_key(prefix).key
        if modifier not in _MODIFIER_BITS:
            raise ValueError(
                f"Invalid modifier {prefix!r}. Use Control, Shift, Alt or Meta."
            )
        if modifier in seen:
            raise ValueError(f"Duplicate modifier {modifier!r}.")
        seen.add(modifier)
        tokens.append(prefix)
    target = _resolve_key(remaining)
    if target.key in seen:
        raise ValueError(f"Duplicate modifier {target.key!r}.")
    return (*tokens, remaining)


async def dispatch_shortcut(tab: Tab, tokens: tuple[str, ...]) -> None:
    """Send physical key data and release all attempted keys after any failure."""
    modifiers = 0
    pressed = []
    failure = None
    try:
        for token in tokens:
            key = _resolve_key(token, modifiers)
            modifiers |= _MODIFIER_BITS.get(key.key, 0)
            pressed.append(key)
            text = "" if modifiers & 7 else key.text
            await tab.send(
                cdp.input_.dispatch_key_event(
                    "keyDown" if text else "rawKeyDown",
                    modifiers=modifiers,
                    key=key.key,
                    code=key.code,
                    windows_virtual_key_code=key.key_code,
                    text=text or None,
                    unmodified_text=text or None,
                    location=key.location,
                    is_keypad=key.location == 3,
                    commands=_editing_commands(key, modifiers),
                )
            )
    except BaseException as exc:
        failure = exc
    finally:
        for key in reversed(pressed):
            modifiers &= ~_MODIFIER_BITS.get(key.key, 0)
            try:
                await tab.send(
                    cdp.input_.dispatch_key_event(
                        "keyUp",
                        modifiers=modifiers,
                        key=key.key,
                        code=key.code,
                        windows_virtual_key_code=key.key_code,
                        location=key.location,
                        is_keypad=key.location == 3,
                    )
                )
            except BaseException as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        raise failure
