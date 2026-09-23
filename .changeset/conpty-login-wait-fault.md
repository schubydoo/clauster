---
default: patch
---

On Windows, a dashboard login whose terminal handle fails while Clauster reads its exit status or stops it now ends as a named failure with its handle closed, instead of staying active or returning a server error.
