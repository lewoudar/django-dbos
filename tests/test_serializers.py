"""Tests for the msgspec serializers."""

import sys
from datetime import UTC, datetime
from decimal import Decimal

import msgspec
import pytest

from django_dbos.exceptions import ImproperlyConfiguredDbos, TaskFailure
from django_dbos.serializers import MsgspecJSONSerializer, MsgspecMsgpackSerializer

SERIALIZERS = [MsgspecJSONSerializer, MsgspecMsgpackSerializer]


@pytest.fixture(params=SERIALIZERS, ids=['json', 'msgpack'])
def serializer(request):
    return request.param()


def raised(exc: Exception) -> Exception:
    """``exc`` with a traceback, as DBOS gets it."""
    try:
        raise exc
    except type(exc) as caught:
        return caught


class TestRoundTrip:
    @pytest.mark.parametrize(
        'value',
        [None, True, 42, 1.5, 'héllo ✓', [1, 'a', None], {'nested': {'list': [1, {'deep': True}]}}],
    )
    def test_json_values_come_back_unchanged(self, serializer, value):
        assert serializer.deserialize(serializer.serialize(value)) == value

    def test_serialized_value_is_text(self, serializer):
        assert isinstance(serializer.serialize({'a': 1}), str)

    def test_workflow_inputs_come_back_with_lists(self, serializer):
        inputs = {'args': (1, 'a'), 'kwargs': {'items': (2, 3)}}
        assert serializer.deserialize(serializer.serialize(inputs)) == {'args': [1, 'a'], 'kwargs': {'items': [2, 3]}}

    def test_other_types_come_back_as_strings(self, serializer):
        assert serializer.deserialize(serializer.serialize(Decimal('1.50'))) == '1.50'

    def test_unsupported_type_names_it(self, serializer):
        with pytest.raises(TypeError, match='Objects of type object cannot be serialized'):
            serializer.serialize(object())

    def test_encoder_and_decoder_are_reused(self, serializer):
        encoder, decoder = serializer._encoder, serializer._decoder
        serializer.deserialize(serializer.serialize([1]))
        assert (serializer._encoder, serializer._decoder) == (encoder, decoder)


class TestExceptions:
    def test_exception_comes_back_as_a_task_failure(self, serializer):
        error = serializer.deserialize(serializer.serialize(raised(ValueError('boom'))))

        assert isinstance(error, TaskFailure)
        assert error.exception_class_path == 'builtins.ValueError'
        assert error.message == 'boom'
        assert str(error) == 'boom'
        assert 'Traceback (most recent call last)' in error.traceback
        assert 'ValueError: boom' in error.traceback

    def test_exception_without_traceback(self, serializer):
        error = serializer.deserialize(serializer.serialize(KeyError('missing')))

        assert error.exception_class_path == 'builtins.KeyError'
        assert error.traceback == "KeyError: 'missing'\n"

    def test_a_dict_that_merely_contains_the_marker_is_left_alone(self):
        serializer = MsgspecJSONSerializer()
        value = {'__django_dbos_error__': {'type': 'x', 'message': 'y'}, 'other': 1}
        assert serializer.deserialize(serializer.serialize(value)) == value

    def test_msgpack_finds_nested_exceptions(self):
        serializer = MsgspecMsgpackSerializer()
        [error] = serializer.deserialize(serializer.serialize([ValueError('boom')]))
        assert isinstance(error, TaskFailure)


class TestMsgpackTypes:
    def test_aware_datetimes_and_bytes_are_kept(self):
        serializer = MsgspecMsgpackSerializer()
        value = {'at': datetime(2026, 9, 30, 12, tzinfo=UTC), 'raw': b'\x00\xff'}
        assert serializer.deserialize(serializer.serialize(value)) == value

    def test_unknown_extension_types_are_returned_as_is(self):
        serializer = MsgspecMsgpackSerializer()
        text = serializer.serialize(msgspec.msgpack.Ext(5, b'data'))
        assert serializer.deserialize(text) == msgspec.msgpack.Ext(5, b'data')


@pytest.mark.parametrize(
    ('serializer_class', 'name'),
    [(MsgspecJSONSerializer, 'django_dbos_msgspec_json'), (MsgspecMsgpackSerializer, 'django_dbos_msgspec_msgpack')],
)
def test_names(serializer_class, name):
    assert serializer_class().name() == name


@pytest.mark.parametrize('serializer_class', SERIALIZERS)
def test_missing_msgspec_tells_how_to_install_it(monkeypatch, serializer_class):
    monkeypatch.setitem(sys.modules, 'msgspec', None)
    with pytest.raises(ImproperlyConfiguredDbos, match=r'django-dbos\[msgspec\]'):
        serializer_class()
