import json
import os
import re

from binaryninja import (BackgroundTaskThread, BinaryView, Function,
                         PluginCommand, log_info, log_warn)
from binaryninja.enums import MediumLevelILOperation as mlilop
from binaryninja.interaction import (get_choice_input,
                                     get_directory_name_input)
from binaryninja.mediumlevelil import MediumLevelILInstruction

# Pseudo C rendering goes through a linear view; it is version dependent, so
# we import it defensively and fall back to HLIL when unavailable.
try:
    from binaryninja import (DisassemblySettings, LinearViewCursor,
                             LinearViewObject)
    from binaryninja.enums import DisassemblyOption
    _HAVE_LINEAR = True
except Exception:
    _HAVE_LINEAR = False

# strips a leading windows drive letter like "C:"
_DRIVE = re.compile(r'^[A-Za-z]:')

# only treat a leaked string as a source path if it ends in one of these.
# Tunable; keeps random strings passed to the log function from creating junk
# directories, and narrows what the plugin will ever write to disk.
_SRC_EXT = ('.c', '.cc', '.cpp', '.cxx', '.c++',
            '.h', '.hh', '.hpp', '.hxx', '.inl',
            '.s', '.asm', '.m', '.mm')

# last output directory used this session, offered as default next time
_last_outdir = ''

# records which functions already had their content written, so re-runs into
# the same directory (e.g. with another log function) stay additive
_MANIFEST = '.arborist.json'


def load_written(outdir: str) -> set[int]:
    """Function starts whose content was written by a previous run."""
    try:
        with open(os.path.join(outdir, _MANIFEST)) as f:
            return {int(x, 16) for x in json.load(f).get('written', [])}
    except (OSError, ValueError):
        return set()


def save_written(outdir: str, written: set[int]):
    try:
        with open(os.path.join(outdir, _MANIFEST), 'w') as f:
            json.dump({'written': [f'{a:#x}' for a in sorted(written)]}, f)
    except OSError as e:
        log_warn(f'arborist: could not write manifest: {e}')


def iscall(i: MediumLevelILInstruction):
    return i.operation == mlilop.MLIL_CALL


def split_path(raw: str) -> list[str]:
    """Normalise a raw __FILE__ string into safe path components.

    The string comes from the binary and is therefore untrusted. Handles both
    '/' and '\\' separators, drops drive letters, '.', '..' and empty segments
    so the result can never climb above the chosen output root. Rejects strings
    with control characters, and keeps only things that look like source files.
    Returns [] when the string should be ignored.
    """
    raw = raw.strip()
    # reject control chars / null bytes (both a traversal-noise and a crash risk
    # for open()); real source paths never contain them
    if any(ord(ch) < 0x20 for ch in raw):
        return []
    p = _DRIVE.sub('', raw.replace('\\', '/'), count=1)
    parts = [x for x in p.split('/') if x not in ('', '.', '..')]
    if not parts:
        return []
    # must look like a source file, not an arbitrary string the logger was fed
    if not parts[-1].lower().endswith(_SRC_EXT):
        return []
    return parts


def extract_path(callee: Function, caller: Function, parami: int):
    """Return the path string passed as `parami` to `callee` from `caller`.

    Takes the first matching direct call, like logrn. None if nothing usable.
    """
    if caller.mlil is None:
        return None
    i: MediumLevelILInstruction
    for i in filter(iscall, caller.mlil.instructions):
        dest = i.operands[1]
        # only direct calls to the selected function
        if dest.operation != mlilop.MLIL_CONST_PTR or dest.constant != callee.start:
            continue
        params = i.operands[2]
        if parami >= len(params):
            continue
        arg = params[parami]
        # we only resolve literal "pointer to string" arguments
        if arg.operation != mlilop.MLIL_CONST_PTR:
            continue
        s = caller.view.get_string_at(arg.constant)
        if s is None:
            continue
        return s.value
    return None


def _hlil_fallback(func: Function) -> str:
    """Last-resort HLIL text. Guaranteed never to raise: on any failure it
    returns a stub comment so a single bad function cannot abort the export."""
    out = [f'// {func.name}  @ {func.start:#x}']
    try:
        hlil = func.hlil
        if hlil is None:
            out.append('// <no decompilation available>')
            return '\n'.join(out)
        for block in hlil:
            for instr in block:
                out.append(str(instr))
    except Exception as e:
        out.append(f'// <decompilation failed: {e}>')
    return '\n'.join(out)


