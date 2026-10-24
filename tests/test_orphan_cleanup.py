import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import runpy
import signal
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ci/ops/stop-orphaned-image-build.py'
spec = importlib.util.spec_from_file_location('orphan', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
COMMAND = b'/kaniko/executor\0--context=rust-image-def\0'
HASH = hashlib.sha256(COMMAND).hexdigest()
ROOT = {'pid': 7, 'parent': 0, 'start': 100, 'state': 'T'}
CHILD = {'pid': 8, 'parent': 7, 'start': 200, 'state': 'T'}


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        def mock(name, **kwargs):
            return self.stack.enter_context(patch.object(mod, name, **kwargs))
        self.proc = mock('process', side_effect=lambda p: {7: ROOT, 8: CHILD}[p])
        self.snap = mock('snapshot', return_value={7: ROOT, 8: CHILD})
        self.send = mock('send')
        self.pending = mock('pending', return_value=set())
        self.stack.enter_context(patch.object(mod.os, 'getpid', return_value=99))
        self.open = self.stack.enter_context(patch.object(mod.os, 'pidfd_open', create=True, side_effect=lambda p:p+100))
        self.close = self.stack.enter_context(patch.object(mod.os, 'close'))
        self.read = self.stack.enter_context(patch.object(mod.Path, 'read_bytes', return_value=COMMAND))
        self.link = self.stack.enter_context(patch.object(mod.os, 'readlink', return_value='/work'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def run_cleanup(self):
        mod.cleanup(7, 100, HASH, '/work')

    def test_success_and_forced_exit(self):
        self.run_cleanup()
        self.assertEqual(self.open.call_count, 2)
        self.assertEqual(self.close.call_count, 2)
        self.send.assert_any_call(107, signal.SIGSTOP)
        self.send.assert_any_call(108, signal.SIGTERM)
        self.pending.side_effect = [{108}, set()]
        self.run_cleanup()
        self.send.assert_any_call(108, signal.SIGKILL)
        self.pending.side_effect = [{108}, {108}]
        with self.assertRaisesRegex(RuntimeError, 'remain alive'):
            self.run_cleanup()

    def test_waits_for_stop_delivery(self):
        self.snap.side_effect = [{7:ROOT,8:CHILD}, {7:{**ROOT,'state':'S'},8:CHILD}, {7:ROOT,8:CHILD}]
        with patch.object(mod.time,'sleep') as sleep:
            self.run_cleanup()
            sleep.assert_called_once_with(0.1)

    def test_identity_refusals_do_not_signal(self):
        for pid in (0, 1, 99):
            with self.assertRaises(RuntimeError):mod.cleanup(pid, 100, HASH, '/work')
        with self.assertRaises(RuntimeError):mod.cleanup(7, 101, HASH, '/work')
        for command in (b'/bin/bash\0', COMMAND + b'extra'):
            self.read.return_value = command
            with self.assertRaises(RuntimeError):self.run_cleanup()
        self.read.return_value = COMMAND
        self.link.return_value = '/different'
        with self.assertRaises(RuntimeError):self.run_cleanup()
        self.send.assert_not_called()

    def test_disappeared_or_changed_root(self):
        for records in ({}, {7: {**ROOT, 'start': 101}}):
            self.snap.return_value = records
            with self.assertRaisesRegex(RuntimeError, 'root identity'):self.run_cleanup()
        self.send.assert_not_called()

    def test_unsafe_tree_and_unstable_tree(self):
        with patch.object(mod, 'descendants', return_value={7,99}):
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):self.run_cleanup()
        with patch.object(mod, 'descendants', return_value=set(range(5000))):
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):self.run_cleanup()
        self.snap.return_value = {i:{'pid':i,'parent':7,'start':100} for i in range(7,40)}
        self.snap.return_value[7] = ROOT
        self.proc.side_effect = lambda p:{'start':100}
        with patch.object(mod, 'descendants', side_effect=[set(range(7,8+i)) for i in range(32)]):
            with self.assertRaisesRegex(RuntimeError, 'stabilize'):self.run_cleanup()
        self.assertTrue(all(c.args[1] in (signal.SIGSTOP,signal.SIGCONT) for c in self.send.call_args_list))

    def test_pid_reuse_and_stat_race(self):
        self.snap.side_effect = [{7:ROOT,8:CHILD}, {7:ROOT,8:{**CHILD,'start':201}}]
        with self.assertRaisesRegex(RuntimeError, 'reused'):self.run_cleanup()
        self.send.assert_any_call(107,signal.SIGCONT)
        self.send.reset_mock();self.close.reset_mock();self.snap.side_effect=None
        self.proc.side_effect = [ROOT, {**ROOT,'start':101}]
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):self.run_cleanup()
        self.close.assert_called_once_with(107)
        self.send.assert_not_called()
        self.proc.side_effect = [ROOT, FileNotFoundError()]
        with self.assertRaises(FileNotFoundError):self.run_cleanup()

    def test_open_and_signal_failure_thaws_only_pinned_processes(self):
        self.open.side_effect = [107, ProcessLookupError()]
        with self.assertRaises(ProcessLookupError):self.run_cleanup()
        self.send.assert_any_call(107,signal.SIGCONT)
        self.open.side_effect=lambda p:p+100
        self.send.side_effect=[RuntimeError('cannot stop'),None]
        with self.assertRaisesRegex(RuntimeError,'cannot stop'):self.run_cleanup()
        self.close.assert_called_with(107)

    def test_cli(self):
        with patch.object(sys,'argv',[str(SCRIPT)]), self.assertRaises(SystemExit):
            runpy.run_path(str(SCRIPT),run_name='__main__')
        with patch.object(sys,'argv',[str(SCRIPT),'--wrong','7','100',HASH,'/work']), self.assertRaises(SystemExit):
            runpy.run_path(str(SCRIPT),run_name='__main__')
        with patch.object(sys,'argv',[str(SCRIPT),'--apply','1','100',HASH,'/work']), self.assertRaisesRegex(RuntimeError,'init/self'):
            runpy.run_path(str(SCRIPT),run_name='__main__')


class PrimitiveTests(unittest.TestCase):
    def test_process_snapshot_and_descendants(self):
        fields = ['S','0'] + ['0']*17 + ['100']
        with patch.object(mod.Path,'read_text',return_value='7 (strange ) name) '+' '.join(fields)):
            self.assertEqual(mod.process(7),{**ROOT,'state':'S'})
        with patch.object(mod.Path,'iterdir',return_value=[Path('self'),Path('7'),Path('8')]), patch.object(mod,'process',side_effect=[ROOT,FileNotFoundError()]):
            self.assertEqual(mod.snapshot(),{7:ROOT})
        self.assertEqual(mod.descendants({7:ROOT,8:CHILD,9:{'parent':8},10:{'parent':0}},7),{7,8,9})

    def test_send_and_wait(self):
        with patch.object(mod.signal,'pidfd_send_signal',create=True) as send:
            mod.send(5,signal.SIGTERM)
            send.side_effect=ProcessLookupError();mod.send(5,signal.SIGTERM)
            send.side_effect=PermissionError()
            with self.assertRaises(PermissionError):mod.send(5,signal.SIGTERM)
        poller=Mock();poller.poll.return_value=[(5,1)]
        with patch.object(mod.select,'poll',return_value=poller):
            self.assertEqual(mod.pending([5],1),set())
            self.assertEqual(mod.pending([],1),set())
            with patch.object(mod.time,'monotonic',side_effect=[0,2]):
                self.assertEqual(mod.pending([5],1),{5})


if __name__=='__main__':unittest.main()
