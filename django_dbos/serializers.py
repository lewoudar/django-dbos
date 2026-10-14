"""msgspec serializers for task arguments, results and errors.

Safe alternatives to DBOS' default serializer (pickle), and much faster than
``dbos.DBOSPortableJSONSerializer``. Install the extra, then select one:

    uv add "django-dbos[msgspec]"

    TASKS = {'default': {..., 'OPTIONS': {'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer'}}}

- ``MsgspecJSONSerializer`` stores JSON: the fastest and most compact,
  readable in the system database and the admin.
- ``MsgspecMsgpackSerializer`` stores base64-encoded msgpack (DBOS
  stores text): slower and bigger than JSON, but aware ``datetime`` and
  ``bytes`` values come back as such instead of strings.

Both keep JSON-like data: tuples and sets come back as lists; ``Decimal``,
``UUID``, dates, times, enums (and naive datetimes) as strings; dataclasses as
dicts. A task's exception comes back as ``TaskFailure``,
carrying the original class path, message and traceback: the original class is
never imported.

Each serializer builds its msgspec encoder and decoder once and reuses them
(they are thread-safe). Workers and web processes must use the same
serializer: its name is stored with every workflow.
"""

from __future__ import annotations

import base64
import traceback
from typing import Any

from dbos import Serializer

from .exceptions import ImproperlyConfiguredDbos, TaskFailure

try:
    import msgspec
except ImportError as exc:  # resolve_serializer() lets it through: manage.py check reports it (E001)
    raise ImproperlyConfiguredDbos(
        'The msgspec serializers need msgspec: install it with the extra, django-dbos[msgspec].'
    ) from exc

# Marks an exception in JSON: ``{"__django_dbos_error__": {...}}``.
_ERROR_KEY = '__django_dbos_error__'
# msgpack extension type code of an exception.
_ERROR_EXT_CODE = 1


class ErrorPayload(msgspec.Struct, frozen=True):
    """How a task's exception is stored: never the exception object itself."""

    # Dotted path of the exception class, never imported back.
    type: str
    message: str
    traceback: str = ''


def _error_payload(exc: BaseException) -> ErrorPayload:
    error_class = type(exc)
    return ErrorPayload(
        type=f'{error_class.__module__}.{error_class.__qualname__}',
        message=str(exc),
        traceback=''.join(traceback.format_exception(exc)),
    )


def _error_from_payload(payload: ErrorPayload) -> TaskFailure:
    return TaskFailure(payload.type, payload.message, payload.traceback)


def _unsupported(value: Any) -> TypeError:
    return TypeError(f'Objects of type {type(value).__qualname__} cannot be serialized by the msgspec serializers.')


class MsgspecJSONSerializer(Serializer):
    """Serialize to JSON with msgspec."""

    def __init__(self) -> None:
        self._encoder = msgspec.json.Encoder(enc_hook=self._enc_hook)
        self._decoder = msgspec.json.Decoder()

    @staticmethod
    def _enc_hook(value: Any) -> Any:
        if isinstance(value, BaseException):
            return {_ERROR_KEY: _error_payload(value)}
        raise _unsupported(value)

    def serialize(self, data: Any) -> str:
        return self._encoder.encode(data).decode()

    def deserialize(self, serialized_data: str) -> Any:
        value = self._decoder.decode(serialized_data)
        # DBOS stores an exception on its own: the marker is only looked for at the top level.
        if isinstance(value, dict) and len(value) == 1 and _ERROR_KEY in value:
            try:
                return _error_from_payload(msgspec.convert(value[_ERROR_KEY], ErrorPayload))
            except msgspec.ValidationError:
                pass  # data that merely looks like an error: returned as is
        return value

    def name(self) -> str:
        return 'django_dbos_msgspec_json'


class MsgspecMsgpackSerializer(Serializer):
    """Serialize to msgpack with msgspec, base64-encoded for DBOS' text columns."""

    def __init__(self) -> None:
        self._encoder = msgspec.msgpack.Encoder(enc_hook=self._enc_hook)
        self._decoder = msgspec.msgpack.Decoder(ext_hook=self._ext_hook)
        # Encode and decode the payload carried by an exception's extension type.
        self._error_payload_encoder = msgspec.msgpack.Encoder()
        self._error_payload_decoder = msgspec.msgpack.Decoder(ErrorPayload)

    def _enc_hook(self, value: Any) -> Any:
        if isinstance(value, BaseException):
            return msgspec.msgpack.Ext(_ERROR_EXT_CODE, self._error_payload_encoder.encode(_error_payload(value)))
        raise _unsupported(value)

    def _ext_hook(self, code: int, data: memoryview) -> Any:
        if code == _ERROR_EXT_CODE:
            return _error_from_payload(self._error_payload_decoder.decode(data))
        return msgspec.msgpack.Ext(code, bytes(data))

    def serialize(self, data: Any) -> str:
        return base64.b64encode(self._encoder.encode(data)).decode('ascii')

    def deserialize(self, serialized_data: str) -> Any:
        return self._decoder.decode(base64.b64decode(serialized_data))

    def name(self) -> str:
        return 'django_dbos_msgspec_msgpack'
