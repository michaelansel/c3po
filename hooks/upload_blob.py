#!/usr/bin/env python3
"""
C3PO PreToolUse Hook - Auto-upload files attached to send_message.

When send_message includes a `file_path` parameter, this hook:
1. Reads the file at file_path
2. Uploads it to the coordinator as a blob via REST
3. Prepends [blob:{blob_id}:{filename}] to the message text
4. Removes file_path from the tool input

Falls through (allows send_message) if upload fails, with warning to stderr.

Exit codes:
- 0: Always (with JSON output to rewrite or allow)
"""

import json
import os
import sys
import urllib.request

from c3po_common import auth_headers, get_coordinator_url, get_ssl_context


def _log(msg: str) -> None:
    """Write to stderr for diagnostics."""
    print(f"[c3po:upload_blob] {msg}", file=sys.stderr)


def main() -> None:
    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = stdin_data.get("tool_name", "")

    # Only intercept send_message with file_path
    if tool_name != "mcp__c3po__send_message":
        sys.exit(0)

    tool_input = stdin_data.get("tool_input", {})
    file_path = tool_input.get("file_path")

    if not file_path:
        sys.exit(0)

    # Validate file exists and check size
    if not os.path.isfile(file_path):
        _log(f"WARNING: file_path does not exist: {file_path}")
        # Remove file_path and let send_message proceed without it
        updated = dict(tool_input)
        del updated["file_path"]
        _output_updated(updated)
        return

    file_size = os.path.getsize(file_path)
    max_size = 5 * 1024 * 1024  # 5MB

    if file_size > max_size:
        _log(f"WARNING: file too large ({file_size} bytes > {max_size} bytes): {file_path}")
        updated = dict(tool_input)
        del updated["file_path"]
        _output_updated(updated)
        return

    if file_size == 0:
        _log(f"WARNING: file is empty: {file_path}")
        updated = dict(tool_input)
        del updated["file_path"]
        _output_updated(updated)
        return

    # Read file content
    try:
        with open(file_path, "rb") as f:
            content = f.read()
    except (IOError, PermissionError) as e:
        _log(f"WARNING: cannot read file: {e}")
        updated = dict(tool_input)
        del updated["file_path"]
        _output_updated(updated)
        return

    # Upload via REST
    filename = os.path.basename(file_path)
    coordinator_url = get_coordinator_url()
    url = f"{coordinator_url}/agent/api/blob"

    headers = auth_headers()
    headers["Content-Type"] = "application/octet-stream"
    headers["X-Filename"] = filename

    try:
        req = urllib.request.Request(url, data=content, headers=headers, method="POST")
        ssl_ctx = get_ssl_context()
        if ssl_ctx:
            resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
        else:
            resp = urllib.request.urlopen(req, timeout=30)

        result = json.loads(resp.read().decode())
        blob_id = result["blob_id"]
        _log(f"Uploaded {filename} as {blob_id} ({len(content)} bytes)")

        # Rewrite tool input: prepend blob reference to message, remove file_path
        updated = dict(tool_input)
        del updated["file_path"]
        original_message = updated.get("message", "")
        updated["message"] = f"[blob:{blob_id}:{filename}] {original_message}"

        _output_updated(updated)

    except Exception as e:
        _log(f"WARNING: blob upload failed: {e}")
        # Fall through: remove file_path and let send_message proceed
        updated = dict(tool_input)
        del updated["file_path"]
        _output_updated(updated)


def _output_updated(updated_input: dict) -> None:
    """Output JSON to rewrite the tool input."""
    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
