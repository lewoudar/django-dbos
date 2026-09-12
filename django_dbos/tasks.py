"""DBOSTask: a Django Task carrying DBOS execution options."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.tasks import Task

from .params import FUNC_PARAMS_ATTRIBUTE
from .params_dataclass import DbosTaskParams


@dataclass(frozen=True, kw_only=True)
class DBOSTask(Task):
    """A Django Task with DBOS execution options."""

    dbos_params: DbosTaskParams = field(default_factory=DbosTaskParams)

    def __post_init__(self) -> None:
        super().__post_init__()
        # Pick up params attached to the function by @dbos_params when it was
        # applied *under* @task (i.e. on the bare function).
        func_params: DbosTaskParams | None = getattr(self.func, FUNC_PARAMS_ATTRIBUTE, None)
        if func_params is not None and self.dbos_params == DbosTaskParams():
            object.__setattr__(self, "dbos_params", func_params)
