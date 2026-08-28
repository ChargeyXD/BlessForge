# retired/ — harnesses for the front end replaced on 2026-08-29

These four drove the previous interface and every one of them passed against
it (155 checks). They are kept, not deleted, because the screens they cover
have not all been rebuilt yet and the *assertions* are still the specification
even though the selectors are gone:

| file | what it still specifies |
|---|---|
| `01-shell-and-views.mjs` | the config editor's gutter and dirty guard, the import dropzone's keyboard and drag states, and that a Crafty 500 on one instance must not break the page |
| `02-mod-list.mjs` | the windowed mod list: spacers, repaint on scroll, selection surviving unmount, in-place row patching |
| `03-jobs.mjs` | the job registry: log dedupe with counts, the stream pane, background → re-attach with replay, the completion summary |
| `04-terminal-and-review.mjs` | the console stream, its filter and command echo, client-side tagging, the dependency view, and that the install review sends `disable_files` rather than `exclude_files` |

Port each one as its screen is rebuilt, then delete it from here. `smoke.mjs`
and `05-roulette.mjs` in the parent directory are the live harnesses.
