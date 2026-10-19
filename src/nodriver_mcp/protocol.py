"""JSON adapters for the official generated nodriver CDP bindings."""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import re
import types
import typing
from collections.abc import Mapping
from functools import cache

from nodriver import cdp


def json_value(value: typing.Any) -> typing.Any:
    if isinstance(value, enum.Enum):
        return value.value
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if hasattr(value, "to_json"):
        return json_value(value.to_json())
    if dataclasses.is_dataclass(value):
        return {
            field.name: json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    return value


def wire_name(name: str) -> str:
    parts = name.rstrip("_").split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def _identifier(name: str) -> str:
    """Match Python and CDP names without guessing acronym capitalization."""
    return name.replace("_", "").lower()


@cache
def _parameter_wire_names(fn: typing.Callable) -> dict[str, str]:
    """Read exact JSON keys from the official generated serializer."""
    tree = ast.parse(inspect.getsource(inspect.unwrap(fn)))
    return {
        _identifier(node.slice.value): node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "params"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }


def domain_module(domain: str) -> types.ModuleType:
    normalized = re.sub(r"[^a-z0-9]", "", domain.lower())
    for name, module in vars(cdp).items():
        if isinstance(module, types.ModuleType) and module.__name__.startswith(
            "nodriver.cdp."
        ):
            if re.sub(r"[^a-z0-9]", "", name.lower()) == normalized:
                return module
    raise ValueError(f"Unknown CDP domain: {domain}")


def command_functions(module: types.ModuleType) -> dict[str, typing.Callable]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == module.__name__
        and inspect.isgeneratorfunction(inspect.unwrap(value))
    }


def command_function(domain: str, command: str) -> typing.Callable:
    module = domain_module(domain)
    normalized = _identifier(command.rsplit(".", 1)[-1])
    for name, fn in command_functions(module).items():
        if normalized == _identifier(name):
            return fn
    raise ValueError(f"Unknown command {domain}.{command}; use list_cdp_commands.")


def typed_value(value: typing.Any, annotation: typing.Any) -> typing.Any:
    """Construct the official ID, enum or dataclass from JSON values."""
    if annotation in (typing.Any, inspect.Parameter.empty):
        return value
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        for option in args:
            if option is type(None):
                continue
            try:
                return typed_value(value, option)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        raise ValueError(f"Value does not match {annotation}")
    if value is None:
        raise ValueError(f"null is not valid for {annotation}")
    if origin is list:
        if not isinstance(value, list):
            raise ValueError("Expected a JSON array")
        return [typed_value(item, args[0]) for item in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return {
            typed_value(k, args[0]): typed_value(v, args[1]) for k, v in value.items()
        }
    if annotation in (str, int, float, bool, dict):
        expected = (int, float) if annotation is float else annotation
        if not isinstance(value, expected) or (
            annotation in (int, float) and isinstance(value, bool)
        ):
            raise ValueError(f"Expected {annotation.__name__}")
        return value
    if isinstance(annotation, type) and isinstance(value, annotation):
        return value
    if hasattr(annotation, "from_json"):
        return annotation.from_json(value)
    raise ValueError(f"Unsupported parameter type {annotation}")


def build_command(domain: str, command: str, parameters: dict | None = None):
    fn = command_function(domain, command)
    signature = inspect.signature(fn)
    annotations = typing.get_type_hints(fn)
    names = {_identifier(name): name for name in signature.parameters}
    kwargs = {}
    for name, value in (parameters or {}).items():
        python_name = names.get(_identifier(name))
        if python_name is None:
            raise ValueError(f"Unknown parameter '{name}' for {domain}.{command}")
        if python_name in kwargs:
            raise ValueError(f"Duplicate parameter '{name}'")
        kwargs[python_name] = typed_value(
            value, annotations.get(python_name, typing.Any)
        )
    signature.bind(**kwargs)
    return fn(**kwargs)


def describe_commands(domain: str = "", query: str = "") -> list[dict]:
    modules = (
        [domain_module(domain)]
        if domain
        else [
            module
            for module in vars(cdp).values()
            if isinstance(module, types.ModuleType)
            and module.__name__.startswith("nodriver.cdp.")
        ]
    )
    commands = []
    for module in modules:
        for name, fn in command_functions(module).items():
            description = inspect.getdoc(fn) or ""
            full_name = module.__name__.rsplit(".", 1)[-1] + "." + name
            if query and query.lower() not in (full_name + " " + description).lower():
                continue
            commands.append(
                {
                    "command": full_name,
                    "description": description,
                    "parameters": [
                        {
                            "name": p.name,
                            "cdp_name": _parameter_wire_names(fn).get(
                                _identifier(p.name), wire_name(p.name)
                            ),
                            "type": str(p.annotation),
                            "required": p.default is p.empty,
                            **(
                                {"default": json_value(p.default)}
                                if p.default is not p.empty
                                else {}
                            ),
                        }
                        for p in inspect.signature(fn).parameters.values()
                    ],
                }
            )
    return sorted(commands, key=lambda entry: entry["command"])


def event_types(domain: str = "") -> dict[str, type]:
    events = cdp.util._event_parsers
    if not domain:
        return dict(events)
    module = domain_module(domain)
    return {
        name: cls for name, cls in events.items() if cls.__module__ == module.__name__
    }


def resolve_event(name: str) -> tuple[str, type]:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    for method, cls in event_types().items():
        aliases = [method, cls.__module__.rsplit(".", 1)[-1] + "." + cls.__name__]
        if any(
            re.sub(r"[^a-z0-9]", "", alias.lower()) == normalized for alias in aliases
        ):
            return method, cls
    raise ValueError(f"Unknown CDP event '{name}'; use list_cdp_events.")
