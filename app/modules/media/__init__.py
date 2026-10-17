"""The media module: direct-to-storage uploads and their metadata.

Owns the presign → upload → confirm → read flow described in the roadmap. The binary
payload never touches this process: a client asks for a grant, PUTs straight to the
object store, and tells the server it is done. The server's job is the part a client
cannot be trusted to do — decide what the file *really* is, how big it is, and whether
its kind may be stored at all.

Only metadata lives here (the ``media_assets`` table). The bytes are addressed by
``storage_key`` and delivered through short-lived signed URLs, exactly as the storage
port's docstring promises.
"""
