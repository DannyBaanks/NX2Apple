"""Linux runtime inputs and argv, independent of Qt and of the Apple packer."""
from __future__ import annotations

import os
import platform
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from .platform_util import child_env


class RuntimeError(ValueError):
    """A run cannot start with these inputs or this host architecture."""


@dataclass
class LaunchPlan:
    argv: list[str]
    cwd: Path
    env: dict[str, str]


def machine_arch() -> str:
    m = platform.machine().lower()
    return {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(m, m)


def elf_arch(path: Path) -> str:
    try:
        with path.open('rb') as f:
            h = f.read(20)
    except OSError:
        return ''
    if len(h) < 20 or h[:6] != b'\x7fELF\x02\x01':
        return ''
    return {62: 'x86_64', 183: 'aarch64'}.get(struct.unpack_from('<H', h, 18)[0], '')


def find_qemu(named: str = '') -> str:
    for n in (named, os.environ.get('QEMU_AARCH64', ''), 'qemu-aarch64-static', 'qemu-aarch64'):
        if n and (p := shutil.which(n)):
            return p
    return ''


def launch_plan(engine: Path, modules: Path, romfs: Path, profile: Path, mode: str,
                *, qemu: str = '', machine: str = '') -> LaunchPlan:
    """Plan one explicit run. Inspection and QEMU are never called native gameplay."""
    engine, modules, romfs, profile = (Path(p).resolve() for p in (engine, modules, romfs, profile))
    for name in ('main.fixups.tsv', 'main.patched.bin'):
        if not (modules / name).is_file():
            raise RuntimeError(f'Missing {name}: prepare the game first')
    for p in (romfs, profile):
        if not p.is_file():
            raise RuntimeError(f'Missing input: {p}')
    arch = machine or machine_arch()
    if mode not in ('native', 'qemu', 'inspect'):
        raise RuntimeError(f'Unknown runtime mode: {mode}')
    if mode == 'native' and arch != 'aarch64':
        raise RuntimeError('The game is ARM64. Native execution requires an ARM64 Linux host')
    if mode != 'inspect':
        with (modules / 'main.fixups.tsv').open(encoding='utf-8') as f:
            header = f.read(2048)
        if not any(line.split() == ['#', 'HOST_ABI', '1'] for line in header.splitlines()):
            raise RuntimeError('Prepare the game for the Linux TLS ABI first')
    target = 'aarch64' if mode == 'qemu' else arch
    binary = engine / 'dm/host' / ('dmhost_linux-' + target)
    if not binary.is_file():
        raise RuntimeError(f'Build the {target} host first: {binary.name}')
    if elf_arch(binary) != target:
        raise RuntimeError(f'Wrong ELF architecture: {binary.name}')
    if not os.access(binary, os.X_OK):
        raise RuntimeError(f'Host is not executable: {binary.name}')
    argv = [str(binary), str(modules)]
    if mode == 'qemu' and arch != 'aarch64':
        emulator = find_qemu(qemu)
        if not emulator:
            raise RuntimeError('QEMU for ARM64 is required for this mode')
        argv.insert(0, emulator)
    work = modules.parent
    env = child_env({'DM_PROFILE': str(profile), 'DM_ROMFS': str(romfs),
                     'DM_SAVE': str(work / 'run/save'), 'DM_TEMP': str(work / 'run/temp')})
    return LaunchPlan(argv, work, env)


def profile_id(profile: Path) -> str:
    for line in profile.read_text(encoding='utf-8').splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[0] == 'title_id' and len(cols[1]) == 16:
            try:
                return f'{int(cols[1], 16):016X}'
            except ValueError:
                break
    raise RuntimeError('The profile has no valid title_id')


def prepared_inputs(work: Path) -> tuple[Path, Path, Path]:
    """The portable manifest holds only paths relative to the game's work directory."""
    import json
    work = work.resolve()
    d = json.loads((work / 'linux-game.json').read_text(encoding='utf-8'))
    if not isinstance(d, dict) or d.get('schema') != 1:
        raise RuntimeError('Unsupported Linux game manifest')
    result = []
    for key in ('modules', 'romfs', 'profile'):
        p = (work / d[key]).resolve()
        if not p.is_relative_to(work):
            raise RuntimeError(f'Manifest path escapes the game folder: {key}')
        if not (p.is_dir() if key == 'modules' else p.is_file()):
            raise RuntimeError(f'Missing prepared input: {key}')
        result.append(p)
    return tuple(result)
