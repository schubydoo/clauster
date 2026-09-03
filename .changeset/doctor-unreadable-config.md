---
default: patch
---

A config file that exists but cannot be read now fails the doctor config row plainly. It previously made `clauster doctor` and `/api/doctor` crash with a 500.
