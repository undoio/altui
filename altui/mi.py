from typing import Any

from pygdbmi import gdbmiparser
from src.udbpy import comms, textutil  # type: ignore[import]
from src.udbpy.gdb_extensions import gdbutils  # type: ignore[import]


class MIError(Exception):
    pass


class MIErrorResponse(MIError):
    pass


class MIInvalidResponse(MIError):
    pass


def parse_response(text: str) -> dict[str, Any]:
    result = None

    for line in text.strip().splitlines():
        record = gdbmiparser.parse_response(line)
        try:
            name = record.get("type")
        except KeyError as exc:
            raise MIInvalidResponse(f'Missing record name ("type" field): {line!r}') from exc
        if name != "result":
            continue
        message = record.get("message")  # message can be missing from valid responses.
        if message not in ("done", "error"):
            continue
        try:
            payload = record["payload"]
        except KeyError as exc:
            raise MIInvalidResponse(f"Missing record payload: {line!r}") from exc
        if message == "error":
            raise MIErrorResponse(payload.get("msg", "Unknown error."))
        if payload is not None and result is not None:
            raise MIInvalidResponse(
                f"Multiple responses found:\nFirst: {result!r}\nSecond: {payload!r}"
            )
        result = payload

    return result or {}


def execute(command: str) -> dict[str, Any]:
    try:
        text = gdbutils.execute_to_string(
            f"interpreter-exec mi3 {textutil.gdb_command_arg_escape(command)}"
        )
    except comms.NotRunningError:
        return {}
    return parse_response(text)
