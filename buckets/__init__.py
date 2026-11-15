"""
Buckets of files: the bytes of the files a bucket holds, one file on disk per
row of the bucket, a /db table made with ``bucket`` (db/tables.py). files.py
keeps them and writes the rows; api/file_routes.py has the routes. A bucket
may name a remote (remotes.py: a place outside the service, through OpenDAL;
naming.py keeps which one, and which files are copied there), which copies.py
copies its files to and reads a missing one back from; api/remote_routes.py
edits the remotes.
"""

from .copies import Copies
from .files import BucketFiles, check_content_type, check_file_id, check_file_name
from .naming import Namings
from .remotes import RemoteSpec, Remotes

__all__ = ["BucketFiles", "Copies", "Namings", "RemoteSpec", "Remotes", "check_content_type", "check_file_id",
           "check_file_name"]
