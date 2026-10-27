"""Synthetic Bash source only; no personal history files are accessed."""
from collections import Counter
from datetime import datetime, timezone
import unittest

from oma_usage.history import extract_commands, parse_history


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


class CommandExtractionTests(unittest.TestCase):
    def test_pipeline_assignments_and_wrappers(self):
        self.assertEqual(extract_commands('FOO=x sudo -u root pacman -Q | rg python'),
                         ['sudo', 'pacman', 'rg'])
        self.assertEqual(extract_commands('sudo -n -E -H -S --user root -- command -- builtin -- printf x'),
                         ['sudo', 'command', 'builtin', 'printf'])
        self.assertEqual(extract_commands('nohup -- rg needle'), ['nohup', 'rg'])

    def test_unknown_or_dynamic_wrapper_options_are_not_commands(self):
        for source, expected in [
            ('sudo -k pacman -Q', ['sudo']),
            ('sudo --user=$USER pacman', ['sudo']),
            ('sudo -u "$USER" pacman', ['sudo']),
            ('sudo "$flags" pacman', ['sudo']),
            ('command -v rg', ['command']),
            ('builtin -x printf', ['builtin']),
            ('nohup -x rg', ['nohup']),
            ('/usr/bin/sudo pacman -Q', ['sudo']),
            ('sudo -u', ['sudo']),
        ]:
            with self.subTest(source=source):
                self.assertEqual(extract_commands(source), expected)

    def test_data_and_nonexecuted_function_bodies(self):
        source = """printf 'rg password'
function download() { curl https://example.invalid; }
other() { wget example.invalid; }
other
bash -c 'rg hidden'
eval 'curl hidden'
ssh host 'git status'
"""
        self.assertEqual(extract_commands(source), ['printf', 'other', 'bash', 'eval', 'ssh'])

    def test_compounds_and_substitutions(self):
        source = """if test -e foo; then git status; else rg bar; fi
for x in a b; do printf '%s' "$(basename "$x")"; done
( pwd && whoami )
value=$(date)
"""
        self.assertEqual(Counter(extract_commands(source)), Counter(
            ['test', 'git', 'rg', 'printf', 'basename', 'pwd', 'whoami', 'date']))

    def test_static_quoting_and_paths(self):
        self.assertEqual(extract_commands("'/opt/tools/my tool' arg; /usr/bin/r\\g x; \"gi\"'t' status"),
                         ['my tool', 'rg', 'git'])
        self.assertEqual(extract_commands(r"$'\x72\147' needle"), ['rg'])
        self.assertEqual(extract_commands(r'"literal\$name" x'), ['literal$name'])

    def test_dynamic_and_unsafe_names_are_not_guessed(self):
        source = '$tool secret; "${tool}" arg; /bin/$tool x; *grep x; ~/bin/rg x'
        self.assertEqual(extract_commands(source), [])
        self.assertEqual(extract_commands("'/tmp/bad\x01name' x"), [])
        self.assertEqual(extract_commands("'" + 'a' * 256 + "'"), [])
        self.assertEqual(extract_commands("'" + 'é' * 128 + "'"), [])
        self.assertEqual(extract_commands(r"$'bad\nname' x"), [])

    def test_dynamic_head_still_visits_command_substitution(self):
        self.assertEqual(extract_commands('$(printf rg) secret'), ['printf'])
        self.assertEqual(extract_commands('printf "$(git status)"'), ['printf', 'git'])

    def test_redirections_comments_and_heredoc_bodies_are_not_tools(self):
        source = """# rg ignored
VALUE=secret
printf x > /tmp/private-file
cat <<EOF
rg secret
$(curl hidden)
EOF
"""
        self.assertEqual(extract_commands(source), ['printf', 'cat'])

    def test_repeated_mentions_and_malformed_source(self):
        self.assertEqual(extract_commands('rg a; rg b'), ['rg', 'rg'])
        for source in ['echo "unterminated', 'if true; then', 'echo )']:
            with self.subTest(source=source):
                self.assertEqual(extract_commands(source), [])


