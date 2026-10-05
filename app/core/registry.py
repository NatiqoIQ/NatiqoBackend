"""ORM model registry.

SQLAlchemy only knows about a table once its module has been imported. A model that
is never imported is silently missing from ``Base.metadata``, which produces two
nasty failure modes:

- ``alembic revision --autogenerate`` writes a migration that **drops** the table;
- DDL generation and ``create_all`` fail with ``NoReferencedTableError`` the moment
  any registered table has a foreign key pointing at the unregistered one.

``configure_mappers`` does *not* catch this: it configures ORM relationships, not
raw foreign-key targets, so it succeeds happily against incomplete metadata.

So every model module is listed here explicitly, and both the application lifespan and
``alembic/env.py`` import this module. Adding a module means adding one line, which is
the point: the list is reviewable, not discovered.
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: Every module that defines a mapped class. Keep alphabetical within its group.
#:
#: Ordering matters: ``users`` is listed before ``ai`` because ``ai_usage_records``
#: carries a foreign key to ``users.id``, and metadata resolves foreign keys in
#: declaration order when generating DDL. ``content`` is self-contained (its foreign keys
#: point only at its own tables, in parent-to-child order) so it is listed last.
MODEL_MODULES: tuple[str, ...] = (
    "app.modules.users.infrastructure.models",
    "app.modules.ai.infrastructure.models",
    "app.modules.content.infrastructure.models",
)


def import_all_models() -> tuple[ModuleType, ...]:
    """Import every model module so the metadata is complete."""
    return tuple(importlib.import_module(path) for path in MODEL_MODULES)
