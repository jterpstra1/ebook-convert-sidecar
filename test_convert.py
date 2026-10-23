import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import convert

FAKE_CONVERTER = """
import shutil, sys
src, out = sys.argv[1], sys.argv[2]
if "broken" in src:
    print("conversion error: broken book", file=sys.stderr)
    sys.exit(1)
shutil.copyfile(src, out)
"""


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.lib = root / "books"
        self.lib.mkdir()
        fake = root / "fake_convert.py"
        fake.write_text(FAKE_CONVERTER)
        self.env = {
            "LIBRARY_DIR": str(self.lib),
            "STATE_DIR": str(root / "config"),
            "EBOOK_CONVERT": f"{sys.executable} {fake}",
            "DRY_RUN": "false",
            "TARGET_FORMATS": "azw3",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, rel, content="book"):
        p = self.lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def run_with(self, **env):
        old = os.environ.copy()
        os.environ.update(self.env)
        os.environ.update(env)
        try:
            convert.run_once(convert.Config.from_env())
        finally:
            os.environ.clear()
            os.environ.update(old)

    def files(self):
        return sorted(str(p.relative_to(self.lib)) for p in self.lib.rglob("*") if p.is_file())

    def test_converts_next_to_original(self):
        self.book("Author/Title/Title.epub")
        self.run_with()
        self.assertEqual(self.files(), ["Author/Title/Title.azw3", "Author/Title/Title.epub"])

    def test_dry_run_writes_nothing(self):
        self.book("Title.epub")
        self.run_with(DRY_RUN="true")
        self.assertEqual(self.files(), ["Title.epub"])

    def test_skips_existing_target_case_insensitive(self):
        self.book("Title.epub")
        self.book("title.AZW3", "original")
        self.run_with()
        self.assertEqual((self.lib / "title.AZW3").read_text(), "original")
        self.assertEqual(self.files(), ["Title.epub", "title.AZW3"])

    def test_multiple_targets_and_preferred_source(self):
        self.book("Title.epub", "from-epub")
        self.book("Title.mobi", "from-mobi")
        self.run_with(SOURCE_FORMATS="epub,mobi", TARGET_FORMATS="azw3,mobi,pdf")
        self.assertEqual((self.lib / "Title.azw3").read_text(), "from-epub")
        self.assertEqual((self.lib / "Title.pdf").read_text(), "from-epub")
        self.assertEqual((self.lib / "Title.mobi").read_text(), "from-mobi")

    def test_ignore_file_and_hidden_dirs_skipped(self):
        self.book("skip/.ignore")
        self.book("skip/A.epub")
        self.book(".hidden/B.epub")
        self.run_with()
        self.assertNotIn("skip/A.azw3", self.files())
        self.assertNotIn(".hidden/B.azw3", self.files())

    def state(self):
        return json.loads((Path(self.env["STATE_DIR"]) / "state.json").read_text())

    def touch_later(self, path, content):
        """Rewrite a file the way library apps do when they save metadata: new content, newer mtime."""
        st = path.stat()
        path.write_text(content)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    def test_custom_ignore_files(self):
        self.book("a/.nomedia")
        self.book("a/A.epub")
        self.book("b/.ignore")
        self.book("b/B.epub")
        self.run_with(IGNORE_FILES=".nomedia")
        self.assertNotIn("a/A.azw3", self.files())
        self.assertIn("b/B.azw3", self.files())

    def test_temporary_file_is_hidden(self):
        part = convert.part_path(self.lib / "Author" / "Title.azw3")
        self.assertTrue(part.name.startswith("."))
        self.assertEqual(part.parent, self.lib / "Author")

    def test_failure_recorded_and_retried_only_when_source_changes(self):
        broken = self.book("broken.epub")
        self.book("good.epub")
        self.run_with()
        self.assertEqual(self.files(), ["broken.epub", "good.azw3", "good.epub"])
        self.assertIn("broken book", self.state()["failed"]["broken.azw3"]["error"])
        self.assertEqual(convert.find_jobs(self.config(), self.state()), [])
        self.touch_later(broken, "still broken")
        self.assertEqual(len(convert.find_jobs(self.config(), self.state())), 1)

    def test_reconverts_when_source_changes(self):
        epub = self.book("Author/Title/Title.epub", "v1")
        self.run_with()
        self.assertEqual((self.lib / "Author/Title/Title.azw3").read_text(), "v1")
        self.run_with()  # nothing changed: nothing to do
        self.assertEqual(convert.find_jobs(self.config(), self.state()), [])
        self.touch_later(epub, "v2 with new metadata")
        self.run_with()
        self.assertEqual((self.lib / "Author/Title/Title.azw3").read_text(), "v2 with new metadata")

    def test_never_replaces_files_it_did_not_create(self):
        epub = self.book("Title.epub", "v1")
        self.book("Title.azw3", "my own")
        self.touch_later(epub, "v2")
        self.run_with()
        self.assertEqual((self.lib / "Title.azw3").read_text(), "my own")

    def test_follows_converted_file_that_the_library_app_moved(self):
        epub = self.book("Old/Title.epub", "v1")
        self.run_with()
        new_dir = self.lib / "New Author" / "New Title"
        new_dir.mkdir(parents=True)
        os.rename(epub, new_dir / "New Title.epub")
        os.rename(self.lib / "Old/Title.azw3", new_dir / "New Title.azw3")
        self.run_with()  # moved but unchanged: nothing to do
        self.assertEqual(list(self.state()["converted"]), ["New Author/New Title/New Title.azw3"])
        self.touch_later(new_dir / "New Title.epub", "v2")
        self.run_with()
        self.assertEqual((new_dir / "New Title.azw3").read_text(), "v2")

    def test_deleted_converted_file_is_recreated(self):
        self.book("Title.epub")
        self.run_with()
        (self.lib / "Title.azw3").unlink()
        self.run_with()
        self.assertIn("Title.azw3", self.files())
        self.assertEqual(list(self.state()["converted"]), ["Title.azw3"])

    def test_dry_run_reports_updates_and_writes_no_state(self):
        epub = self.book("Title.epub", "v1")
        self.run_with()
        self.touch_later(epub, "v2")
        before = self.state()
        self.run_with(DRY_RUN="true")
        self.assertEqual(self.state(), before)
        self.assertEqual((self.lib / "Title.azw3").read_text(), "v1")

    def test_max_per_run(self):
        for n in range(3):
            self.book(f"B{n}.epub")
        self.run_with(MAX_PER_RUN="2")
        self.assertEqual(len([f for f in self.files() if f.endswith(".azw3")]), 2)
        self.run_with(MAX_PER_RUN="2")
        self.assertEqual(len([f for f in self.files() if f.endswith(".azw3")]), 3)

    def test_leftover_part_files_removed(self):
        self.book("Title.epub")
        self.book(".Title.azw3" + convert.PART_SUFFIX, "half")
        self.run_with()
        self.assertEqual(self.files(), ["Title.azw3", "Title.epub"])
        self.assertEqual((self.lib / "Title.azw3").read_text(), "book")

    def test_unreadable_file_is_skipped_not_fatal(self):
        (self.lib / "Gone.epub").symlink_to(self.lib / "does-not-exist.epub")
        self.book("Good.epub")
        self.run_with()
        self.assertIn("Good.azw3", self.files())

    def test_plugins_installed_into_fresh_config(self):
        root = Path(self.tmp.name)
        plugins = root / "plugins"
        plugins.mkdir()
        (plugins / "B Plugin.zip").write_text("zip")
        (plugins / "a.zip").write_text("zip")
        (plugins / "B Plugin.json").write_text('{"setting": 1}')
        config = root / "calibre-config"
        (config / "plugins").mkdir(parents=True)
        (config / "plugins" / "old.zip").write_text("removed plugin")
        calls = root / "calls.txt"
        fake = root / "fake_customize.py"
        fake.write_text(f"import sys\nopen({str(calls)!r}, 'a').write(sys.argv[2] + '\\n')\n")
        old = os.environ.copy()
        os.environ.update(self.env, PLUGINS_DIR=str(plugins), CALIBRE_CONFIG_DIRECTORY=str(config),
                          CALIBRE_CUSTOMIZE=f"{sys.executable} {fake}")
        try:
            convert.install_plugins(convert.Config.from_env())
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(calls.read_text().splitlines(), [str(plugins / "B Plugin.zip"), str(plugins / "a.zip")])
        self.assertFalse((config / "plugins" / "old.zip").exists())
        self.assertEqual((config / "plugins" / "B Plugin.json").read_text(), '{"setting": 1}')

    def test_failed_conversions_retried_when_plugins_change(self):
        root = Path(self.tmp.name)
        plugins = root / "plugins"
        plugins.mkdir()
        needs_plugin = root / "needs_plugin.py"
        needs_plugin.write_text(
            "import os, shutil, sys\n"
            f"if not os.path.exists({str(plugins / 'helper.zip')!r}):\n"
            "    sys.exit('cannot read this book without a plugin')\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n"
        )
        env = {"PLUGINS_DIR": str(plugins), "EBOOK_CONVERT": f"{sys.executable} {needs_plugin}"}
        self.book("Title.epub")
        self.run_with(**env)
        self.run_with(**env)  # same plugins: stays skipped
        self.assertIn("Title.azw3", self.state()["failed"])
        (plugins / "helper.zip").write_text("zip")
        self.run_with(**env)
        self.assertIn("Title.azw3", self.files())
        self.assertEqual(self.state()["failed"], {})

    def test_part_symlink_not_followed(self):
        self.book("Title.epub")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("untouched")
        target = self.lib / "Title.azw3"
        source, _, fmt, _ = convert.find_jobs(self.config(), convert.load_state(self.config()))[0]
        convert.part_path(target).symlink_to(outside)
        convert.convert(self.config(), source, target, fmt)
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), "book")

    def config(self):
        old = os.environ.copy()
        os.environ.update(self.env)
        try:
            return convert.Config.from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
