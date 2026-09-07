"""Route modules split out of ``clauster.app.create_app`` (#1156).

Each module added here defines one :class:`fastapi.APIRouter` for a single domain
and will be wired into the app by an ``app.include_router(...)`` call in
:func:`clauster.app.create_app`. Handlers read their collaborators through the
typed accessors in :mod:`clauster.dependencies` instead of closing over them.
This package holds only the marker until the first domain PR lands a router.
"""
