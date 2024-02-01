import pydantic
from src.udbpy.gdb_extensions import udb_base  # type: ignore[import]


class AltuiTelemetry(pydantic.BaseModel):
    sourced: bool = False
    enabled: bool = False
    disabled: bool = False


def get(udb: udb_base.Udb) -> AltuiTelemetry:
    return udb.telemetry_session.get_extra("altui", AltuiTelemetry)
