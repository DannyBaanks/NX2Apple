#!/usr/bin/env python3
"""Linux game preparation and explicit host runs. Never invokes the Apple packer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'frontend-qt'))
from nspb_qt import engine, linux_runtime as rt


def run(argv, *, cwd=None, env=None):
    subprocess.run([str(x) for x in argv], cwd=cwd, env=env, check=True)


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def prepare(eng, nsp, keys, work):
    if not nsp.is_file() or not keys.is_file():
        raise rt.RuntimeError('Choose an existing NSP and prod.keys file')
    # Game data always lives beside the dump, outside any source checkout.
    if work.is_relative_to(ROOT) or work.is_relative_to(eng):
        raise rt.RuntimeError('The game work folder must be outside the source checkout')
    source_hash = digest(nsp)
    manifest = work / 'linux-game.json'
    previous = None
    if manifest.exists():
        d = json.loads(manifest.read_text())
        if d.get('source_sha256') != source_hash:
            raise rt.RuntimeError('This work folder belongs to another dump. Choose a new work folder')
        if d.get('host_abi') == 'linux-tls-v1':
            rt.prepared_inputs(work)
            print(f'Prepared: {manifest}', flush=True)
            return
        previous = d
    work.mkdir(parents=True, exist_ok=True)
    dotnet = shutil.which('dotnet')
    if not dotnet:
        raise rt.RuntimeError('Install a .NET SDK (8 or newer) to build the extractor')
    sdks = subprocess.check_output([dotnet, '--list-sdks'], text=True)
    versions = [int(m[1]) for m in re.finditer(r'^(\d+)\.', sdks, re.M)]
    sdk = max(versions, default=0)
    if sdk < 8:
        raise rt.RuntimeError('A .NET SDK 8 or newer is required')
    # Same extractor source and locked LibHac dependency, without modifying its
    # net10.0 upstream project. The source also builds with the .NET 8 SDK.
    tools = work / 'tools/extractor'
    tools.mkdir(parents=True, exist_ok=True)
    src = eng / 'dm/tools/nx_extract/Program.cs'
    from xml.etree.ElementTree import Element, SubElement, tostring
    project = Element('Project', Sdk='Microsoft.NET.Sdk')
    props = SubElement(project, 'PropertyGroup')
    for k, v in {'OutputType': 'Exe', 'TargetFramework': f'net{sdk}.0',
                 'AllowUnsafeBlocks': 'true', 'InvariantGlobalization': 'true'}.items():
        SubElement(props, k).text = v
    items = SubElement(project, 'ItemGroup')
    SubElement(items, 'Compile', Include=str(src))
    SubElement(items, 'PackageReference', Include='LibHac', Version='0.19.0')
    csproj = tools / 'extract.csproj'
    csproj.write_bytes(tostring(project))
    run([dotnet, 'build', csproj, '-o', tools / 'bin', '--nologo'])
    extracted = work / 'extract'
    if extracted.exists() and not previous:
        raise rt.RuntimeError('An unfinished extraction exists. Preserve it and choose a new work folder')
    title_id = previous['title_id'] if previous else None
    if previous:
        print('Reusing the extraction of this NSP (matching SHA-256)', flush=True)
    else:
        print('Extracting the game', flush=True)
        extracted_run = subprocess.run([dotnet, str(tools / 'bin/extract.dll'), str(keys), str(nsp), '-',
                                    str(extracted), '--romfs-image'], capture_output=True, text=True)
        print(extracted_run.stdout, end='', flush=True)
        if extracted_run.returncode:
            print(extracted_run.stderr, end='', file=sys.stderr)
            raise rt.RuntimeError(f'Extractor failed with exit {extracted_run.returncode}')
        title = re.search(r'^title ([0-9a-fA-F]{16})$', extracted_run.stdout, re.M)
        if not title:
            raise rt.RuntimeError('The extractor did not report the title ID')
        title_id = title[1].upper()
    sys.path.insert(0, str(eng / 'dm/games'))
    import profile_guess as pg
    F = pg.facts_of(extracted / 'ExeFS', extracted / 'RomFS.img')
    tid = title_id
    profiles = work / 'profile'
    dest = profiles / tid
    known = eng / 'dm/games' / tid
    if known.is_dir() and not dest.exists():
        shutil.copytree(known, dest)
    else:
        spec = importlib.util.spec_from_file_location('linux_nsp_pipeline', eng / 'scripts/build_ipa_from_nsp.py')
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        pipeline.pg = pg
        pipeline.write_profile(dest, int(tid, 16), {'F': F, 'name': nsp.stem,
            'version': 'v0', 'display': 'unverified', 'survey': 'Linux intake, gameplay not validated'})
    env = rt.child_env({'DM_GAME': tid, 'DM_GAMES_DIR': str(profiles), 'DM_REWRITE_J': '2',
                        'DM_REWRITE_HOST': 'linux'})
    modules = pg.guess(F)['modules'][0].split()
    import tempfile
    patched = Path(tempfile.mkdtemp(prefix='patched-linux-', dir=work))
    for name in modules:
        print(f'Rewriting {name}', flush=True)
        run([sys.executable, eng / 'dm/tools/patch_nso.py', extracted / 'ExeFS' / name,
             patched, name], cwd=eng, env=env)
    d = {'schema': 1, 'title_id': tid, 'name': nsp.stem, 'source_sha256': source_hash,
         'extractor_source_sha256': digest(src), 'modules': patched.name, 'host_abi': 'linux-tls-v1',
         'romfs': 'extract/RomFS.img', 'profile': f'profile/{tid}/game.tsv',
         'gameplay': 'NOT_DEMONSTRATED'}
    manifest.write_text(json.dumps(d, indent=2) + '\n')
    print(f'Prepared: {manifest}', flush=True)


def build(eng, work, arch):
    _, _, profile = rt.prepared_inputs(work)
    env = rt.child_env({'DM_GAMES_DIR': str(profile.parent.parent)})
    run(['make', '-C', eng / 'dm/host', '-f', 'Makefile.linux',
         f'GAME={rt.profile_id(profile)}', f'ARCH={arch}', '-j2'], env=env)


def heap_fingerprint(plan, profile):
    paths = [profile, Path(plan.argv[-2])]
    modules = Path(plan.argv[-1])
    paths += sorted(modules.glob('*.patched.bin')) + sorted(modules.glob('*.fixups.tsv'))
    return [digest(p) for p in paths]


def probe_heap(plan, folder, timeout=20):
    """Observe startup using private save/temp storage and no live player pipes."""
    folder.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in plan.env.items()
           if k not in ('DM_FB_SHM', 'DM_INPUT_PIPE', 'DM_AUDIO_PIPE')}
    env.update(DM_SAVE=str(folder / 'save'), DM_TEMP=str(folder / 'temp'), DM_AUDIO='0')
    for key in ('DM_SAVE', 'DM_TEMP'):
        Path(env[key]).mkdir()
    log = folder / 'runtime.log'
    with log.open('w') as output:
        child = subprocess.Popen(plan.argv, cwd=plan.cwd, env=env,
                                 stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline, nnmain = time.monotonic() + timeout, None
            while True:
                text = log.read_text(errors='replace')
                exited = child.poll() is not None
                if '>>> nnMain' in text:
                    nnmain = nnmain or time.monotonic()
                if (exited or 'SetHeapSize OOM' in text or
                    re.search(r'\[mem\] SetHeapSize .* > app pool', text) or
                    (nnmain is not None and time.monotonic() - nnmain >= 2) or
                    time.monotonic() >= deadline):
                    break
                time.sleep(0.05)
            failed = (exited and child.returncode != 0) or time.monotonic() >= deadline
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        return log.read_text(errors='replace'), log, failed


def calibrate_heap(eng, work, mode, qemu=''):
    """Apply the engine's measured heap rules before the first playable run.

    Failed and timed-out probes are never cached. Changing modules, host or profile
    invalidates the successful check. Existing saves are never used by a probe.
    """
    modules, romfs, profile = rt.prepared_inputs(work)
    plan = rt.launch_plan(eng, modules, romfs, profile, mode, qemu=qemu)
    stamp = work / 'linux-heap-check.json'
    fingerprint = heap_fingerprint(plan, profile)
    if stamp.is_file():
        try:
            if json.loads(stamp.read_text()).get('fingerprint') == fingerprint:
                return
        except (ValueError, OSError):
            pass
    sys.path.insert(0, str(eng / 'dm/games'))
    import game
    import profile_guess as pg
    folder = Path(tempfile.mkdtemp(prefix='heap-check-', dir=work))
    print(f'Checking the Linux heap (isolated save): {folder}', flush=True)
    for rnd in range(4):
        g = game.Game(profile.parent)
        text, log, failed = probe_heap(plan, folder / str(rnd + 1))
        changes = {k: v for k, v in pg.heap_signals(text, g).items()
                   if g.get(k, '') != v[0]}
        if changes:
            lines = [line for line in profile.read_text().splitlines()
                     if line.split('\t', 1)[0].strip() not in changes]
            lines += ['', '# ---- Linux heap check ----']
            for key, (value, why) in changes.items():
                print(f'Heap check: {key} {value}: {why}', flush=True)
                lines += [f'# {why}', f'{key}\t{value}']
            staged = profile.with_name('game.tsv.heap-check.tmp')
            staged.write_text('\n'.join(lines) + '\n')
            staged.replace(profile)
            continue
        if '>>> nnMain' not in text or 'SetHeapSize OOM' in text or 'FATAL' in text or failed or ' > app pool' in text:
            raise rt.RuntimeError(f'Linux heap check failed or timed out. See {log}')
        stamp.write_text(json.dumps({'fingerprint': heap_fingerprint(plan, profile),
                                     'log': str(log.relative_to(work))}, indent=2) + '\n')
        print(f'Linux heap check ready: {log}', flush=True)
        return
    raise rt.RuntimeError(f'Linux heap calibration did not converge. See {folder}')


def check_render(eng, work):
    work.mkdir(parents=True, exist_ok=True)
    host = eng / 'dm/host'
    inc = Path(os.environ.get('NX2APPLE_VULKAN_INCLUDE', '/usr/include'))
    if not (inc / 'vulkan/vulkan.h').is_file():
        raise rt.RuntimeError('Install libvulkan-dev or set NX2APPLE_VULKAN_INCLUDE')
    cc = shutil.which('cc')
    if not cc:
        raise rt.RuntimeError('Install a C compiler')
    binary = work / 'vulkan-replay-test'
    run([cc, '-O1', '-D_GNU_SOURCE', '-DDM_LINUX=1', '-Ilinux', '-Ilinux/include', '-I.',
         '-I../gfx/nvn', '-I../gfx/nvn/include', f'-I{inc}', 'linux/nvn_vk.c',
         'linux/nvn_vk_replay.c', 'linux/tests/t_nvn_vk_replay.c',
         '-Wl,-l:libvulkan.so.1', '-lm', '-o', binary], cwd=host)
    image = work / 'vulkan-replay.ppm'
    # A missing driver is a skipped check, never successful rendering.
    env = rt.child_env({'DM_TEST_IMAGE': str(image), 'DM_VK_REQUIRE': '1'})
    run([binary], cwd=work, env=env)
    if not image.is_file():
        raise rt.RuntimeError('The renderer produced no image')
    print(f'Image: {image}', flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--engine', type=Path, default=Path(engine.engine_root(str(ROOT))))
    sp = ap.add_subparsers(dest='command', required=True)
    p = sp.add_parser('prepare')
    p.add_argument('--nsp', type=Path, required=True)
    p.add_argument('--keys', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    p = sp.add_parser('build')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--arch', choices=('aarch64', 'x86_64'), default='aarch64')
    p = sp.add_parser('run')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--mode', choices=('native', 'qemu', 'inspect'), required=True)
    p.add_argument('--qemu', default='')
    p = sp.add_parser('check-render')
    p.add_argument('--work', type=Path, required=True)
    a = ap.parse_args(argv)
    eng, work = a.engine.resolve(), a.work.resolve()
    try:
        if a.command == 'prepare':
            prepare(eng, a.nsp.resolve(), a.keys.resolve(), work)
        elif a.command == 'build':
            build(eng, work, a.arch)
        elif a.command == 'check-render':
            check_render(eng, work)
        else:
            if a.mode != 'inspect':
                calibrate_heap(eng, work, a.mode, a.qemu)
            p = rt.launch_plan(eng, *rt.prepared_inputs(work), a.mode, qemu=a.qemu)
            for key in ('DM_SAVE', 'DM_TEMP'):
                Path(p.env[key]).mkdir(parents=True, exist_ok=True)
            os.chdir(p.cwd)
            os.execvpe(p.argv[0], p.argv, p.env)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f'[RED] {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
