"""Handing a stored upload back to the person allowed to see it.

Uploads are never served from a public /media/ URL: that route can't tell
who is asking, so it either shows every company's files to anyone holding
the link or (as the role table did) refuses everybody who isn't an admin --
and in production it isn't there at all. Each kind of file has its own view
that checks login, company and the record's own visibility, then calls
`attachment_response` to stream it.
"""
import mimetypes
import os

from django.http import FileResponse, Http404


def attachment_response(fieldfile, filename=None):
    """Stream `fieldfile` as a download.

    * `as_attachment=True`: the browser saves the file instead of rendering
      it, so an HTML or SVG file that got past the upload checks still never
      runs as this site;
    * the content type is worked out from the file name on the server, not
      taken from what the uploader's browser claimed at upload time;
    * `X-Content-Type-Options: nosniff` stops the browser second-guessing
      that type, and a `sandbox` CSP neuters the response if it is ever
      opened inline anyway.
    """
    if not fieldfile:
        raise Http404("No file is stored for this record.")
    try:
        handle = fieldfile.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("The stored file is missing. Ask an admin to upload it again.")
    stored_name = os.path.basename(fieldfile.name or "")
    ext = os.path.splitext(stored_name)[1]
    download_name = filename or stored_name
    if ext and not download_name.lower().endswith(ext.lower()):
        download_name = f"{download_name}{ext}"
    content_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
    response = FileResponse(handle, as_attachment=True, filename=download_name,
                            content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = "private, no-store"
    return response
