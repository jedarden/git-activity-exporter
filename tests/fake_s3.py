"""A bare in-memory S3 shared by the publication tests.

It models put/get/delete/list exactly as boto3 shapes them -- including
NoSuchKey on a missing get and continuation-token pagination -- so injected
faults land on the same calls the real client makes.
"""
import io

from botocore.exceptions import ClientError


def _nosuch_key():
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject")


class FakeS3:
    """In-memory S3 with injectable per-operation faults.

    ``fault(op, key)`` returning an exception makes that call raise; return
    None to let the call through. ``puts`` records fixed-key put order.
    """

    def __init__(self):
        self.objects = {}
        self.puts = []
        self._fault = None

    def fail_when(self, fault):
        self._fault = fault

    def _gate(self, op, key):
        if self._fault:
            err = self._fault(op, key)
            if err is not None:
                raise err

    def put_object(self, Bucket, Key, Body, ContentType):
        self._gate("put", Key)
        self.objects[Key] = (bytes(Body), ContentType)
        self.puts.append(Key)

    def get_object(self, Bucket, Key):
        self._gate("get", Key)
        if Key not in self.objects:
            raise _nosuch_key()
        data, ct = self.objects[Key]
        return {"Body": io.BytesIO(data), "ContentType": ct}

    def delete_object(self, Bucket, Key):
        self._gate("delete", Key)
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None,
                        MaxKeys=1000):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if ContinuationToken:
            keys = [k for k in keys if k > ContinuationToken]
        page = keys[:MaxKeys]
        resp = {}
        if Delimiter:
            prefixes = sorted({
                Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in page
            } - {Prefix})
            if prefixes:
                resp["CommonPrefixes"] = [{"Prefix": p} for p in prefixes]
        else:
            resp["Contents"] = [{"Key": k} for k in page]
        if len(keys) > len(page):
            resp["NextContinuationToken"] = page[-1]
        return resp
