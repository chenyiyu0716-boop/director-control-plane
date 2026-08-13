# Julius Task Mapping

| Ledger field | Control Plane field | Rule |
|---|---|---|
| episode number | candidate ID | `001` becomes `JUL-EP-001` |
| source + question | title/objective candidate | Text is preserved, not executed |
| Ledger status | candidate metadata | Never directly becomes READY |
| Owner-approved scope | `scope` | Required before registration |
| deterministic checks | `acceptance` | Required before decision |

`JUL-*` is the only prefix for new Control Plane tasks. Existing `JEP-*` or older `JUL-*` records are retained as
history and are not overwritten. The first shadow task is `JUL-SHADOW-001`.
