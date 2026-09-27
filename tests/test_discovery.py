"""Tests for the autodiscovery of task modules."""

import importlib
import sys
from types import SimpleNamespace

import pytest

from django_dbos import discovery


@pytest.fixture
def make_app(tmp_path, monkeypatch, mocker):
    """Build throwaway app packages on disk and expose them as installed apps."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []
    app_configs: list[SimpleNamespace] = []
    mocker.patch.object(discovery.apps, 'get_app_configs', return_value=app_configs)

    def factory(name: str, modules: dict[str, str]) -> None:
        package = tmp_path / name
        package.mkdir()
        (package / '__init__.py').write_text('')
        for module_name, source in modules.items():
            (package / f'{module_name}.py').write_text(source)
        importlib.invalidate_caches()
        app_configs.append(SimpleNamespace(name=name, module=importlib.import_module(name)))
        created.append(name)

    yield factory
    for name in list(sys.modules):
        if name.split('.')[0] in created:
            del sys.modules[name]


def test_imports_tasks_and_dbos_tasks_modules(make_app):
    make_app('discovery_both', {'tasks': 'LOADED = True', 'dbos_tasks': 'LOADED = True'})

    discovery.autodiscover_tasks()

    assert 'discovery_both.tasks' in sys.modules
    assert 'discovery_both.dbos_tasks' in sys.modules


def test_apps_without_task_modules_are_skipped(make_app):
    make_app('discovery_empty', {})
    make_app('discovery_only_dbos', {'dbos_tasks': 'LOADED = True'})

    discovery.autodiscover_tasks()

    assert 'discovery_empty.tasks' not in sys.modules
    assert 'discovery_only_dbos.dbos_tasks' in sys.modules


def test_broken_task_module_import_error_propagates(make_app):
    # The module exists but one of its own imports fails: that is a bug in
    # the app, not a missing module, and must not be silently swallowed.
    make_app('discovery_broken', {'tasks': 'import discovery_missing_dependency'})

    with pytest.raises(ModuleNotFoundError, match='discovery_missing_dependency'):
        discovery.autodiscover_tasks()
