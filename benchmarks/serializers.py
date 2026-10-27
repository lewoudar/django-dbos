"""Compare the serializers django-dbos can store task data with.

Times a round trip (serialize, then deserialize) of a list of dicts, each
with six fields (ints, strings, a float, a boolean, a short list): the
payload behind the timings quoted in docs/configuration.md (Serialization).
Run it from the repository root::

    uv run benchmarks/serializers.py
    uv run benchmarks/serializers.py --rows 10000

Absolute times depend on the machine; compare the ratios.
"""

from __future__ import annotations

import argparse
import base64
import pickle
import timeit
from typing import Any

from dbos import DBOSPortableJSONSerializer, Serializer

from django_dbos.serializers import MsgspecJSONSerializer, MsgspecMsgpackSerializer


class PickleSerializer(Serializer):
    """DBOS' default serializer (pickle, base64-encoded), reproduced: DBOS only exposes it privately."""

    def serialize(self, data: Any) -> str:
        return base64.b64encode(pickle.dumps(data)).decode('utf-8')

    def deserialize(self, serialized_data: str) -> Any:
        return pickle.loads(base64.b64decode(serialized_data))  # noqa: S301 - our own payload

    def name(self) -> str:
        return 'py_pickle'


SERIALIZERS: dict[str, Serializer] = {
    'msgspec JSON': MsgspecJSONSerializer(),
    'msgspec msgpack': MsgspecMsgpackSerializer(),
    'pickle (DBOS default)': PickleSerializer(),
    'DBOS portable JSON': DBOSPortableJSONSerializer(),
}


def make_payload(rows: int) -> list[dict[str, Any]]:
    """A list of ``rows`` dicts with six fields of the common JSON types.

    Args:
        rows: Number of dicts in the list.

    Returns:
        The payload to serialize.
    """
    return [
        {
            'id': index,
            'name': f'customer {index}',
            'email': f'customer{index}@example.com',
            'score': index * 1.5,
            'active': index % 2 == 0,
            'tags': ['vip', 'newsletter'] if index % 3 == 0 else ['standard'],
        }
        for index in range(rows)
    ]


def measure(serializer: Serializer, payload: Any, repeat: int) -> tuple[float, int]:
    """The best round-trip time of a serializer, and the size of what it stores.

    Args:
        serializer: The serializer to time.
        payload: The value to serialize, then deserialize.
        repeat: Number of timing runs; the fastest is kept, the others being
            slowed down by the rest of the machine.

    Returns:
        The round-trip time in milliseconds, and the serialized size in bytes.

    Raises:
        ValueError: The value read back differs from the payload: its timing
            would not compare like with like.
    """
    serialized = serializer.serialize(payload)
    if serializer.deserialize(serialized) != payload:
        raise ValueError(f'{serializer.name()} does not round-trip the payload')

    def round_trip() -> None:
        serializer.deserialize(serializer.serialize(payload))

    timer = timeit.Timer(round_trip)
    number, _ = timer.autorange()  # enough calls per run to last at least 0.2 s
    best = min(timer.repeat(repeat=repeat, number=number)) / number
    return best * 1000, len(serialized.encode())


def main() -> None:
    """Time every serializer on the payload and print a comparison table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--rows', type=int, default=1000, help='dicts in the payload (default: 1000)')
    parser.add_argument('--repeat', type=int, default=5, help='timing runs per serializer (default: 5)')
    args = parser.parse_args()

    payload = make_payload(args.rows)
    results = {label: measure(serializer, payload, args.repeat) for label, serializer in SERIALIZERS.items()}
    fastest = min(milliseconds for milliseconds, _ in results.values())

    print(f'Round trip of {args.rows} dicts (best of {args.repeat} runs)\n')
    print(f'{"Serializer":<24}{"Round trip":>12}{"Ratio":>9}{"Stored size":>14}')
    for label, (milliseconds, size) in sorted(results.items(), key=lambda item: item[1][0]):
        print(f'{label:<24}{milliseconds:>9.3f} ms{milliseconds / fastest:>8.1f}x{size / 1024:>11.1f} KB')


if __name__ == '__main__':
    main()
