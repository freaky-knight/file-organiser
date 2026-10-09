import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import organizer
import terminal_ui


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = organizer.load_config(self.root / "missing.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_case_insensitive_and_unknown_classification(self):
        self.assertEqual(organizer.category_for(Path("PHOTO.JpG"), self.config), "Images")
        self.assertEqual(organizer.category_for(Path("program.AppImage"), self.config), "Programs")
        self.assertEqual(organizer.category_for(Path("README"), self.config), "Others")

    def test_preview_plan_does_not_change_files(self):
        source = self.root / "my homework (final).PDF"
        source.write_text("content", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].destination, self.root / "Documents" / source.name)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "Documents").exists())

    def test_duplicate_name_is_renamed(self):
        (self.root / "photo.jpg").write_text("new")
        images = self.root / "Images"
        images.mkdir()
        (images / "photo.jpg").write_text("old")
        plan = organizer.build_plan(self.root, self.config)
        self.assertEqual(plan.operations[0].destination.name, "photo_1.jpg")

    def test_recursive_scan_does_not_rescan_destination_folder(self):
        (self.root / "nested").mkdir()
        (self.root / "nested" / "song.mp3").write_text("song")
        (self.root / "Music").mkdir()
        (self.root / "Music" / "already.mp3").write_text("organized")
        plan = organizer.build_plan(self.root, self.config, recursive=True)
        self.assertEqual([op.source.name for op in plan.operations], ["song.mp3"])

    def test_malformed_configuration_is_clear_error(self):
        path = self.root / "config.json"
        path.write_text("{ bad json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Configuration error"):
            organizer.load_config(path)

    def test_execution_and_undo(self):
        source = self.root / "note.txt"
        source.write_text("note")
        plan = organizer.build_plan(self.root, self.config)
        local_history = self.root / "history.json"
        with patch.object(organizer, "history_path", return_value=local_history):
            successful, failed = organizer.execute_plan(plan, "rename", quiet=True)
            self.assertEqual(len(successful), 1)
            self.assertFalse(failed)
            self.assertFalse(source.exists())
            self.assertTrue((self.root / "Documents" / "note.txt").exists())
            self.assertEqual(organizer.undo(quiet=True), organizer.EXIT_OK)
        self.assertTrue(source.exists())

    def test_explicit_overwrite_replaces_existing_file(self):
        source = self.root / "photo.jpg"
        source.write_text("new")
        destination = self.root / "Images" / "photo.jpg"
        destination.parent.mkdir()
        destination.write_text("old")
        plan = organizer.build_plan(self.root, self.config, conflict_policy="overwrite")
        with patch.object(organizer, "history_path", return_value=self.root / "history.json"):
            successful, failed = organizer.execute_plan(plan, "overwrite", quiet=True)
        self.assertEqual(len(successful), 1)
        self.assertFalse(failed)
        self.assertEqual(destination.read_text(), "new")

    def test_ask_conflict_decline_is_reported_as_skipped(self):
        source = self.root / "photo.jpg"
        source.write_text("new", encoding="utf-8")
        destination = self.root / "Images" / "photo.jpg"
        destination.parent.mkdir()
        destination.write_text("keep", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config, conflict_policy="ask")
        with patch("builtins.input", return_value="n"):
            successful, failures = organizer.execute_plan(plan, "ask", quiet=True)
        self.assertFalse(successful)
        self.assertFalse(failures)
        self.assertEqual(plan.runtime_skipped, 1)
        self.assertEqual(source.read_text(encoding="utf-8"), "new")
        self.assertEqual(destination.read_text(encoding="utf-8"), "keep")

    def test_config_is_json_serializable(self):
        path = organizer.save_default_config(self.root / "config.json")
        with path.open(encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["conflict_policy"], "rename")

    def test_custom_category_rule_and_fallback(self):
        config = organizer.merge_config(organizer.DEFAULT_CONFIG, {
            "categories": {"Documents": [".pdf"], "Programming": [".py"]},
            "default_category": "Unsorted",
            "custom_rules": [{"category": "School", "extensions": [".pdf"]}],
        })
        rules = organizer.RuleEngine(config)
        self.assertEqual(rules.classify(Path("report.PDF")), organizer.Classification("School", "custom extension"))
        self.assertEqual(rules.classify(Path("tool.PY")), organizer.Classification("Programming", "extension"))
        self.assertEqual(rules.classify(Path("README")), organizer.Classification("Unsorted", "fallback"))

    def test_duplicate_extensions_and_invalid_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate extension"):
            organizer.merge_config(organizer.DEFAULT_CONFIG, {
                "categories": {"Images": [".jpg"], "AlsoImages": [".JPG"]},
            })
        with self.assertRaisesRegex(ValueError, "conflict_policy"):
            organizer.merge_config(organizer.DEFAULT_CONFIG, {"conflict_policy": "replace"})
        with self.assertRaisesRegex(ValueError, "unknown configuration"):
            organizer.validate_config({"not_a_real_setting": True})
        with self.assertRaisesRegex(ValueError, "unsupported date format"):
            organizer.merge_config(organizer.DEFAULT_CONFIG, {
                "date_organization": {"enabled": True, "timestamp": "modified", "format": "%Q"},
            })
        with self.assertRaisesRegex(ValueError, "single folder name"):
            organizer.merge_config(organizer.DEFAULT_CONFIG, {
                "date_organization": {"enabled": True, "timestamp": "modified", "format": "../"},
            })

    def test_date_and_size_destinations_use_configuration(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"x" * 2048)
        timestamp = datetime(2024, 1, 1, 12).timestamp()
        os.utime(source, (timestamp, timestamp))
        config = organizer.merge_config(organizer.DEFAULT_CONFIG, {
            "date_organization": {"enabled": True, "timestamp": "modified", "format": "%Y"},
            "size_thresholds_mb": {"small": 0.0001, "medium": 0.001},
            "size_organization": {"enabled": True, "labels": ["Tiny", "Normal", "Huge"]},
        })
        plan = organizer.build_plan(self.root, config)
        year = datetime.fromtimestamp(timestamp).strftime("%Y")
        self.assertEqual(plan.operations[0].destination, self.root / "Images" / year / "Huge" / "photo.jpg")

    def test_config_reset_requires_exact_confirmation(self):
        config_file = self.root / "config.json"
        config_file.write_text('{"default_category": "Keep"}', encoding="utf-8")
        app = Path(organizer.__file__).resolve()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.root / "app-data")
        cancelled = subprocess.run([sys.executable, str(app), "config", "reset", "--config", str(config_file)], input="no\n", capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(cancelled.returncode, organizer.EXIT_OK)
        self.assertIn("Keep", config_file.read_text(encoding="utf-8"))
        confirmed = subprocess.run([sys.executable, str(app), "config", "reset", "--config", str(config_file)], input="RESET\n", capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(confirmed.returncode, organizer.EXIT_OK, confirmed.stderr)
        self.assertEqual(json.loads(config_file.read_text(encoding="utf-8")), organizer.DEFAULT_CONFIG)

    def test_application_folder_protects_its_own_source_files(self):
        application_folder = Path(organizer.__file__).resolve().parent
        protected = organizer.protected_application_files(application_folder)
        self.assertIn(application_folder / "organizer.py", protected)
        self.assertIn(application_folder / "file.py", protected)
        self.assertIn(application_folder / "terminal_ui.py", protected)
        self.assertIn(application_folder / "config.json", protected)
        self.assertEqual(organizer.protected_application_files(self.root), set())

    def test_complete_cli_workflow(self):
        (self.root / "photo.JPG").write_text("same image", encoding="utf-8")
        (self.root / "copy.jpg").write_text("same image", encoding="utf-8")
        (self.root / "notes.txt").write_text("notes", encoding="utf-8")
        (self.root / "unknown.bin").write_text("data", encoding="utf-8")
        app = Path(organizer.__file__).resolve()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.root / "app-data")

        def command(*arguments, input_text=None):
            return subprocess.run(
                [sys.executable, str(app), *arguments],
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                check=False,
            )

        preview = command("preview", str(self.root), "--json")
        self.assertEqual(preview.returncode, organizer.EXIT_OK, preview.stderr)
        self.assertEqual(len(json.loads(preview.stdout)["operations"]), 4)
        self.assertTrue((self.root / "photo.JPG").exists())

        organized = command("organize", str(self.root), "--quiet", input_text="y\n")
        self.assertEqual(organized.returncode, organizer.EXIT_OK, organized.stderr)
        self.assertTrue((self.root / "Images" / "photo.JPG").exists())
        self.assertTrue((self.root / "Documents" / "notes.txt").exists())

        self.assertEqual(command("stats", str(self.root), "--json").returncode, organizer.EXIT_OK)
        self.assertIn("copy.jpg", command("find", str(self.root), "*.jpg").stdout)
        duplicate_output = command("duplicates", str(self.root), "--json")
        self.assertEqual(duplicate_output.returncode, organizer.EXIT_OK)
        self.assertIn("sha256", duplicate_output.stdout)
        self.assertEqual(command("largest", str(self.root), "--limit", "2").returncode, organizer.EXIT_OK)
        self.assertIn("organize", command("history", "--json").stdout)

        undone = command("undo", "--quiet", "--yes")
        self.assertEqual(undone.returncode, organizer.EXIT_OK, undone.stderr)
        self.assertTrue((self.root / "photo.JPG").exists())

    def test_external_config_changes_cli_rules_without_source_edits(self):
        app = Path(organizer.__file__).resolve()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.root / "app-data")

        def write_config(path, category):
            config = json.loads(json.dumps(organizer.DEFAULT_CONFIG))
            config["categories"] = {"Images": [".jpg"]}
            config["custom_rules"] = [{"category": category, "extensions": [".py"]}]
            path.write_text(json.dumps(config), encoding="utf-8")

        def run(*arguments, input_text=None):
            return subprocess.run(
                [sys.executable, str(app), *arguments], input=input_text,
                capture_output=True, text=True, encoding="utf-8", env=environment,
            )

        programming_config = self.root / "programming.json"
        write_config(programming_config, "Programming")
        folder_a = self.root / "test-a"
        folder_a.mkdir()
        (folder_a / "test.py").write_text("print('a')", encoding="utf-8")
        preview_a = run("preview", str(folder_a), "--config", str(programming_config))
        self.assertEqual(preview_a.returncode, organizer.EXIT_OK, preview_a.stderr)
        self.assertIn("test.py ->", preview_a.stdout)
        self.assertIn(str(Path("Programming") / "test.py"), preview_a.stdout)
        execute_a = run("organize", str(folder_a), "--config", str(programming_config), input_text="y\n")
        self.assertEqual(execute_a.returncode, organizer.EXIT_OK, execute_a.stderr)
        self.assertIn("ORGANIZATION COMPLETE", execute_a.stdout)
        self.assertIn("Files moved: 1", execute_a.stdout)
        self.assertTrue((folder_a / "Programming" / "test.py").exists())

        code_config = self.root / "code.json"
        write_config(code_config, "Code")
        folder_b = self.root / "test-b"
        folder_b.mkdir()
        (folder_b / "test.py").write_text("print('b')", encoding="utf-8")
        execute_b = run("organize", str(folder_b), "--config", str(code_config), input_text="y\n")
        self.assertEqual(execute_b.returncode, organizer.EXIT_OK, execute_b.stderr)
        self.assertIn("test.py ->", execute_b.stdout)
        self.assertIn(str(Path("Code") / "test.py"), execute_b.stdout)
        self.assertTrue((folder_b / "Code" / "test.py").exists())

        folder_c = self.root / "test-c"
        folder_c.mkdir()
        (folder_c / "test.py").write_text("print('c')", encoding="utf-8")
        preview_c = run("preview", str(folder_c), "--config", str(code_config))
        self.assertEqual(preview_c.returncode, organizer.EXIT_OK, preview_c.stderr)
        self.assertIn("test.py ->", preview_c.stdout)
        self.assertIn(str(Path("Code") / "test.py"), preview_c.stdout)
        self.assertTrue((folder_c / "test.py").exists())
        declined = run("organize", str(folder_c), "--config", str(code_config), input_text="n\n")
        self.assertEqual(declined.returncode, organizer.EXIT_OK, declined.stderr)
        self.assertIn("No files were changed", declined.stdout)
        self.assertTrue((folder_c / "test.py").exists())

        invalid_config = self.root / "invalid.json"
        invalid_config.write_text('{"categories": "not an object"}', encoding="utf-8")
        invalid = run("preview", str(folder_c), "--config", str(invalid_config))
        self.assertEqual(invalid.returncode, organizer.EXIT_INVALID)
        self.assertIn("Configuration error", invalid.stderr)

    def test_search_filters_and_storage_analysis(self):
        nested = self.root / "nested"
        nested.mkdir()
        image = self.root / "Holiday.JPG"
        image.write_bytes(b"x" * 2048)
        note = nested / "notes.txt"
        note.write_text("notes", encoding="utf-8")
        os.utime(image, (1_704_067_200, 1_704_067_200))
        os.utime(note, (1_600_000_000, 1_600_000_000))
        files = organizer.scan(self.root, self.config, recursive=True, include_hidden=False)
        self.assertEqual(organizer.search_files(files, self.config, name="holiday", extension="jpg"), [image])
        self.assertEqual(organizer.search_files(files, self.config, category="Documents"), [note])
        self.assertEqual(organizer.search_files(files, self.config, min_size=2000), [image])
        report = organizer.storage_analysis(files, self.config, limit=1)
        self.assertEqual(report["files_found"], 2)
        self.assertEqual(report["largest_files"][0]["path"], str(image))
        self.assertEqual(report["oldest_files"][0]["path"], str(note))

    def test_duplicate_savings_and_batch_rename_collision(self):
        first = self.root / "report.txt"
        second = self.root / "copy.txt"
        first.write_text("same", encoding="utf-8")
        second.write_text("same", encoding="utf-8")
        groups = organizer.duplicate_groups([first, second])
        self.assertEqual(groups[0]["potential_savings"], first.stat().st_size)
        (self.root / "archived_report.txt").write_text("existing", encoding="utf-8")
        plan = organizer.build_rename_plan(self.root, [first], prefix="archived")
        self.assertEqual(plan.operations[0].destination.name, "archived_report_1.txt")
        with patch.object(organizer, "history_path", return_value=self.root / "history.json"):
            successful, failures = organizer.execute_rename_plan(plan, quiet=True)
        self.assertEqual(len(successful), 1)
        self.assertFalse(failures)
        self.assertTrue((self.root / "archived_report_1.txt").exists())

    def test_multiple_folders_and_overlap_validation(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir(); second.mkdir()
        selected = organizer.selected_folders(Namespace(folder=str(first), also=[str(second)]))
        self.assertEqual(selected, [first.resolve(), second.resolve()])
        (first / "child").mkdir()
        with self.assertRaisesRegex(ValueError, "overlapping folders"):
            organizer.selected_folders(Namespace(folder=str(first), also=[str(first / "child")]))

    def test_folder_validation_reports_missing_and_non_directory_paths(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            organizer.normalized_folder(str(self.root / "missing"))
        file_path = self.root / "not-a-folder.txt"
        file_path.write_text("content", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a folder"):
            organizer.normalized_folder(str(file_path))

    def test_scan_surfaces_file_permission_errors(self):
        (self.root / "secret.txt").write_text("private", encoding="utf-8")
        with patch.object(Path, "stat", side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(PermissionError, "access denied"):
                organizer.scan(self.root, self.config, recursive=False, include_hidden=False)

    def test_category_names_reject_paths_and_windows_reserved_names(self):
        for category in ("..", "bad:name", "CON", "folder."):
            with self.subTest(category=category), self.assertRaises(ValueError):
                organizer.merge_config(organizer.DEFAULT_CONFIG, {"categories": {category: [".sample"]}})

    def test_recursive_scan_skips_symbolic_link_files(self):
        target = self.root / "target.txt"
        target.write_text("data", encoding="utf-8")
        linked = self.root / "linked.txt"
        try:
            os.symlink(target, linked)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symbolic links are unavailable: {error}")
        found = organizer.scan(self.root, self.config, recursive=True, include_hidden=False)
        self.assertEqual(found, [target])

    def test_preview_cli_does_not_create_application_data_or_change_source(self):
        source = self.root / "notes.txt"
        source.write_text("content", encoding="utf-8")
        app_data = self.root / "app-data"
        app = Path(organizer.__file__).resolve()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(app_data)
        result = subprocess.run(
            [sys.executable, str(app), "--verbose", "preview", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(result.returncode, organizer.EXIT_OK, result.stderr)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "Documents").exists())
        self.assertFalse(app_data.exists())

    def test_late_collision_is_never_overwritten(self):
        source = self.root / "photo.jpg"
        source.write_text("source", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config, conflict_policy="overwrite")
        destination = plan.operations[0].destination
        destination.parent.mkdir()
        destination.write_text("destination", encoding="utf-8")
        successful, failures = organizer.execute_plan(plan, "overwrite", quiet=True)
        self.assertFalse(successful)
        self.assertEqual(len(failures), 1)
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(destination.read_text(encoding="utf-8"), "destination")

    def test_no_overwrite_move_refuses_existing_destination(self):
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("source", encoding="utf-8")
        destination.write_text("destination", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            organizer.move_no_overwrite(source, destination)
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(destination.read_text(encoding="utf-8"), "destination")

    def test_no_overwrite_move_falls_back_to_exclusive_copy(self):
        source = self.root / "source.txt"
        destination = self.root / "nested" / "destination.txt"
        source.write_text("source data", encoding="utf-8")
        destination.parent.mkdir()
        with patch.object(organizer.os, "link", side_effect=OSError("cross-device link")):
            organizer.move_no_overwrite(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "source data")

    def test_history_is_versioned_and_invalid_history_is_reported(self):
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("data", encoding="utf-8")
        entry_path = self.root / "history.json"
        operation = organizer.Operation(source, destination, "Documents")
        with patch.object(organizer, "history_path", return_value=entry_path):
            organizer.record_history(self.root, [operation])
            self.assertEqual(organizer.load_history()[0]["moves"][0]["source"], str(source))
        saved = json.loads(entry_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(len(saved["entries"]), 1)
        entry_path.write_text('{"version": 99, "entries": []}', encoding="utf-8")
        with patch.object(organizer, "history_path", return_value=entry_path):
            with self.assertRaisesRegex(ValueError, "History error"):
                organizer.load_history()

    def test_undo_never_overwrites_original_and_keeps_partial_history(self):
        source = self.root / "note.txt"
        destination = self.root / "Documents" / "note.txt"
        destination.parent.mkdir()
        source.write_text("new original", encoding="utf-8")
        destination.write_text("organized", encoding="utf-8")
        history_file = self.root / "history.json"
        operation = organizer.Operation(source, destination, "Documents")
        with patch.object(organizer, "history_path", return_value=history_file):
            organizer.record_history(self.root, [operation])
            self.assertEqual(organizer.undo(quiet=True), organizer.EXIT_PARTIAL)
            self.assertEqual(source.read_text(encoding="utf-8"), "new original")
            self.assertEqual(destination.read_text(encoding="utf-8"), "organized")
            entry = organizer.load_history()[0]
            self.assertEqual(entry["status"], "partially_undone")
            self.assertEqual(len(entry["moves"]), 1)

    def test_partial_move_failure_preserves_successful_work_and_history(self):
        first = self.root / "a.txt"
        second = self.root / "b.txt"
        first.write_text("a", encoding="utf-8")
        second.write_text("b", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config)
        original_move = organizer.move_no_overwrite
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("test permission failure")
            return original_move(source, destination)

        history_file = self.root / "history.json"
        with patch.object(organizer, "history_path", return_value=history_file), patch.object(organizer, "move_no_overwrite", side_effect=fail_second):
            successful, failures = organizer.execute_plan(plan, "rename", quiet=True)
            self.assertEqual(len(organizer.load_history()), 1)
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(sum(path.exists() for path in (first, second)), 1)

    def test_interrupted_operation_records_completed_moves(self):
        first = self.root / "a.txt"
        second = self.root / "b.txt"
        first.write_text("a", encoding="utf-8")
        second.write_text("b", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config)
        original_move = organizer.move_no_overwrite
        calls = 0

        def interrupt_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original_move(source, destination)

        history_file = self.root / "history.json"
        with patch.object(organizer, "history_path", return_value=history_file), patch.object(organizer, "move_no_overwrite", side_effect=interrupt_second):
            successful, failures = organizer.execute_plan(plan, "rename", quiet=True)
            self.assertEqual(len(organizer.load_history()[0]["moves"]), 1)
        self.assertEqual(len(successful), 1)
        self.assertEqual(failures[0][1], "interrupted by user")

    def test_history_write_failure_is_reported_after_moves(self):
        source = self.root / "note.txt"
        source.write_text("data", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config)
        with patch.object(organizer, "record_history", side_effect=OSError("disk is full")), redirect_stderr(io.StringIO()):
            successful, failures = organizer.execute_plan(plan, "rename", quiet=True)
        self.assertEqual(len(successful), 1)
        self.assertFalse(failures)
        self.assertIn("disk is full", plan.history_error)

    def test_interactive_menu_retries_invalid_choice_then_exits(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["invalid", "0"]), redirect_stdout(output):
            status = organizer.interactive()
        self.assertEqual(status, organizer.EXIT_OK)
        self.assertIn("Choose one of the listed numbers", output.getvalue())
        self.assertIn("FILE ORGANIZER", output.getvalue())

    def test_noninteractive_launch_shows_help_instead_of_waiting(self):
        output = io.StringIO()
        with patch.object(organizer.sys, "stdin", io.StringIO("")), redirect_stdout(output):
            status = organizer.main([])
        self.assertEqual(status, organizer.EXIT_OK)
        self.assertIn("usage:", output.getvalue())

    def test_menu_routes_configuration_actions_to_existing_commands(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["10", "validate", "0"]), patch.object(organizer, "main", return_value=organizer.EXIT_OK) as dispatch, redirect_stdout(output):
            status = organizer.interactive()
        self.assertEqual(status, organizer.EXIT_OK)
        dispatch.assert_called_once_with(["config", "validate"])

    def test_configuration_rejects_wrong_field_types(self):
        with self.assertRaisesRegex(ValueError, "conflict_policy"):
            organizer.merge_config(organizer.DEFAULT_CONFIG, {"conflict_policy": []})

    def test_terminal_ui_has_plain_text_fallback(self):
        class PlainStream:
            encoding = "ascii"

            def isatty(self):
                return False

        stream = PlainStream()
        with patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertFalse(terminal_ui.supports_color(stream))
            self.assertFalse(terminal_ui.supports_unicode(stream))
            self.assertEqual(terminal_ui.paint("heading", "blue", stream), "heading")
            self.assertEqual(terminal_ui.icon("success", stream), "[OK]")
            self.assertIn("+", terminal_ui.panel("TITLE", ["plain"], stream))
            self.assertNotIn("·", terminal_ui.divider("READY · EXIT", stream))

    def test_terminal_ui_header_table_and_narrow_layout(self):
        class PlainStream:
            encoding = "ascii"

            def isatty(self):
                return False

        stream = PlainStream()
        with patch.object(terminal_ui, "terminal_width", return_value=24):
            header = terminal_ui.header("9.9.9", stream)
            panel = terminal_ui.panel("A LONG PANEL TITLE", ["A very long path " * 3], stream)
            table = terminal_ui.table(["SOURCE", "DESTINATION", "ACTION"], [["file.txt", "Documents/file.txt", "MOVE"]], stream)
        with patch.object(terminal_ui, "terminal_width", return_value=80):
            long_path_table = terminal_ui.table(["PATH"], [[str(self.root / "a" / "nested" / "copy.jpg")]], stream)
        self.assertIn("FILE ORGANIZER", header)
        self.assertIn("v9.9.9", header)
        self.assertIn("Organize with", header)
        self.assertIn("move reversible.", header)
        self.assertTrue(all(len(line) <= 24 for line in header.splitlines()))
        self.assertTrue(all(len(line) <= 24 for line in panel.splitlines()))
        self.assertIn("ITEM 1", table)
        self.assertIn("DESTINATION", table)
        self.assertIn("copy.jpg", long_path_table)

    def test_terminal_table_highlights_selected_row_when_color_is_available(self):
        class ColorTerminal:
            encoding = "utf-8"

            def isatty(self):
                return True

        stream = ColorTerminal()
        with patch.object(terminal_ui, "terminal_width", return_value=80), \
             patch.object(terminal_ui, "supports_color", return_value=True), \
             patch.object(terminal_ui, "supports_unicode", return_value=False):
            rendered = terminal_ui.table(
                ["KEY", "WORKFLOW"],
                [["1", "Organize"], ["2", "Preview"]],
                stream,
                highlight_row=1,
            )
        self.assertIn(terminal_ui.COLORS["selected"], rendered)
        self.assertIn("Preview", rendered)

    def test_scan_reports_genuine_incremental_file_counts(self):
        (self.root / "one.txt").write_text("1", encoding="utf-8")
        (self.root / "two.pdf").write_text("2", encoding="utf-8")
        counts = []
        found = organizer.scan(
            self.root,
            self.config,
            recursive=False,
            include_hidden=False,
            on_progress=counts.append,
        )
        self.assertEqual(counts, list(range(1, len(found) + 1)))
        self.assertEqual(len(found), 2)

    def test_terminal_ui_respects_no_color_for_tty(self):
        class ColorTerminal:
            encoding = "utf-8"

            def isatty(self):
                return True

        stream = ColorTerminal()
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(terminal_ui.supports_color(stream))
            self.assertEqual(terminal_ui.paint("ready", "cyan", stream), "ready")

    def test_dashboard_reports_unknown_scan_count_and_real_categories(self):
        rows = organizer.dashboard_snapshot()
        self.assertIn("Files scanned  Not scanned yet", rows)
        self.assertTrue(any("categories active" in row for row in rows))

    def test_rename_plan_protects_application_files(self):
        application_folder = Path(organizer.__file__).resolve().parent
        source = application_folder / "terminal_ui.py"
        plan = organizer.build_rename_plan(application_folder, [source], prefix="backup")
        self.assertFalse(plan.operations)
        self.assertEqual(plan.skipped[0][1], "active application file")

    def test_cli_undo_requires_confirmation_or_explicit_yes(self):
        source = self.root / "note.txt"
        source.write_text("data", encoding="utf-8")
        plan = organizer.build_plan(self.root, self.config)
        history_file = self.root / "history.json"
        with patch.object(organizer, "history_path", return_value=history_file):
            successful, _ = organizer.execute_plan(plan, "rename", quiet=True)
            with patch("builtins.input", side_effect=EOFError), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                refused = organizer.main(["undo"])
            self.assertEqual(refused, organizer.EXIT_INVALID)
        self.assertEqual(len(successful), 1)

    def test_analyze_and_json_organize_are_runnable_and_machine_readable(self):
        source_folder = self.root / "sample"
        source_folder.mkdir()
        (source_folder / "note.txt").write_text("sample", encoding="utf-8")
        app = Path(organizer.__file__).resolve()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.root / "app-data")

        analysis = subprocess.run(
            [sys.executable, str(app), "analyze", str(source_folder), "--json"],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(analysis.returncode, organizer.EXIT_OK, analysis.stderr)
        self.assertEqual(json.loads(analysis.stdout)["files_found"], 1)

        organized = subprocess.run(
            [sys.executable, str(app), "organize", str(source_folder), "--yes", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(organized.returncode, organizer.EXIT_OK, organized.stderr)
        result = json.loads(organized.stdout)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((source_folder / "Documents" / "note.txt").exists())

if __name__ == "__main__":
    unittest.main()
