"""Tests for ff.  Run with:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ff  # noqa: E402


class FuzzyScoringTests(unittest.TestCase):
    def test_subsequence_required(self):
        self.assertIsNone(ff.token_score("xyz", "D:/work/projects", "d:/work/projects"))

    def test_positions_are_increasing(self):
        score, positions = ff.token_score("proj", "D:/work/projects", "d:/work/projects")
        self.assertGreater(score, 0)
        self.assertEqual(positions, sorted(positions))

    def test_boundary_match_beats_mid_word(self):
        boundary, _ = ff.token_score("proj", "D:/work/projects", "d:/work/projects")
        middle, _ = ff.token_score("proj", "D:/work/xprojects", "d:/work/xprojects")
        self.assertGreater(boundary, middle)

    def test_exact_basename_ranks_first(self):
        dirs = [
            r"D:\code\my-project\src",
            r"D:\code\project-x\src",
            r"D:\code\project",
        ]
        matches, total = ff.search(dirs, "project", 10)
        self.assertEqual(total, 3)
        self.assertEqual(matches[0].path, r"D:\code\project")

    def test_token_matches_across_a_segment_boundary(self):
        # "project" must be able to match inside "projects\readme"; a boundary
        # preference for later characters used to break this.
        dirs = [r"D:\code\projects\readme"]
        matches, total = ff.search(dirs, "project", 10)
        self.assertEqual(total, 1)

    def test_all_words_must_match(self):
        dirs = [r"D:\ai\stuff", r"D:\ai\other", r"D:\media\stuff"]
        matches, _ = ff.search(dirs, "ai stuff", 10)
        self.assertEqual([m.path for m in matches], [r"D:\ai\stuff"])

    def test_limit_and_empty_query(self):
        dirs = ["C:/a/alpha", "C:/a/alpine", "C:/a/alps"]
        matches, total = ff.search(dirs, "al", 2)
        self.assertEqual(len(matches), 2)
        self.assertEqual(total, 3)
        self.assertEqual(ff.search(dirs, "   ", 10), ([], 0))

    def test_nearby_folders_get_a_bonus(self):
        dirs = ["D:/far/proj", "D:/near/proj"]
        matches, _ = ff.search(dirs, "proj", 10, cwd="D:/near")
        self.assertEqual(matches[0].path, "D:/near/proj")


class DisplayTests(unittest.TestCase):
    def test_home_is_contracted(self):
        home = os.path.expanduser("~")
        shown = ff.display_path(os.path.join(home, "projects"))
        self.assertTrue(shown.startswith("~"))
        self.assertTrue(shown.endswith("projects"))

    def test_short_path_untouched(self):
        text, positions = ff.fit_path("abc/def", [0, 4], 40)
        self.assertEqual(text, "abc/def")
        self.assertEqual(positions, [0, 4])

    def test_long_path_is_tail_trimmed(self):
        path = r"D:\AI stuff\folderFind\some\deep\folder"
        text, positions = ff.fit_path(path, [len(path) - 6], 20)
        self.assertLessEqual(len(text), 20)
        self.assertTrue(text.startswith("..."))
        self.assertEqual(text[positions[0]], path[len(path) - 6])

    def test_highlight_groups_are_colored(self):
        colored = ff.colorize("abcd", [1, 2], True)
        self.assertIn(ff.RESULT_COLOR + "bc" + ff.RESET_COLOR, colored)
        self.assertEqual(ff.colorize("abcd", [1, 2], False), "abcd")


class PickerTests(unittest.TestCase):
    def test_parse_choice(self):
        self.assertEqual(ff.parse_choice("3\n", 10), ("pick", 2))
        self.assertEqual(ff.parse_choice(" 10 ", 10), ("pick", 9))
        self.assertEqual(ff.parse_choice("11", 10)[0], "invalid")
        self.assertEqual(ff.parse_choice("0", 10)[0], "invalid")
        self.assertEqual(ff.parse_choice("q", 10), ("quit", None))
        self.assertEqual(ff.parse_choice("", 10), ("quit", None))
        self.assertEqual(ff.parse_choice("r", 10), ("refine", None))
        self.assertEqual(ff.parse_choice("abc", 10)[0], "invalid")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for relative in [
            "alpha",
            "alpha/beta",
            "alpha/beta/gamma",
            "alpha/node_modules/skipme",
            "alpha/.git/skipme",
            os.path.join("alpha", "beta", "gamma", "delta", "epsilon"),
        ]:
            os.makedirs(os.path.join(self.root, relative), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_excludes_are_skipped(self):
        dirs, truncated = ff.build_index([self.root], ff.DEFAULT_EXCLUDES, 10, 1000)
        self.assertFalse(truncated)
        joined = "\n".join(dirs).lower()
        self.assertNotIn("node_modules", joined)
        self.assertNotIn(".git", joined)
        self.assertIn(os.path.join(self.root, "alpha", "beta"), dirs)

    def test_depth_limit(self):
        dirs, _ = ff.build_index([self.root], ff.DEFAULT_EXCLUDES, 2, 1000)
        self.assertNotIn(os.path.join(self.root, "alpha", "beta", "gamma"), dirs)
        self.assertIn(os.path.join(self.root, "alpha", "beta"), dirs)

    def test_max_entries_reports_truncation(self):
        dirs, truncated = ff.build_index([self.root], ff.DEFAULT_EXCLUDES, 10, 2)
        self.assertTrue(truncated)
        self.assertEqual(len(dirs), 2)

    def test_drive_roots_are_not_duplicated(self):
        roots = ff.prune_roots([self.root, os.path.join(self.root, "alpha")])
        self.assertEqual(roots, [self.root])


class SnippetTests(unittest.TestCase):
    def test_powershell_quotes_single_quotes(self):
        self.assertEqual(ff.ps_quote("C:\\it's here"), "'C:\\it''s here'")

    def test_python_file_is_not_used_as_launcher(self):
        with mock.patch.object(ff.shutil, "which", return_value=r"D:\proj\ff.py"):
            argv = ff.launcher_argv()
        self.assertEqual(argv[0], sys.executable)

    def test_real_launcher_preferred(self):
        def fake_which(name):
            return r"C:\tools\ff.cmd" if name == "ff.cmd" else None

        with mock.patch.object(ff.shutil, "which", side_effect=fake_which):
            argv = ff.launcher_argv()
        self.assertTrue(argv[0].endswith("ff.cmd"))

    def test_posix_snippet_has_function_and_cd(self):
        snippet = ff.shell_snippet("bash")
        self.assertIn("ff()", snippet)
        self.assertIn("builtin cd", snippet)
        self.assertIn("FOLDERFIND_OUT", snippet)

    def test_powershell_snippet_sets_location(self):
        snippet = ff.shell_snippet("powershell")
        self.assertIn("function ff", snippet)
        self.assertIn("Set-Location", snippet)


if __name__ == "__main__":
    unittest.main()
