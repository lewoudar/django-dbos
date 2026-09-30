"""msgspec serializers for task arguments, results and errors.

Safe alternatives to DBOS' default serializer (pickle), and much faster than
``dbos.DBOSPortableJSONSerializer``. Install the extra, then select one::

    uv add "django-dbos[msgspec]"

    DJANGO_DBOS = {'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer'}

- :class:`MsgspecJSONSerializer` stores JSON: the fastest and most compact,
  readable in the system database and the admin.
- :class:`MsgspecMsgpackSerializer` stores base64-encoded msgpack (DBOS
  stores text): slower and bigger than JSON, but aware ``datetime`` and
  ``bytes`` values come back as such instead of strings.

Both keep JSON-like data: tuples and sets come back as lists; ``Decimal``,
``UUID``, dates, times, enums (and naive datetimes) as strings; dataclasses as
dicts. A task's exception comes back as :class:`~django_dbos.exceptions.TaskFailure`,
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

#: Marks an exception in JSON: ``{"__django_dbos_error__": {...}}``.
_ERROR_KEY = '__django_dbos_error__'
#: msgpack extension type code of an exception.
_ERROR_EXT_CODE = 1


def _import_msgspec() -> Any:
    try:
        import msgspec
    except ImportError as exc:
        raise ImproperlyConfiguredDbos(
            'The msgspec serializers need msgspec: install it with the extra, django-dbos[msgspec].'
        ) from exc
    return msgspec


def _error_payload(exc: BaseException) -> dict[str, str]:
    error_class = type(exc)
    return {
        'type': f'{error_class.__module__}.{error_class.__qualname__}',
        'message': str(exc),
        'traceback': ''.join(traceback.format_exception(exc)),
    }


def _unsupported(value: Any) -> TypeError:
    return TypeError(f'Objects of type {type(value).__qualname__} cannot be serialized by the msgspec serializers.')


def _error_from_payload(payload: dict[str, Any]) -> TaskFailure:
    return TaskFailure(str(payload['type']), str(payload['message']), str(payload.get('traceback') or ''))


class MsgspecJSONSerializer(Serializer):
    """Serialize to JSON with msgspec."""

    def __init__(self) -> None:
        msgspec = _import_msgspec()
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
            return _error_from_payload(value[_ERROR_KEY])
        return value

    def name(self) -> str:
        return 'django_dbos_msgspec_json'


class MsgspecMsgpackSerializer(Serializer):
    """Serialize to msgpack with msgspec, base64-encoded for DBOS' text columns."""

    def __init__(self) -> None:
        msgspec = _import_msgspec()
        self._ext = msgspec.msgpack.Ext
        self._encoder = msgspec.msgpack.Encoder(enc_hook=self._enc_hook)
        self._decoder = msgspec.msgpack.Decoder(ext_hook=self._ext_hook)
        # Encode and decode the payload carried by an exception's extension type.
        self._payload_encoder = msgspec.msgpack.Encoder()
        self._payload_decoder = msgspec.msgpack.Decoder(dict[str, str])

    def _enc_hook(self, value: Any) -> Any:
        if isinstance(value, BaseException):
            return self._ext(_ERROR_EXT_CODE, self._payload_encoder.encode(_error_payload(value)))
        raise _unsupported(value)

    def _ext_hook(self, code: int, data: memoryview) -> Any:
        if code == _ERROR_EXT_CODE:
            return _error_from_payload(self._payload_decoder.decode(data))
        return self._ext(code, bytes(data))

    def serialize(self, data: Any) -> str:
        return base64.b64encode(self._encoder.encode(data)).decode('ascii')

    def deserialize(self, serialized_data: str) -> Any:
        return self._decoder.decode(base64.b64decode(serialized_data))

    def name(self) -> str:
        return 'django_dbos_msgspec_msgpack'
