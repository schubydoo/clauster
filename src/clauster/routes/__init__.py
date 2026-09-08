"""Route modules split out of ``clauster.app.create_app`` (#1156).

Each module added here defines one :class:`fastapi.APIRouter` for a single domain
and will be wired into the app by an ``app.include_router(...)`` call in
:func:`clauster.app.create_app`. Handlers read their collaborators through the
typed accessors in :mod:`clauster.dependencies` instead of closing over them.
Routers declare full paths and are included with no prefix (#1156), so each
route's path is the URL it serves.
"""
