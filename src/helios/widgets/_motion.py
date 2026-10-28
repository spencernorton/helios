"""Single source of truth for Helios transition durations.

`helios.css` cannot reference these. Ubuntu 24.04 ships GTK 4.14; CSS custom
properties / `var()` landed in 4.16, and `@define-color` is colors only. So
"declare the duration once" is achievable only as a Python constant for the
Python side plus normalised literals in the stylesheet.

**The stylesheet mirrors these values.** Every `transition` duration and
one-shot `animation` duration in `helios.css` is one of `DURATIONS`, and
`tests/test_style_tokens.py` guards both sides. Keyframe loop periods
(`infinite` animations) are exempt — a 1.5–2.8s pulse period is not a
transition.

Pure ints: no `import gi`, so this is importable from the `python:3.13-slim`
`tests` lane, and `test_style_tokens.py` imports it to prove exactly that.
"""

from __future__ import annotations

FAST_MS = 140  # micro-feedback: the context-pane page switch
BASE_MS = 220  # everything else: revealers, hovers, stack and label crossfades
SLOW_MS = 320  # deliberate: the context arc sweep (arrives with M-3b's 420/440)

DURATIONS = (FAST_MS, BASE_MS, SLOW_MS)
