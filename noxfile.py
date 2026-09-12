"""Nox sessions for django-dbos."""

from __future__ import annotations

import nox

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["lint", "typecheck", "tests"]

PYTHON_VERSIONS = ["3.12", "3.13", "3.14"]


@nox.session(python=PYTHON_VERSIONS)
def tests(session: nox.Session) -> None:
    session.run("uv", "run", "--active", "pytest", "-m", "not integration", external=True)


@nox.session(python=False)
def integration(session: nox.Session) -> None:
    """Integration tests, requiring Docker for testcontainers."""
    session.run("uv", "run", "--active", "pytest", "-m", "integration", external=True)


@nox.session
def lint(session: nox.Session) -> None:
    session.run("uv", "run", "--active", "ruff", "check", ".", external=True)
    session.run("uv", "run", "--active", "ruff", "format", "--check", ".", external=True)


@nox.session
def format(session: nox.Session) -> None:
    session.run("uv", "run", "--active", "ruff", "check", "--fix", ".", external=True)
    session.run("uv", "run", "--active", "ruff", "format", ".", external=True)


@nox.session
def typecheck(session: nox.Session) -> None:
    session.run("uv", "run", "--active", "pyrefly", "check", external=True)


@nox.session
def docs(session: nox.Session) -> None:
    session.run("uv", "run", "--group", "doc", "zensical", "build", external=True)
