# arborist

Binary Ninja plugin that rebuilds a plausible source tree from the `__FILE__`
path strings passed to a logging or assert function. Spiritual sibling of
the [logrn](https://github.com/catnip/logrn) plugin: logrn gives functions their names,
arborist gives them their place.

## Usage

1. Find a logging/assert function that takes a source path (`__FILE__`) as an
   argument, e.g. `log(const char *file, int line, ...)`.
2. Right-click it and run **`export caller tree`**.
3. Pick which argument holds the file path.
4. Choose an export mode:
   - **tree only** recreates the directory structure with empty files.
   - **tree + Pseudo C** also fills each file with the decompiled Pseudo C of
     the callers attributed to it (falls back to HLIL if Pseudo C is
     unavailable).
5. Pick an output directory (prefilled with the last one used this session).
   Boom, a browsable tree.

All callers of the function are walked, each one placed by the path it passes
in the chosen argument. Runs as a background task so it won't freeze binja.

### Re-running into the same directory

Runs are **additive**: existing files are never truncated, and a function is
never written twice for the same format (tracked in `.arborist.json`). So you
can point several log functions at one directory and everything merges. The
final log line reports how many *new* files and functions were written. Delete
`.arborist.json` (and the files) to start fresh.

## Caveats

- **Attribution is first-hit per caller**, like logrn. A function inlined from
  another translation unit can carry a foreign `__FILE__`; a few misplaced
  functions are the price of coverage.
- **Partial by design.** Functions that never pass a usable path argument are
  skipped, the tree reflects what the binary leaks, not the whole project.
- **Reconstruction, not source.** The written Pseudo C is decompiler output,
  not the original source, and is not compilable.
- Only literal "pointer to string" arguments are resolved. Computed or
  obfuscated paths are ignored.
- Pseudo C needs a Binary Ninja recent enough to expose the linear "Pseudo C"
  representation, otherwise it silently falls back to HLIL.

## Roadmap

- Export format choice: HLIL (`.hlil`) and disassembly (`.asm`).
- Order functions within a file using `__LINE__` when available.
- Majority-vote attribution instead of first-hit.
- Coverage report + "unknown/" bucket for unattributed functions.