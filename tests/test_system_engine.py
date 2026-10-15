"""Tests for the SQLAlchemy engine handed to DBOS for a CockroachDB system database."""

import sys

import pytest

from django_dbos import system_engine
from django_dbos.exceptions import ImproperlyConfiguredDbos

COCKROACHDB_URL = 'cockroachdb+psycopg2://root@crdb:26257/dbos'


def test_other_databases_need_nothing():
    assert system_engine.system_database_options('postgresql+psycopg://db/dbos') == {}
    assert system_engine.system_database_options('sqlite:///dbos.sqlite') == {}


def test_cockroachdb_gets_an_engine_without_listen_notify():
    options = system_engine.system_database_options(COCKROACHDB_URL)
    try:
        engine = options['system_database_engine']
        assert engine.dialect.name == 'cockroachdb'
        assert engine.url.drivername == 'cockroachdb+psycopg2'
        assert options['use_listen_notify'] is False
    finally:
        system_engine.dispose(options)


@pytest.mark.parametrize('module', ['psycopg2', 'sqlalchemy_cockroachdb'])
def test_a_missing_extra_names_it(monkeypatch, module):
    monkeypatch.setitem(sys.modules, module, None)  # makes the import fail
    with pytest.raises(ImproperlyConfiguredDbos, match=r'needs the django-dbos\[cockroachdb\] extra'):
        system_engine.create_engine(COCKROACHDB_URL)


def test_dispose(mocker):
    engine = mocker.Mock()
    system_engine.dispose({'system_database_engine': engine, 'use_listen_notify': False})
    system_engine.dispose({})  # nothing to dispose of
    engine.dispose.assert_called_once_with()
