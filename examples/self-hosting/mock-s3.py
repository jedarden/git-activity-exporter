"""Minimal path-style S3 fixture for the self-hosting smoke profile."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape


OBJECTS = {}
OBJECT_CACHE_CONTROL = {}

WEBSITE_HOST = os.environ.get("WEBSITE_HOST", "")
WEBSITE_BUCKET = os.environ.get("WEBSITE_BUCKET", "")
WEBSITE_SOURCE_PREFIX = os.environ.get("WEBSITE_SOURCE_PREFIX", "").strip("/")
WEBSITE_PATH_PREFIX = "/" + os.environ.get("WEBSITE_PATH_PREFIX", "").strip("/")


def _parts(path):
    pieces = [unquote(piece) for piece in urlsplit(path).path.split("/") if piece]
    if not pieces:
        return "", ""
    return pieces[0], "/".join(pieces[1:])


def _website_key(path):
    """Map a website request to the same bucket objects as the S3 API."""
    request_path = urlsplit(path).path
    if not WEBSITE_HOST or not WEBSITE_BUCKET or request_path == "/":
        return None
    if not request_path.startswith(WEBSITE_PATH_PREFIX + "/"):
        return None
    relative = request_path[len(WEBSITE_PATH_PREFIX) + 1:].strip("/")
    if not relative:
        return None
    return WEBSITE_BUCKET, "/".join(
        part for part in (WEBSITE_SOURCE_PREFIX, relative) if part
    )


def _list_response(bucket, query):
    prefix = query.get("prefix", [""])[0]
    delimiter = query.get("delimiter", [""])[0]
    keys = sorted(key for (item_bucket, key) in OBJECTS if item_bucket == bucket)
    keys = [key for key in keys if key.startswith(prefix)]
    if delimiter:
        children = sorted({key[:key.index(delimiter) + 1]
                           for key in (key[len(prefix):] for key in keys)
                           if delimiter in key})
        contents = []
    else:
        children = []
        contents = keys
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
        f"<Name>{escape(bucket)}</Name>",
        f"<Prefix>{escape(prefix)}</Prefix>",
        "<IsTruncated>false</IsTruncated>",
    ]
    for child in children:
        body.append(f"<CommonPrefixes><Prefix>{escape(prefix + child)}</Prefix></CommonPrefixes>")
    for key in contents:
        body.append(f"<Contents><Key>{escape(key)}</Key><Size>{len(OBJECTS[(bucket, key)][0])}</Size></Contents>")
    body.append("</ListBucketResult>")
    return "".join(body).encode()


class Handler(BaseHTTPRequestHandler):
    def _send_object(self, include_body):
        website = self.headers.get("Host", "").split(":", 1)[0] == WEBSITE_HOST
        if website:
            target = _website_key(self.path)
            if target is None:
                self.send_error(404, "NoSuchKey")
                return
            bucket, key = target
        else:
            bucket, key = _parts(self.path)
        item = OBJECTS.get((bucket, key))
        if item is None:
            self.send_error(404, "NoSuchKey")
            return
        body, content_type = item
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        cache_control = OBJECT_CACHE_CONTROL.get((bucket, key))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if self.headers.get("Host", "").split(":", 1)[0] == WEBSITE_HOST:
            self._send_object(include_body=True)
            return
        bucket, key = _parts(self.path)
        query = parse_qs(urlsplit(self.path).query)
        if query.get("list-type") == ["2"]:
            body = _list_response(bucket, query)
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_object(include_body=True)

    def do_HEAD(self):
        # boto3's HeadObject call is the metadata permission check. The
        # fixture returns the same object headers as GET without a body.
        self._send_object(include_body=False)

    def do_PUT(self):
        bucket, key = _parts(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        OBJECTS[(bucket, key)] = (
            self.rfile.read(length),
            self.headers.get("Content-Type", "application/octet-stream"),
        )
        OBJECT_CACHE_CONTROL[(bucket, key)] = self.headers.get("Cache-Control")
        self.send_response(200)
        self.end_headers()

    def do_DELETE(self):
        bucket, key = _parts(self.path)
        OBJECTS.pop((bucket, key), None)
        OBJECT_CACHE_CONTROL.pop((bucket, key), None)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        pass


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