class HistoryRecordsTests(unittest.TestCase):
    def parse(self, text):
        return parse_history(text, now=NOW)

    def test_timestamped_file_records_actual_evidence(self):
        result = self.parse('#1700000000\ngit status\n#1700000060\ngit diff\n')
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['rows'], [{
            'name': 'git', 'day': '2023-11-14', 'count': 2,
            'first_at': '2023-11-14T22:13:20.000000+00:00',
            'last_at': '2023-11-14T22:14:20.000000+00:00',
        }])

    def test_untimestamped_file_never_invents_dates(self):
        result = self.parse('rg secret /private/path\ngit status\n')
        self.assertEqual({r['name'] for r in result['rows']}, {'rg', 'git'})
        for row in result['rows']:
            self.assertIsNone(row['day'])
            self.assertIsNone(row['first_at'])
            self.assertIsNone(row['last_at'])
            self.assertEqual(row['count'], 1)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('/private/path', str(result))

    def test_timestamp_applies_to_whole_multiline_record(self):
        result = self.parse('#1700000000\ngit status\nrg needle\n#1700086400\nprintf x\n')
        self.assertEqual([(r['name'], r['day']) for r in result['rows']],
                         [('git', '2023-11-14'), ('printf', '2023-11-15'), ('rg', '2023-11-14')])

    def test_untimestamped_multiline_complete_syntax(self):
        source = 'if true; then\n  rg needle\nfi\nprintf "two\nlines"\n'
        result = self.parse(source)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual({r['name'] for r in result['rows']}, {'true', 'rg', 'printf'})

    def test_timestamp_shaped_heredoc_data_is_not_a_record_boundary(self):
        for opening, closing in [('<<EOF', 'EOF'), ("<<'EOF'", 'EOF'),
                                 ('<<-EOF', '\tEOF')]:
            for prefix, day in [('', None), ('#1700086400\n', '2023-11-15')]:
                with self.subTest(opening=opening, timestamped=bool(prefix)):
                    result = self.parse(
                        prefix + f'cat {opening}\n#1700000000\nPRIVATE_SENTINEL\n'
                        + closing + '\n#1700172800\nrg needle\n')
                    self.assertEqual(result['skipped'], 0)
                    self.assertEqual([(r['name'], r['day']) for r in result['rows']],
                                     [('cat', day), ('rg', '2023-11-16')])
                    self.assertNotIn('PRIVATE_SENTINEL', str(result))
                    self.assertNotIn('EOF', str(result))

    def test_timestamp_shaped_multiline_quoted_data_is_not_a_boundary(self):
        for quote in ["'", '"', "$'"]:
            for prefix, day in [('', None), ('#1700086400\n', '2023-11-15')]:
                with self.subTest(quote=quote, timestamped=bool(prefix)):
                    closing = quote[-1]
                    result = self.parse(
                        prefix + f'printf {quote}données\n#1700000000\n'
                        + f'PRIVATE_SENTINEL\n{closing}\n'
                        + '#1700172800\nrg needle\n')
                    self.assertEqual(result['skipped'], 0)
                    self.assertEqual([(r['name'], r['day']) for r in result['rows']],
                                     [('printf', day), ('rg', '2023-11-16')])
                    self.assertNotIn('PRIVATE_SENTINEL', str(result))

    def test_timestamp_comment_in_substitution_keeps_actual_commands(self):
        result = self.parse(
            '#1700086400\nprintf "%s" "$(\n#1700000000\n'
            "printf 'PRIVATE_SENTINEL'\ngit status\n)\"\n"
            '#1700172800\nrg needle\n')
        self.assertEqual(result['skipped'], 0)
        self.assertEqual([(r['name'], r['day'], r['count']) for r in result['rows']],
                         [('git', '2023-11-15', 1), ('printf', '2023-11-15', 2),
                          ('rg', '2023-11-16', 1)])
        self.assertNotIn('PRIVATE_SENTINEL', str(result))

    def test_timestamp_comment_inside_compound_is_not_a_record_boundary(self):
        result = self.parse(
            '#1700086400\nif true; then\n#1700000000\nrg needle\nfi\n'
            '#1700172800\ngit status\n')
        self.assertEqual(result['skipped'], 0)
        self.assertEqual([(r['name'], r['day']) for r in result['rows']],
                         [('git', '2023-11-16'), ('rg', '2023-11-15'),
                          ('true', '2023-11-15')])

    def test_ambiguous_unterminated_data_is_skipped_without_reparsing(self):
        for opening in ['cat <<EOF\n', "printf 'unfinished\n", 'printf "unfinished\n']:
            for prefix in ['', '#1700086400\n']:
                with self.subTest(opening=opening, timestamped=bool(prefix)):
                    result = self.parse(
                        prefix + opening + '#1700000000\nPRIVATE_SENTINEL\n'
                        '#1700172800\nMORE_PRIVATE_DATA\n')
                    self.assertEqual(result, {'rows': [], 'skipped': 1})

    def test_unquoted_line_continuation_does_not_invent_argument_command(self):
        result = self.parse('rg \\\nneedle\n')
        self.assertEqual([r['name'] for r in result['rows']], ['rg'])
        self.assertEqual(result['skipped'], 0)

    def test_future_and_unrepresentable_epochs_are_undated(self):
        result = self.parse('#9999999999\nrg future\n#99999999999999999999999999999999\ngit status\n')
        self.assertEqual(len(result['rows']), 2)
        self.assertTrue(all(r['day'] is None and r['first_at'] is None and r['last_at'] is None
                            for r in result['rows']))

    def test_malformed_record_diagnostic_and_next_marker_recovery(self):
        result = self.parse('#1700000000\necho )\n#1700000060\nrg needle\n')
        self.assertEqual(result['skipped'], 1)
        self.assertEqual([r['name'] for r in result['rows']], ['rg'])
        self.assertEqual(self.parse('if true; then\n')['skipped'], 1)

    def test_empty_comments_and_unordered_timestamps(self):
        self.assertEqual(self.parse(''), {'rows': [], 'skipped': 0})
        self.assertEqual(self.parse('# comment\n\n'), {'rows': [], 'skipped': 0})
        result = self.parse('#1700000060\ngit status\n#1700000000\ngit diff\n')
        row = result['rows'][0]
        self.assertLess(row['first_at'], row['last_at'])
        self.assertEqual(row['count'], 2)

    def test_dated_and_undated_mentions_do_not_mix(self):
        result = self.parse('git status\n#1700000000\ngit diff\n')
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual([r['count'] for r in result['rows']], [1, 1])
        self.assertIsNone(result['rows'][0]['last_at'])

    def test_now_requires_timezone(self):
        with self.assertRaises(ValueError):
            parse_history('rg x', now=datetime(2026, 10, 24))


if __name__ == '__main__':
    unittest.main()
