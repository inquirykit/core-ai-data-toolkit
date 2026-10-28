"""
The browser pages (/admin, /admin/setup, /playground).

They are a React app in frontend/, built into api/static (``npm run build``;
the container images build it themselves). The pages hold no data and need no
key; they ask for one and call the API like any client.
"""

from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, Response

STATIC_DIR = Path(__file__).resolve().parent / "static"

NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AI &amp; Data Toolkit: pages not built</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:10vh auto;padding:0 16px;line-height:1.5">
<h1 style="font-size:18px">AI &amp; Data Toolkit: the {name} page is not built</h1>
<p>The browser pages are a React app in <code>frontend/</code>. Build them once, then reload:</p>
<pre style="background:#f3f3f1;padding:12px;border-radius:8px">cd frontend
npm install
npm run build</pre>
<p>The API itself works without them.</p>
</body></html>
"""


def page(name: str) -> Response:
    """The built page ``name`` (e.g. "admin.html"), or instructions to build it."""
    path = STATIC_DIR / name
    if path.is_file():
        # Always revalidated, so a rebuilt page (with new asset names) shows at once.
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    return HTMLResponse(NOT_BUILT.format(name=name.removesuffix(".html")), status_code=503)
