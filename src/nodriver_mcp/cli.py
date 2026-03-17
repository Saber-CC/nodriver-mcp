"""
CLI entry point for nodriver-mcp.

Usage:
    nodriver-mcp                     # Run the MCP server (stdio)
    nodriver-mcp install             # Interactive install
    nodriver-mcp install claude,kiro # Install to specific clients
    nodriver-mcp uninstall           # Interactive uninstall
    nodriver-mcp uninstall cursor    # Uninstall from specific client
    nodriver-mcp --list-clients      # List available clients
    nodriver-mcp --config            # Print MCP config JSON
"""

import argparse
import sys

from .installer import (
    list_available_clients,
    print_mcp_config,
    run_install_command,
)


def main():
    parser = argparse.ArgumentParser(
        prog="nodriver-mcp",
        description="Undetected Chrome automation MCP server powered by nodriver",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Print MCP config JSON",
    )
    parser.add_argument(
        "--list-clients",
        action="store_true",
        help="List all available MCP client targets",
    )
    parser.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Installation scope: global (user-level, default) or project (current directory)",
    )

    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser("install", help="Install MCP server to clients")
    install_parser.add_argument(
        "targets",
        nargs="?",
        default="",
        help="Comma-separated client list, e.g. claude,cursor,kiro. Leave empty for interactive mode",
    )

    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall MCP server from clients")
    uninstall_parser.add_argument(
        "targets",
        nargs="?",
        default="",
        help="Comma-separated client list",
    )

    args = parser.parse_args()

    if args.list_clients:
        list_available_clients()
        return

    if args.config:
        print_mcp_config()
        return

    if args.command == "install":
        run_install_command(
            uninstall=False,
            targets_str=args.targets,
            project=(args.scope == "project"),
        )
        return

    if args.command == "uninstall":
        run_install_command(
            uninstall=True,
            targets_str=args.targets,
            project=(args.scope == "project"),
        )
        return

    # Default: run the MCP server
    from .server import main as server_main
    server_main()
