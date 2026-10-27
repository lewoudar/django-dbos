"""Regenerate the admin screenshots of the documentation (docs/assets/admin/).

Builds a throwaway Django project in a temporary directory (SQLite databases,
a demo app with a task that succeeds, one that fails and a ``@cron`` task),
starts its DBOS worker and web server, enqueues a few tasks and captures the
workflow list, a failed workflow and the schedules, in light and dark themes.

Run it from the repository root after any change to the admin pages::

    uv run --with playwright playwright install chromium   # once
    uv run --with playwright python scripts/admin_screenshots.py

Playwright is not a dependency of the project: ``--with`` brings it for
this run only.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path(__file__).resolve().parent.parent / 'docs' / 'assets' / 'admin'
VIEWPORT = {'width': 1100, 'height': 760}
USERNAME = 'admin'
PASSWORD = 'screenshots-only'  # noqa: S105 - a throwaway superuser of a throwaway database

SETTINGS = """
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = 'screenshots-only'
DEBUG = True
ALLOWED_HOSTS = ['127.0.0.1']
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_dbos',
    'shop',
]
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]
ROOT_URLCONF = 'screenshot_urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3'}}
USE_TZ = True
TIME_ZONE = 'UTC'
STATIC_URL = 'static/'
TASKS = {'default': {'BACKEND': 'django_dbos.backends.DBOSBackend', 'QUEUES': ['default', 'emails']}}
"""

URLS = """
from django.contrib import admin
from django.urls import path

urlpatterns = [path('admin/', admin.site.urls)]
"""

TASKS = """
import time

from django.tasks import task

from django_dbos import cron


@task(queue_name='emails')
def send_welcome_email(user_id: int) -> dict:
    time.sleep(1)
    return {'user_id': user_id, 'template': 'welcome', 'sent': True}


@task
def charge_customer(customer_id: int, amount_cents: int) -> dict:
    raise ValueError(f'card declined for customer {customer_id}')


@cron('*/10 * * * * *')  # every 10 seconds, so it fires while the screenshots are taken
@task
def sales_report() -> dict:
    return {'orders': 1284, 'revenue_cents': 2_391_500}
"""

ENQUEUE = """
from shop.tasks import charge_customer, send_welcome_email

for user_id in (17, 42, 108):
    send_welcome_email.enqueue(user_id)
charge_customer.enqueue(42, 1999)
"""


def _write_project(directory: Path) -> None:
    (directory / 'shop').mkdir()
    (directory / 'shop' / '__init__.py').write_text('')
    (directory / 'shop' / 'tasks.py').write_text(TASKS)
    (directory / 'screenshot_settings.py').write_text(SETTINGS)
    (directory / 'screenshot_urls.py').write_text(URLS)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _django(*args: str) -> list[str]:
    return [sys.executable, '-m', 'django', *args]


def _environment(directory: Path) -> dict[str, str]:
    return {
        **os.environ,
        'PYTHONPATH': str(directory),
        'DJANGO_SETTINGS_MODULE': 'screenshot_settings',
        'DJANGO_SUPERUSER_PASSWORD': PASSWORD,
    }


@contextmanager
def _running(command: list[str], env: dict[str, str], log: Path) -> Iterator[subprocess.Popen[bytes]]:
    with log.open('wb') as output:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Ignored SIGTERM: SIGKILL cannot be. Wait for it to die before the
                # temporary directory (its databases and log) is removed.
                process.kill()
                process.wait()


def _wait_for_server(port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f'the web server did not listen on port {port} within {timeout:.0f}s')


def _screenshot(page: Any, path: Path) -> None:
    """A full-page screenshot that ends with the content, not with the window."""
    page.set_viewport_size({'width': VIEWPORT['width'], 'height': 100})
    page.screenshot(path=path, full_page=True)
    page.set_viewport_size(VIEWPORT)


def _capture(base_url: str) -> None:
    from playwright.sync_api import sync_playwright  # pyrefly: ignore[missing-import]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scheme in ('light', 'dark'):
            page = browser.new_page(viewport=VIEWPORT, color_scheme=scheme)
            # Keep the admin's navigation sidebar closed: the screenshots show the page itself.
            page.add_init_script("localStorage.setItem('django.admin.navSidebarIsOpen', 'false')")
            page.goto(f'{base_url}/admin/login/')
            page.fill('#id_username', USERNAME)
            page.fill('#id_password', PASSWORD)
            page.click('input[type=submit]')

            page.goto(f'{base_url}/admin/dbos/workflows/')
            _screenshot(page, OUTPUT_DIR / f'workflows-{scheme}.png')

            page.goto(f'{base_url}/admin/dbos/workflows/?name=shop.tasks.charge_customer')
            page.locator('#result_list tbody a').first.click()
            _screenshot(page, OUTPUT_DIR / f'workflow-detail-{scheme}.png')

            page.goto(f'{base_url}/admin/dbos/schedules/')
            _screenshot(page, OUTPUT_DIR / f'schedules-{scheme}.png')
            page.close()
        browser.close()


def main() -> None:
    """Build the demo project, run it and capture the admin pages."""
    with tempfile.TemporaryDirectory(prefix='django-dbos-screenshots-', ignore_cleanup_errors=True) as tmp:
        directory = Path(tmp)
        _write_project(directory)
        env = _environment(directory)
        subprocess.run(_django('migrate', '-v0'), env=env, check=True)
        subprocess.run(
            _django('createsuperuser', '--noinput', '--username', USERNAME, '--email', 'admin@example.com'),
            env=env,
            check=True,
        )
        port = _free_port()
        with (
            _running(_django('dbos', 'worker'), env, directory / 'worker.log'),
            _running(_django('runserver', f'127.0.0.1:{port}', '--noreload'), env, directory / 'web.log'),
        ):
            _wait_for_server(port)
            subprocess.run(_django('shell', '-c', ENQUEUE), env=env, check=True)
            # Let the tasks finish and the cron fire a few times.
            time.sleep(25)
            _capture(f'http://127.0.0.1:{port}')
    print(f'screenshots written to {OUTPUT_DIR}')


if __name__ == '__main__':
    main()
