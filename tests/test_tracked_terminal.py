"""Exercise the tracked Bash wrapper with a pre-existing history file."""
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest


class TrackedTerminalTests(unittest.TestCase):
    def test_first_prompt_never_submits_saved_history(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            rc_files = Path(__file__).resolve().parents[1] / 'integrations/bash'
            for name in ('terminal.bash', 'oma-usage.bash'):
                shutil.copyfile(rc_files / name, home / name)
            (home / '.bashrc').write_text("PS1='oma-qa> '\nHISTCONTROL=\n")
            history = home / 'synthetic-history'
            history.write_text('saved_tail_fixture\n')
            submitted = home / 'submitted'
            launcher = home / '.local/bin/oma-usage'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('''#!/bin/sh
for arg do
    if [ "$arg" = --check ]; then printf 'collecting\\n'; exit 0; fi
done
cat >> "$HOME/submitted"
''')
            launcher.chmod(0o700)
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            try:
                shell = subprocess.Popen(['/usr/bin/bash', '--rcfile', str(home / 'terminal.bash'), '-i'],
                                         cwd=home, env={'HOME': str(home), 'PATH': '/usr/bin',
                                                        'HISTFILE': str(history), 'TERM': 'dumb'},
                                         stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            try:
                self.wait_for_prompt(master)
                # This is before any terminal input, not merely after an exit.
                self.assertEqual(submitted.read_bytes() if submitted.exists() else b'', b'')
                os.write(master, b'true\n')
                self.wait_for_prompt(master)
                self.assertEqual(submitted.read_bytes().strip(), b'true')
                os.write(master, b'exit 0\n')
                self.assertEqual(shell.wait(timeout=5), 0)
                self.assertEqual(submitted.read_bytes().strip(), b'true')
            finally:
                if shell.poll() is None:
                    shell.kill()
                    shell.wait(timeout=5)

    def wait_for_prompt(self, master):
        deadline = time.monotonic() + 5
        output = b''
        while not output.endswith(b'oma-qa> '):
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, 'Bash did not reach its prompt')
            ready, _, _ = select.select([master], [], [], remaining)
            self.assertTrue(ready, 'Bash did not reach its prompt')
            output += os.read(master, 4096)
