# Benchmarks

Scripts measuring django-dbos' performance-related choices. They are not
part of the test suite nor of the CI: timings taken on shared runners vary
too much to assert on. Run them from the repository root.

## Serializers

`serializers.py` times a round trip (serialization, then deserialization) of
a list of dicts through every serializer the `SERIALIZER` option accepts,
and prints each one's time, its ratio to the fastest and the size it stores.
It is where the timings in the documentation
([Configuration › Serialization](../docs/configuration.md#serialization))
come from.

```sh
uv run benchmarks/serializers.py              # 1000 dicts
uv run benchmarks/serializers.py --rows 10000 # a larger payload
```

```
Round trip of 1000 dicts (best of 5 runs)

Serializer                Round trip    Ratio   Stored size
msgspec JSON                0.571 ms     1.0x      115.3 KB
msgspec msgpack             0.965 ms     1.7x      122.9 KB
pickle (DBOS default)       1.067 ms     1.9x       98.4 KB
DBOS portable JSON          3.783 ms     6.6x      115.3 KB
```

Absolute times depend on the machine, and vary from one run to the next on
a laptop (power management): compare the ratios and the order, which hold.
Each serializer is checked to return the payload unchanged before it is
timed.
