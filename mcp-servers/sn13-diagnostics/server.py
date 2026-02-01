#!/usr/bin/env python3
"""
SN13 Diagnostics MCP Server
===========================

MCP server providing diagnostic tools for SN13 miner.
Tools: scan_logs, get_miner_status, lookup_error, check_x_accounts
"""

import asyncio
import json
import re
import subprocess
import os
from pathlib import Path
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Config from environment or defaults
DATA_UNIVERSE_PATH = Path(os.environ.get(
    "DATA_UNIVERSE_PATH",
    "/home/afu/bittensor/data-universe"
))
ERROR_CATALOG_PATH = DATA_UNIVERSE_PATH / "scripts" / "error_catalog.json"
HOTKEY = "5Hg6xKtasfFdqx2XQV7dPuVBJFJXp9yEr58rMHSJ7zb5EDD3"

server = Server("sn13-diagnostics")


def load_error_catalog() -> dict:
    """Load error catalog from JSON."""
    if ERROR_CATALOG_PATH.exists():
        with open(ERROR_CATALOG_PATH) as f:
            return json.load(f)
    return {"version": "0", "errors": []}


def get_recent_logs(lines: int = 500) -> str:
    """Get recent miner logs from pm2."""
    try:
        result = subprocess.run(
            ["pm2", "logs", "sn13-miner", "--lines", str(lines), "--nostream"],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout + result.stderr
    except Exception as e:
        return f"Error getting logs: {e}"


@server.list_tools()
async def list_tools():
    """List available diagnostic tools."""
    return [
        Tool(
            name="scan_logs",
            description="Scan miner logs for known error patterns from error catalog",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to scan (default 500)",
                        "default": 500
                    }
                }
            }
        ),
        Tool(
            name="get_miner_status",
            description="Get current miner health status including pm2 process and metagraph position",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="lookup_error",
            description="Look up a known error by ID or search pattern",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Error ID (e.g. X001) or search text"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="check_x_accounts",
            description="Verify X (Twitter) account cookies are valid",
            inputSchema={
                "type": "object",
                "properties": {
                    "accounts": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Account numbers to check (1-8), default all"
                    }
                }
            }
        ),
        Tool(
            name="get_data_stats",
            description="Get current data counts from miner database",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_validator_report",
            description="Get validator scoring report for our miner (parses wandb/exported logs)",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Path to exported validator logs JSON file (optional)"
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours of history to analyze from wandb (default 24)",
                        "default": 24
                    }
                }
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Handle tool calls."""

    if name == "scan_logs":
        lines = arguments.get("lines", 500)
        catalog = load_error_catalog()
        logs = get_recent_logs(lines)

        found = []
        for err in catalog.get("errors", []):
            pattern = err.get("pattern", "")
            if pattern:
                try:
                    if re.search(pattern, logs, re.IGNORECASE):
                        found.append({
                            "id": err.get("id"),
                            "category": err.get("category"),
                            "severity": err.get("severity"),
                            "root_cause": err.get("root_cause"),
                            "fix": err.get("fix")
                        })
                except re.error:
                    pass

        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "lines_scanned": lines,
            "catalog_version": catalog.get("version"),
            "errors_found": found,
            "count": len(found),
            "status": "healthy" if not found else (
                "critical" if any(e["severity"] == "high" for e in found)
                else "warning"
            )
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_miner_status":
        # Check pm2 status
        try:
            pm2_result = subprocess.run(
                ["pm2", "jlist"],
                capture_output=True,
                text=True,
                timeout=10
            )
            processes = json.loads(pm2_result.stdout)
            pm2_status = {"found": False}
            for proc in processes:
                if proc.get("name") == "sn13-miner":
                    pm2_status = {
                        "found": True,
                        "status": proc.get("pm2_env", {}).get("status"),
                        "uptime_ms": proc.get("pm2_env", {}).get("pm_uptime"),
                        "restarts": proc.get("pm2_env", {}).get("restart_time", 0),
                        "memory_mb": round(proc.get("monit", {}).get("memory", 0) / 1024 / 1024, 1),
                        "cpu": proc.get("monit", {}).get("cpu")
                    }
                    break
        except Exception as e:
            pm2_status = {"error": str(e)}

        # Check metagraph (async-safe via subprocess)
        try:
            script = f"""
import bittensor as bt
import json
m = bt.subtensor('finney').metagraph(13)
for u in range(len(m.hotkeys)):
    if m.hotkeys[u] == '{HOTKEY}':
        print(json.dumps({{'uid': u, 'incentive': float(m.incentive[u]), 'trust': float(m.trust[u]), 'rank': float(m.ranks[u])}}))
        break
else:
    print(json.dumps({{'error': 'not_found'}}))
