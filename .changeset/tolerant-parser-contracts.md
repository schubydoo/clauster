---
default: patch
---

Seven untrusted-input parsers no longer raise on deeply-nested or non-object payloads their contracts promise to tolerate, so a hostile transcript line, settings file, frontmatter block, 404 body, or hosted-state record degrades instead of crashing the request or the stream pump.