def render_pseudo_c(func: Function) -> str:
    """Pseudo C via the linear view. Falls back to HLIL if unsupported."""
    if not _HAVE_LINEAR:
        return _hlil_fallback(func)
    try:
        settings = DisassemblySettings()
        settings.set_option(DisassemblyOption.ShowAddress, False)
        obj = LinearViewObject.language_representation(func.view, settings)
        cursor = LinearViewCursor(obj)
        cursor.seek_to_address(func.start)
        end = func.highest_address
        out: list[str] = []
        for _ in range(100000):  # hard cap, just in case
            lines = cursor.lines
            if not lines:
                break
            past = False
            for line in lines:
                if line.contents.address > end:
                    past = True
                    break
                out.append(str(line))
            if past or not cursor.next():
                break
        text = '\n'.join(out).rstrip()
        return text if text else _hlil_fallback(func)
    except Exception as e:
        log_warn(f'arborist: pseudo C failed for {func.name}, HLIL used ({e})')
        return _hlil_fallback(func)


class TreeTask(BackgroundTaskThread):
    def __init__(self, bv: BinaryView, func: Function):
        super().__init__('arborist: starting', can_cancel=True)
        self.bv = bv
        self.func = func

    def run(self):
        # finish() is not called automatically by the API, and must run on
        # every exit path (abort, cancel, error) or the status bar task hangs.
        try:
            self._run()
        finally:
            self.finish()

    def _run(self):
        global _last_outdir

        params = self.func.type.parameters
        if not params:
            log_warn('arborist: selected function has no parameters')
            return

        parami = get_choice_input(
            'Which argument holds the file path?', 'arborist',
            [p.name for p in params])
        if parami is None:
            return

        # 0 = structure only, 1 = structure + decompiled Pseudo C
        mode = get_choice_input('Export mode', 'arborist', [
            'tree only (empty files)',
            'tree + Pseudo C',
        ])
        if mode is None:
            return

        # ask every run, prefilled with the last directory used this session
        outdir = get_directory_name_input('Select output directory',
                                          _last_outdir)
        if not outdir:
            return
        _last_outdir = outdir

        # path "a/b/c.c" -> list of callers attributed to it
        tree: dict[str, list[Function]] = {}
        callers = list(self.func.callers)
        total = len(callers)
        log_info(f'arborist: scanning {total} callers of {self.func.name}')
        for idx, c in enumerate(callers):
            if self.cancelled:
                log_warn('arborist: cancelled')
                return
            if idx % 64 == 0:
                self.progress = f'arborist: scanning callers {idx}/{total}'
            raw = extract_path(self.func, c, parami)
            if raw is None:
                continue
            parts = split_path(raw)
            if not parts:
                continue
            tree.setdefault('/'.join(parts), []).append(c)

        if not tree:
            log_warn('arborist: no file paths found in the selected argument')
            return

        # additive: never truncate, never write the same function twice.
        written = load_written(outdir)
        root_real = os.path.realpath(outdir)
        new_files = new_funcs = 0
        done = 0
        nkeys = len(tree)
        for key, funcs in tree.items():
            if self.cancelled:
                log_warn('arborist: cancelled')
                return
            self.progress = f'arborist: writing {done}/{nkeys} files'
            done += 1
            dest = os.path.join(outdir, *key.split('/'))
            # defense in depth: resolve symlinks and confirm the target really
            # lands under the chosen root before touching the filesystem, so a
            # crafted path (or a symlink already in outdir) can't escape it
            try:
                dest_real = os.path.realpath(dest)
                if os.path.commonpath([root_real, dest_real]) != root_real:
                    raise ValueError
            except ValueError:
                log_warn(f'arborist: skipping path outside output dir: {key}')
                continue
            if not os.path.exists(dest):
                new_files += 1
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # append so a previous run's content is preserved; only functions
            # not already written are added
            fresh = [fn for fn in funcs if fn.start not in written]
            with open(dest, 'a') as f:
                if mode == 1:
                    for fn in fresh:
                        f.write(render_pseudo_c(fn))
                        f.write('\n\n')
                        written.add(fn.start)
                        new_funcs += 1

        if mode == 1:
            save_written(outdir, written)
        log_info(f'arborist: done - {total} callers scanned, '
                 f'{new_files} new file(s), {new_funcs} new function(s) '
                 f'written under {outdir}')


def run(bv: BinaryView, func: Function):
    TreeTask(bv, func).start()


PluginCommand.register_for_function(
    'export caller tree',
    'rebuild a source tree from the file-path argument passed to this function '
    'by all of its callers, filled with decompiled Pseudo C',
    run)