"""
            meta_result = subprocess.run(
                [str(DATA_UNIVERSE_PATH / "venv" / "bin" / "python"), "-c", script],
                capture_output=True,
                text=True,
                timeout=60
            )
            metagraph = json.loads(meta_result.stdout.strip()) if meta_result.stdout.strip() else {"error": meta_result.stderr}
        except Exception as e:
            metagraph = {"error": str(e)}

        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "pm2": pm2_status,
            "metagraph": metagraph,
            "overall": "healthy" if pm2_status.get("status") == "online" else "critical"
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "lookup_error":
        query = arguments.get("query", "").upper()
        catalog = load_error_catalog()

        matches = []
        for err in catalog.get("errors", []):
            # Match by ID
            if err.get("id", "").upper() == query:
                matches.append(err)
            # Match by text in any field
            elif query.lower() in json.dumps(err).lower():
                matches.append(err)

        result = {
            "query": query,
            "matches": matches,
            "count": len(matches)
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "check_x_accounts":
        accounts = arguments.get("accounts", list(range(1, 9)))
        results = []

        for acc_num in accounts:
            suffix = "" if acc_num == 1 else f"_account{acc_num}"
            cookie_file = DATA_UNIVERSE_PATH / f"twitter_cookies{suffix}.json"

            if not cookie_file.exists():
                results.append({"account": acc_num, "status": "missing", "file": str(cookie_file)})
                continue

            # Test cookie validity using user() - more reliable than search
            script = f"""
import asyncio, json
from twikit import Client
async def test():
    try:
        c = Client('en-US')
        c.set_cookies(json.load(open('{cookie_file}')))
        u = await c.user()
        return 'valid:' + (u.screen_name if u else 'unknown')
    except Exception as e:
        return 'error:' + str(e)[:80]
print(asyncio.run(test()))
"""
            try:
                test_result = subprocess.run(
                    [str(DATA_UNIVERSE_PATH / "venv" / "bin" / "python"), "-c", script],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                output = test_result.stdout.strip()
                if output.startswith("valid:"):
                    username = output.split(":", 1)[1]
                    results.append({"account": acc_num, "status": "valid", "username": username})
                else:
                    msg = output.split(":", 1)[1] if ":" in output else output
                    results.append({"account": acc_num, "status": "error", "message": msg})
            except Exception as e:
                results.append({"account": acc_num, "status": "error", "message": str(e)[:100]})

        valid_count = sum(1 for r in results if r["status"] == "valid")
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "accounts": results,
            "valid": valid_count,
            "total": len(accounts),
            "overall": "healthy" if valid_count == len(accounts) else (
                "critical" if valid_count == 0 else "degraded"
            )
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_data_stats":
        # Query SQLite for record counts
        db_path = DATA_UNIVERSE_PATH / "SqliteMinerStorage.sqlite"
        if not db_path.exists():
            return [TextContent(type="text", text=json.dumps({"error": "Database not found"}))]

        script = f"""
import sqlite3
import json
conn = sqlite3.connect('{db_path}')
c = conn.cursor()
reddit = c.execute("SELECT COUNT(*) FROM DataEntity WHERE source=2").fetchone()[0]
x = c.execute("SELECT COUNT(*) FROM DataEntity WHERE source=1").fetchone()[0]
total = c.execute("SELECT COUNT(*) FROM DataEntity").fetchone()[0]
# Get DB size
import os
size_mb = round(os.path.getsize('{db_path}') / 1024 / 1024, 1)
print(json.dumps({{'reddit': reddit, 'x': x, 'total': total, 'size_mb': size_mb}}))
"""
        try:
            result = subprocess.run(
                [str(DATA_UNIVERSE_PATH / "venv" / "bin" / "python"), "-c", script],
                capture_output=True,
                text=True,
                timeout=30
            )
            stats = json.loads(result.stdout.strip())
            stats["timestamp"] = datetime.utcnow().isoformat()
            return [TextContent(type="text", text=json.dumps(stats, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elif name == "get_validator_report":
        # Run validator_monitor.py to analyze logs
        file_path = arguments.get("file")
        hours = arguments.get("hours", 24)

        cmd = [str(DATA_UNIVERSE_PATH / "venv" / "bin" / "python"),
               str(DATA_UNIVERSE_PATH / "scripts" / "validator_monitor.py"),
               "--json"]

        if file_path:
            cmd.extend(["--file", file_path])
        else:
            cmd.append("--wandb")
            cmd.extend(["--hours", str(hours)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                stats = json.loads(result.stdout.strip())
                stats["timestamp"] = datetime.utcnow().isoformat()
                return [TextContent(type="text", text=json.dumps(stats, indent=2))]
            else:
                return [TextContent(type="text", text=json.dumps({
                    "error": result.stderr or "validator_monitor.py failed",
                    "note": "Try providing --file path to exported validator logs JSON"
                }))]
        except json.JSONDecodeError:
            return [TextContent(type="text", text=json.dumps({
                "error": "Could not parse validator monitor output",
                "raw": result.stdout[:500] if result.stdout else None
            }))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
