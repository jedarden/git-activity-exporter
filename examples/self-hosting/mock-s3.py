"""Minimal path-style S3 fixture for the self-hosting smoke profile."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape


OBJECTS = {}


def _parts(path):
    pieces = [unquote(piece) for piece in urlsplit(path).path.split("/") if piece]
    if not pieces:
        return "", ""
    return pieces[0], "/".join(pieces[1:])


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
    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
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
        item = OBJECTS.get((bucket, key))
        if item is None:
            self.send_error(404, "NoSuchKey")
            return
        body, content_type = item
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        bucket, key = _parts(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        OBJECTS[(bucket, key)] = (
            self.rfile.read(length),
            self.headers.get("Content-Type", "application/octet-stream"),
        )
        self.send_response(200)
        self.end_headers()

    def do_DELETE(self):
        bucket, key = _parts(self.path)
        OBJECTS.pop((bucket, key), None)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        pass


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
