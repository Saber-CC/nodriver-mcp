"""
MCP client configuration paths for all supported platforms.
"""

import os
import sys

MCP_SERVER_NAME = "nodriver"

#: DeepSeek Harness keeps a YAML patch layer instead of a JSON/TOML settings file.
DSH_CLIENT_NAME = "DeepSeek Harness"
DSH_PROFILE_FILENAME = "cordis.patch.yml"

CLIENT_ALIASES: dict[str, str] = {
    "vscode": "VS Code",
    "vs-code": "VS Code",
    "deepseek": "DeepSeek Harness",
    "deepseek-harness": "DeepSeek Harness",
    "deepseekharness": "DeepSeek Harness",
    "dsh": "DeepSeek Harness",
    "harness": "DeepSeek Harness",
    "claude": "Claude Desktop",
    "claude-desktop": "Claude Desktop",
    "claude-app": "Claude Desktop",
    "claude-code": "Claude Code",
    "roo": "Roo Code",
    "roocode": "Roo Code",
    "gemini": "Gemini CLI",
    "copilot": "Copilot CLI",
    "amazonq": "Amazon Q",
    "amazon-q": "Amazon Q",
    "lmstudio": "LM Studio",
    "lm-studio": "LM Studio",
    "augment": "Augment Code",
}

# Special JSON structures for global configs
GLOBAL_SPECIAL_JSON_STRUCTURES: dict[str, tuple[str | None, str]] = {
    "VS Code": ("mcp", "servers"),
    "Opencode": (None, "mcp"),
}

# Project-level config definitions
PROJECT_LEVEL_CONFIGS: dict[str, tuple[str, str]] = {
    "Claude Code": ("", ".mcp.json"),
    "Cursor": (".cursor", "mcp.json"),
    "VS Code": (".vscode", "mcp.json"),
    "Windsurf": (".windsurf", "mcp.json"),
}

PROJECT_SPECIAL_JSON_STRUCTURES: dict[str, tuple[str | None, str]] = {
    "VS Code": (None, "servers"),
}


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def get_dsh_profile_dir() -> str:
    """Directory of the DeepSeek Harness profile this install should target.

    The running harness exports ``DSH_PROFILE`` (and ``DSH_HOME``) to every shell
    it launches, so an install started from a harness terminal lands in the
    profile that launched it. An explicit ``DSH_PROFILE`` wins even when its
    directory does not exist yet, so the installer reports that path instead of
    silently choosing another profile. Without it, the desktop profile, the web
    profile and then the first profile in name order are tried; a machine with no
    profile at all falls back to the default name and reports it as missing.
    """
    home = os.environ.get("DSH_HOME") or _home(".dsh")
    profiles = os.path.join(home, "profiles")
    selected = os.environ.get("DSH_PROFILE", "").strip()
    if selected:
        return os.path.join(profiles, selected)
    for candidate in ("desktop", "web"):
        if os.path.isdir(os.path.join(profiles, candidate)):
            return os.path.join(profiles, candidate)
    try:
        existing = sorted(
            name
            for name in os.listdir(profiles)
            if os.path.isdir(os.path.join(profiles, name))
        )
    except OSError:
        existing = []
    if existing:
        return os.path.join(profiles, existing[0])
    return os.path.join(profiles, "desktop")


def get_global_configs() -> dict[str, tuple[str, str]]:
    if sys.platform == "darwin":
        return {
            "Claude Desktop": (
                _home("Library", "Application Support", "Claude"),
                "claude_desktop_config.json",
            ),
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                _home("Library", "Application Support", "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                _home("Library", "Application Support", "Code", "User",
                      "globalStorage", "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                _home("Library", "Application Support", "Code", "User",
                      "globalStorage", "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
            "DeepSeek Harness": (get_dsh_profile_dir(), DSH_PROFILE_FILENAME),
        }
    elif sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        return {
            "Claude Desktop": (
                os.path.join(appdata, "Claude"),
                "claude_desktop_config.json",
            ),
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                os.path.join(appdata, "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                os.path.join(appdata, "Code", "User", "globalStorage",
                             "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                os.path.join(appdata, "Code", "User", "globalStorage",
                             "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
            "DeepSeek Harness": (get_dsh_profile_dir(), DSH_PROFILE_FILENAME),
        }
    elif sys.platform == "linux":
        return {
            "Claude Code": (_home(), ".claude.json"),
            "Cursor": (_home(".cursor"), "mcp.json"),
            "Windsurf": (_home(".codeium", "windsurf"), "mcp_config.json"),
            "Codex": (_home(".codex"), "config.toml"),
            "Gemini CLI": (_home(".gemini"), "settings.json"),
            "Copilot CLI": (_home(".copilot"), "mcp-config.json"),
            "Kiro": (_home(".kiro", "settings"), "mcp.json"),
            "VS Code": (
                _home(".config", "Code", "User"),
                "settings.json",
            ),
            "Cline": (
                _home(".config", "Code", "User", "globalStorage",
                      "saoudrizwan.claude-dev", "settings"),
                "cline_mcp_settings.json",
            ),
            "Roo Code": (
                _home(".config", "Code", "User", "globalStorage",
                      "rooveterinaryinc.roo-cline", "settings"),
                "mcp_settings.json",
            ),
            "Amazon Q": (_home(".aws", "amazonq"), "mcp_config.json"),
            "Warp": (_home(".warp"), "mcp_config.json"),
            "Opencode": (_home(".config", "opencode"), "opencode.json"),
            "Trae": (_home(".trae"), "mcp_config.json"),
            "DeepSeek Harness": (get_dsh_profile_dir(), DSH_PROFILE_FILENAME),
        }
    return {}


def get_project_configs(project_dir: str) -> dict[str, tuple[str, str]]:
    result = {}
    for name, (subdir, config_file) in PROJECT_LEVEL_CONFIGS.items():
        config_dir = os.path.join(project_dir, subdir) if subdir else project_dir
        result[name] = (config_dir, config_file)
    return result


def resolve_client_name(input_name: str, available: list[str]) -> str | None:
    lower = input_name.strip().lower()
    for c in available:
        if c.lower() == lower:
            return c
    if lower in CLIENT_ALIASES:
        target = CLIENT_ALIASES[lower]
        if target in available:
            return target
    matches = [c for c in available if lower in c.lower()]
    return matches[0] if len(matches) == 1 else None
