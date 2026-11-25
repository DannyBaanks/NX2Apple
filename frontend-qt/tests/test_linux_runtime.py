"""Linux run planning never disguises an x86 host inspection as ARM64 execution."""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nspb_qt import linux_runtime as rt


class RuntimePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = self.root / 'dm/host'
        self.host.mkdir(parents=True)
        self.work = self.root / 'game with spaces'
        self.work.mkdir()
        self.mods = self.work / 'patched'
        self.mods.mkdir()
        (self.mods / 'main.patched.bin').write_bytes(b'game')
        (self.mods / 'main.fixups.tsv').write_text('# HOST_ABI\t1\n')
        (self.work / 'RomFS.img').write_bytes(b'image')
        self.profile = self.work / 'game.tsv'
        self.profile.write_text('title_id\t010034D02340E000\n')

    def binary(self, arch):
        data = bytearray(64)
        data[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', data, 18, {'aarch64': 183, 'x86_64': 62}[arch])
        p = self.host / ('dmhost_linux-' + arch)
        p.write_bytes(data)
        p.chmod(0o700)
        return p

    def test_x86_native_is_not_game_execution(self):
        self.binary('x86_64')
        with self.assertRaisesRegex(rt.RuntimeError, 'ARM64'):
            rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                           'native', machine='x86_64')

    def test_arm_native_argv_keeps_paths_whole(self):
        binary = self.binary('aarch64')
        p = rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                           'native', machine='aarch64')
        self.assertEqual(p.argv, [str(binary), str(self.mods)])
        self.assertEqual(p.env['DM_PROFILE'], str(self.profile))
        self.assertEqual(p.env['DM_ROMFS'], str(self.work / 'RomFS.img'))
        self.assertEqual(p.env['LC_ALL'], 'C')

    def test_qemu_missing_does_not_fall_back_to_native(self):
        self.binary('aarch64')
        with mock.patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(rt.RuntimeError, 'QEMU'):
                rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                               'qemu', machine='x86_64')

    def test_wrong_elf_is_rejected(self):
        p = self.binary('aarch64')
        p.rename(self.host / 'dmhost_linux-x86_64')
        with self.assertRaisesRegex(rt.RuntimeError, 'ELF'):
            rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                           'inspect', machine='x86_64')

    def test_qemu_uses_arm_host_and_explicit_emulator(self):
        binary = self.binary('aarch64')
        emulator = self.root / 'qemu-aarch64'
        emulator.write_text('#!/bin/sh\nexit 0\n')
        emulator.chmod(0o700)
        p = rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                           'qemu', qemu=str(emulator), machine='x86_64')
        self.assertEqual(p.argv[:2], [str(emulator), str(binary)])

    def test_missing_modules_fail_before_launch(self):
        (self.mods / 'main.fixups.tsv').unlink()
        with self.assertRaisesRegex(rt.RuntimeError, 'main.fixups.tsv'):
            rt.launch_plan(self.root, self.mods, self.work / 'RomFS.img', self.profile,
                           'qemu', machine='x86_64')


if __name__ == '__main__':
    unittest.main()
