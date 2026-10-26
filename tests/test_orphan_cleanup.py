import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

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
        self.send = mock('send', return_value=True)
        self.pending = mock('pending', return_value=set())
        self.post = mock('post_termination_targets', return_value={})
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
        self.assertTrue(all(call.args[1] == signal.SIGSTOP
                            for call in self.send.call_args_list))

    def test_disappeared_or_changed_root(self):
        for records in ({}, {7: {**ROOT, 'start': 101}}):
            self.snap.return_value = records
            with self.assertRaisesRegex(RuntimeError, 'root'):self.run_cleanup()
        self.send.assert_not_called()

    def test_unsafe_tree_and_unstable_tree(self):
        with patch.object(mod, 'descendants', return_value={7,99}):
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):self.run_cleanup()
        with patch.object(mod, 'descendants', return_value=set(range(1000,6000))):
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):self.run_cleanup()
        growing = [{7:ROOT}]
        growing.extend([
            {7:ROOT, **{p:{'pid':p,'parent':7,'start':p,'state':'T'}
                         for p in range(8, 8+i)}}
            for i in range(1, 33)
        ])
        self.snap.side_effect = growing
        self.proc.side_effect = lambda p: ROOT if p == 7 else {
            'pid':p,'parent':7,'start':p,'state':'T'}
        with self.assertRaisesRegex(RuntimeError, 'new live process'):self.run_cleanup()
        self.assertTrue(all(c.args[1] == signal.SIGSTOP
                            for c in self.send.call_args_list))

    def test_pid_reuse_and_stat_race(self):
        self.snap.side_effect = [{7:ROOT,8:CHILD}, {7:ROOT,8:{**CHILD,'start':201}}]
        with self.assertRaisesRegex(RuntimeError, 'new live process'):self.run_cleanup()
        self.send.reset_mock();self.close.reset_mock();self.snap.side_effect=None
        self.proc.side_effect = [ROOT, {**ROOT,'start':101}]
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):self.run_cleanup()
        self.close.assert_called_once_with(107)
        self.assertEqual([call(107, signal.SIGSTOP)], self.send.call_args_list)
        self.proc.side_effect = [ROOT, FileNotFoundError()]
        with self.assertRaises(FileNotFoundError):self.run_cleanup()

    def test_approved_child_identity_is_rechecked_before_signal(self):
        self.proc.side_effect = [ROOT, ROOT, {**CHILD, 'start':201}]
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            self.run_cleanup()
        self.close.assert_any_call(108)
        self.assertFalse(any(call.args[0] == 108 for call in self.send.call_args_list))

    def test_approved_process_must_reach_stopped_state(self):
        running_child = {**CHILD, 'state':'S'}
        self.snap.return_value = {7:ROOT, 8:running_child}
        self.proc.side_effect = lambda p: {7:ROOT, 8:running_child}[p]
        with patch.object(mod.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'stabilize'):
                self.run_cleanup()

    def test_open_and_signal_failure_thaws_only_pinned_processes(self):
        self.open.side_effect = [107, ProcessLookupError()]
        with self.assertRaises(ProcessLookupError):self.run_cleanup()
        self.assertNotIn(((107, signal.SIGCONT),),
                         tuple((call.args,) for call in self.send.call_args_list))
        self.open.side_effect=lambda p:p+100
        self.send.side_effect=[RuntimeError('cannot stop'),None]
        with self.assertRaisesRegex(RuntimeError,'cannot stop'):self.run_cleanup()
        self.close.assert_called_with(107)

    def test_command_is_validated_only_after_root_is_stopped(self):
        def command_after_stop():
            self.assertTrue(any(call.args == (107, signal.SIGSTOP)
                                for call in self.send.call_args_list))
            return COMMAND
        self.read.side_effect = command_after_stop
        self.run_cleanup()

    def test_thaw_failure_does_not_skip_later_descriptors(self):
        running_root = {**ROOT, 'state': 'S'}
        running_child = {**CHILD, 'state': 'S'}
        self.proc.side_effect = [running_root, running_root, ROOT, CHILD]
        self.snap.side_effect = [{7:running_root,8:running_child},
                                 {7:running_root,8:running_child}, {}]
        self.send.side_effect = [None, None, PermissionError('cannot thaw'), None]
        with self.assertRaisesRegex(RuntimeError, 'root identity'):
            self.run_cleanup()
        self.send.assert_any_call(108, signal.SIGCONT)
        self.close.assert_any_call(107)
        self.close.assert_any_call(108)

    def test_workload_parent_is_not_protected(self):
        supervisor = {'pid':1, 'parent':0, 'start':10, 'state':'S'}
        helper = {'pid':99, 'parent':1, 'start':300, 'state':'S'}
        wrapper = {'pid':6, 'parent':1, 'start':90, 'state':'T'}
        root = {**ROOT, 'parent':6}
        records = {1:supervisor, 6:wrapper, 7:root, 8:CHILD, 99:helper}
        self.snap.return_value = records
        self.proc.side_effect = lambda p: records[p]
        self.run_cleanup()
        self.open.assert_any_call(6)
        self.send.assert_any_call(106, signal.SIGTERM)

    def test_first_term_failure_thaws_all_frozen_processes(self):
        running_root = {**ROOT, 'state':'S'}
        running_child = {**CHILD, 'state':'S'}
        self.snap.side_effect = [
            {7:running_root, 8:running_child},
            {7:running_root, 8:running_child},
            {7:ROOT, 8:CHILD},
        ]
        self.proc.side_effect = [running_root, ROOT, CHILD]
        self.send.side_effect = [True, True, PermissionError('term failed'), True, True]
        with self.assertRaisesRegex(PermissionError, 'term failed'):
            self.run_cleanup()
        self.send.assert_any_call(107, signal.SIGCONT)
        self.send.assert_any_call(108, signal.SIGCONT)

    def test_partial_termination_failure_does_not_thaw(self):
        running_root = {**ROOT, 'state':'S'}
        running_child = {**CHILD, 'state':'S'}
        self.snap.side_effect = [
            {7:running_root, 8:running_child},
            {7:running_root, 8:running_child},
            {7:ROOT, 8:CHILD},
        ]
        self.proc.side_effect = [running_root, ROOT, CHILD]
        self.send.side_effect = [True, True, True, True,
                                 RuntimeError('second term failed')]
        with self.assertRaisesRegex(RuntimeError, 'second term failed'):
            self.run_cleanup()
        self.assertNotIn(call(107, signal.SIGCONT), self.send.call_args_list)

    def test_uncertain_term_delivery_does_not_thaw(self):
        running_root = {**ROOT, 'state':'S'}
        running_child = {**CHILD, 'state':'S'}
        self.snap.side_effect = [
            {7:running_root, 8:running_child},
            {7:running_root, 8:running_child},
            {7:ROOT, 8:CHILD},
        ]
        self.proc.side_effect = [running_root, ROOT, CHILD]
        self.send.side_effect = [True, True, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_cleanup()
        self.assertNotIn(call(107, signal.SIGCONT), self.send.call_args_list)
        self.assertNotIn(call(108, signal.SIGCONT), self.send.call_args_list)

    def test_shared_non_init_supervisor_is_rejected(self):
        supervisor = {'pid':5, 'parent':1, 'start':50, 'state':'S'}
        helper = {'pid':99, 'parent':5, 'start':300, 'state':'S'}
        root = {**ROOT, 'parent':5}
        self.snap.return_value = {5:supervisor, 7:root, 8:CHILD, 99:helper}
        with self.assertRaisesRegex(RuntimeError, 'untrusted supervisor'):
            self.run_cleanup()
        self.open.assert_not_called()

    def test_additional_preflight_guards(self):
        unrelated = {'pid':10, 'parent':0, 'start':400, 'state':'S'}
        self.snap.return_value = {7:ROOT, 8:CHILD, 10:unrelated}
        with self.assertRaisesRegex(RuntimeError, 'unrelated'):
            self.run_cleanup()

        self.snap.return_value = {7:ROOT, 8:CHILD}
        self.proc.side_effect = [{**ROOT, 'start':101}]
        with self.assertRaisesRegex(RuntimeError, 'before freeze'):
            self.run_cleanup()
        self.close.assert_called_with(107)

    def test_root_must_reach_stopped_state(self):
        running = {**ROOT, 'state':'S'}
        self.snap.return_value = {7:running, 8:CHILD}
        self.proc.side_effect = lambda _pid: running
        with patch.object(mod.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'root did not stop'):
                self.run_cleanup()
        self.send.assert_any_call(107, signal.SIGCONT)

    def test_freeze_reuse_and_late_growth_guards(self):
        changed_child = {**CHILD, 'start':201}
        self.snap.side_effect = [
            {7:ROOT, 8:CHILD}, {7:ROOT, 8:CHILD},
            {7:ROOT, 8:changed_child},
        ]
        with self.assertRaisesRegex(RuntimeError, 'new live process'):
            self.run_cleanup()

    def test_late_process_fails_without_signaling_it(self):
        escaped = {'pid':9, 'parent':0, 'start':900, 'state':'S'}
        self.post.return_value = {9:escaped}
        with self.assertRaisesRegex(RuntimeError, 'new live process'):
            self.run_cleanup()
        self.assertFalse(any(call.args[0] == 109 for call in self.send.call_args_list))

    def test_late_process_on_confirmation_scan_fails_closed(self):
        escaped = {'pid':9, 'parent':0, 'start':900, 'state':'S'}
        self.post.side_effect = [{}, {9:escaped}]
        with self.assertRaisesRegex(RuntimeError, 'new live process'):
            self.run_cleanup()
        self.assertFalse(any(call.args[0] == 109 for call in self.send.call_args_list))

    def test_cleanup_close_error_is_reported(self):
        self.close.side_effect = [RuntimeError('close failed'), None]
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            self.run_cleanup()

    def test_late_target_growth_is_bounded(self):
        many = {7:ROOT}
        many.update({p:{'pid':p, 'parent':7, 'start':p, 'state':'T'}
                     for p in range(1000, 5100)})
        self.snap.side_effect = [{7:ROOT, 8:CHILD}, many]
        with self.assertRaisesRegex(RuntimeError, 'new live process'):
            self.run_cleanup()

    def test_new_identity_during_freeze_is_not_signaled(self):
        admin = {'pid':9, 'parent':1, 'start':900, 'state':'S'}
        self.snap.side_effect = [
            {7:ROOT, 8:CHILD},
            {7:ROOT, 8:CHILD, 9:admin},
        ]
        with self.assertRaisesRegex(RuntimeError, 'new live process'):
            self.run_cleanup()
        self.assertNotIn(call(9), self.open.call_args_list)
        self.assertFalse(any(call.args[0] == 109 for call in self.send.call_args_list))

    def test_waits_after_all_targets_are_pinned(self):
        running_root = {**ROOT, 'state':'S'}
        self.snap.side_effect = [
            {7:ROOT, 8:CHILD}, {7:ROOT, 8:CHILD},
            {7:running_root, 8:CHILD}, {7:ROOT, 8:CHILD},
        ]
        with patch.object(mod.time, 'sleep') as sleep:
            self.run_cleanup()
        sleep.assert_any_call(0.1)

    def test_missing_term_target_does_not_start_termination(self):
        self.send.side_effect = lambda _fd, sig: sig != signal.SIGTERM
        self.run_cleanup()


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
        self.assertEqual(mod.ancestors({7:ROOT,8:CHILD,99:{'parent':8}},99),{99,8,7})
        protected = {(99, 300)}
        records = {7: ROOT, 8: {**CHILD, 'state': 'Z'},
                   99: {'pid':99, 'parent':0, 'start':300, 'state':'S'}}
        self.assertEqual(mod.active_unprotected(records, protected), {7: ROOT})

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


@unittest.skipUnless(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'),
                     'Linux pidfds required')
class LinuxProcessIntegrationTests(unittest.TestCase):
    def test_term_handler_fork_cannot_escape_success(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'escaped.pid'
            root = os.fork()
            if root == 0:
                def handle_term(_signum, _frame):
                    child = os.fork()
                    if child == 0:
                        signal.signal(signal.SIGTERM, signal.SIG_DFL)
                        marker.write_text(str(os.getpid()))
                        time.sleep(30)
                        os._exit(0)
                    os._exit(0)
                signal.signal(signal.SIGTERM, handle_term)
                while True:
                    time.sleep(1)

            escaped = None
            real_snapshot = mod.snapshot

            def relevant_snapshot():
                records = real_snapshot()
                selected = {1, os.getpid()}
                if root in records:
                    selected.update(mod.descendants(records, root))
                if marker.exists():
                    value = marker.read_text().strip()
                    if value:
                        selected.add(int(value))
                result = {pid: dict(record) for pid, record in records.items()
                          if pid in selected}
                # Model the retained container's explicit PID 1 supervisor
                # boundary instead of the test runner's real parent/child tree.
                for pid in (os.getpid(), root):
                    if pid in result:
                        result[pid]['parent'] = 1
                return result

            try:
                time.sleep(0.05)
                start = mod.process(root)['start']
                with patch.object(mod, 'snapshot', side_effect=relevant_snapshot), \
                     patch.object(mod.Path, 'read_bytes', return_value=COMMAND), \
                     patch.object(mod.os, 'readlink', return_value='/work'), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, 'new live process'):
                        mod.cleanup(root, start, HASH, '/work')
                for _ in range(100):
                    if marker.exists() and marker.read_text().strip():
                        escaped = int(marker.read_text())
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(escaped)
                active = Path(f'/proc/{escaped}').exists() \
                    and mod.process(escaped)['state'] != 'Z'
                self.assertTrue(active, 'late process was signaled instead of failing closed')
            finally:
                for pid in (root, escaped):
                    if pid is not None:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                try:
                    os.waitpid(root, 0)
                except ChildProcessError:
                    pass


if __name__=='__main__':unittest.main()
