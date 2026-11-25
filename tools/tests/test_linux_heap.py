"""Heap calibration observes isolated child logs and never caches a failed boot."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'frontend-qt'))
spec = importlib.util.spec_from_file_location('linux_prepare_tool', ROOT / 'tools/linux_runtime.py')
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)


class HeapCalibration(unittest.TestCase):
    def setUp(self):
        if not (ROOT / 'engine/dm/games/profile_guess.py').is_file():
            self.skipTest('sync engine first')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.mods = self.work / 'patched'
        self.mods.mkdir()
        (self.mods / 'main.patched.bin').write_bytes(b'fixture module')
        self.profile = self.work / 'game.tsv'
        self.profile.write_text('mem_reserve\t0x140000000\nmem_alias\t0x118000000\n'
                                'mem_stack\t0x10000000\napp_pool\t0xcd000000\nmem_used\t0x4000000\n')
        self.host = self.work / 'host'
        self.host.write_text('#!' + sys.executable + '\n' + '''import os
from pathlib import Path
p = dict(line.split() for line in Path(os.environ['DM_PROFILE']).read_text().splitlines()
         if line.strip() and not line.startswith('#'))
assert os.environ['DM_AUDIO'] == '0'
assert not any(k in os.environ for k in ('DM_FB_SHM', 'DM_AUDIO_PIPE', 'DM_INPUT_PIPE'))
assert 'heap-check-' in os.environ['DM_SAVE']
Path(os.environ['DM_SAVE'], 'probe-only').touch()
if int(p['mem_alias'], 0) > 0x58000000:
    print('SetHeapSize OOM: req 0xc6a00000 > cap 0x8000000', flush=True)
elif int(p['mem_used'], 0) < 0x6a00000:
    print('[mem] SetHeapSize 0xc6a00000 + modules 0x6600000 > app pool', flush=True)
else:
    print('[mem] heap 3.10 GB\\n>>> nnMain', flush=True)
''')
        self.host.chmod(0o700)
        self.plan = L.rt.LaunchPlan([sys.executable, str(self.host), str(self.mods)], self.work,
                                    dict(os.environ, DM_PROFILE=str(self.profile), DM_FB_SHM='unused',
                                         DM_INPUT_PIPE='unused', DM_AUDIO_PIPE='unused'))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(L.rt, 'prepared_inputs', return_value=(self.mods, self.work / 'romfs', self.profile)).start()
        mock.patch.object(L.rt, 'launch_plan', return_value=self.plan).start()

    def test_measured_layout_converges_and_reuses_only_matching_inputs(self):
        L.calibrate_heap(ROOT / 'engine', self.work, 'qemu')
        text = self.profile.read_text()
        self.assertIn('mem_alias\t0x58000000', text)
        self.assertIn('mem_used\t0x6a00000', text)
        self.assertEqual(len(list(self.work.glob('heap-check-*/*/runtime.log'))), 3)
        L.calibrate_heap(ROOT / 'engine', self.work, 'qemu')
        self.assertEqual(len(list(self.work.glob('heap-check-*'))), 1)
        (self.mods / 'main.patched.bin').write_bytes(b'changed modules')
        L.calibrate_heap(ROOT / 'engine', self.work, 'qemu')
        self.assertEqual(len(list(self.work.glob('heap-check-*'))), 2)
        self.assertFalse((self.work / 'run/save').exists())

    def test_crash_after_nnmain_is_not_cached(self):
        self.host.write_text('#!' + sys.executable + '\nprint(">>> nnMain", flush=True)\nraise SystemExit(7)\n')
        with self.assertRaisesRegex(L.rt.RuntimeError, 'failed or timed out'):
            L.calibrate_heap(ROOT / 'engine', self.work, 'qemu')
        self.assertFalse((self.work / 'linux-heap-check.json').exists())

    def test_timeout_terminates_probe_and_preserves_log(self):
        self.host.write_text('#!' + sys.executable + '\nimport os,time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n')
        _, log, _ = L.probe_heap(self.plan, self.work / 'probe', timeout=0.15)
        pid = int(log.read_text().strip())
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertFalse((self.work / 'linux-heap-check.json').exists())

    def test_cli_defaults_to_arm_game_host(self):
        with mock.patch.object(L, 'build') as build:
            self.assertEqual(L.main(['build', '--work', str(self.work)]), 0)
        self.assertEqual(build.call_args.args[-1], 'aarch64')
